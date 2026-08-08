"""Phase 6 Step 2 — supervising simulation processes.

Tracks PIDs and lifecycle, divides the inference budget, and cleans up after a
restart. Three things here are less obvious than they look.

**The concurrency arithmetic is explicit and done in the parent.** Phase 2's
gate is per process, so three concurrent runs configured at 4 each put 12
requests in flight against one Ollama — the GPU exhaustion the bound exists to
prevent. Each worker is told its share through the environment at spawn:

    share = (LLM_CONCURRENCY - API_LLM_RESERVE) // MAX_CONCURRENT_SIMULATIONS

Divided by the *maximum* number of concurrent runs rather than the current
number, so a worker's share never changes underneath it. The alternative —
rebalancing live workers over IPC — buys idle GPU capacity at the cost of a
failure mode where a lost rebalance message oversubscribes the card. The
reserve exists so the API can still serve interviews and queries while a run
saturates the GPU; without it the UI looks dead exactly when someone is
watching.

**PID reuse is treated as real.** A recorded PID is not proof of identity: the
kernel recycles them, and a manager restarting hours later could find that PID
belonging to something else entirely. Killing it would be a serious bug. Every
record therefore stores the process's start time from ``/proc/<pid>/stat``,
which together with the PID is unique for the lifetime of a boot.

**An orphan is adopted if it answers.** After an API restart a healthy worker is
still running and still holds the GPU. Its socket outlives the parent, so the
manager knocks: if the worker replies it is adopted and supervision resumes. If
it does not, it is gone or wedged — killed if the PID is provably still ours,
and the run marked failed so Step 3's resume can pick it up.
"""

from __future__ import annotations

import contextlib
import json
import logging
import multiprocessing
import os
import signal
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.config import Config, get_config
from app.services.simulation_ipc import ControlClient, IPCError, socket_path
from app.services.simulation_store import (
    SimulationNotFound,
    SimulationState,
    SimulationStore,
)
from app.services.simulation_worker import CONCURRENCY_ENV, worker_main

logger = logging.getLogger(__name__)

__all__ = [
    "WORKER_FILE",
    "CapacityError",
    "SimulationManager",
    "WorkerRecord",
    "process_start_time",
    "process_status",
    "worker_share",
]

WORKER_FILE = "worker.json"

#: How long a graceful stop is given before SIGTERM, and SIGTERM before SIGKILL.
GRACEFUL_STOP_SECONDS = 30.0

#: A health probe answers quickly or not at all. Five seconds to learn that
#: something is wrong is too slow for something meant to be polled.
PROBE_TIMEOUT = 2.0
SIGTERM_SECONDS = 10.0


#: A run in one of these is over. Distinct from ``SimulationState.LOCKED``,
#: which also contains ``running`` — locking is about whether the config may
#: be edited, not about whether the work is done.
FINISHED_STATES = frozenset({SimulationState.COMPLETE, SimulationState.FAILED})


class CapacityError(RuntimeError):
    """No room to start another simulation."""


def worker_share(config: Config | None = None) -> int:
    """Each worker's slice of the inference budget. At least one."""
    config = config or get_config()
    available = config.LLM_CONCURRENCY - config.API_LLM_RESERVE
    return max(1, available // config.MAX_CONCURRENT_SIMULATIONS)


#: ``/proc/<pid>/stat`` states meaning "this process has already exited".
#: A zombie still has a ``/proc`` entry — it lingers until its parent reaps it —
#: so existence alone is not evidence that anything is running. Treating a
#: zombie as alive made a clean stop wait out its whole timeout and then kill
#: and fail a run that had in fact finished.
DEAD_STATES = frozenset({"Z", "X", "x"})


def process_status(pid: int) -> tuple[str, int] | None:
    """``(state, start_time)`` from ``/proc/<pid>/stat``, or ``None`` if gone.

    Field 3 is the state; field 22 is the start time in clock ticks. PID plus
    start time identifies a process uniquely for the life of a boot, which a
    PID alone does not.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    # The second field is the executable name in parentheses and may itself
    # contain spaces and brackets, so split from the *last* closing bracket.
    try:
        after = raw[raw.rindex(")") + 2:].split()
        return after[0], int(after[19])
    except (ValueError, IndexError):  # pragma: no cover - malformed /proc
        return None


def process_start_time(pid: int) -> int | None:
    """When the process began, in clock ticks. ``None`` if it is gone or exited."""
    status = process_status(pid)
    if status is None or status[0] in DEAD_STATES:
        return None
    return status[1]


@dataclass
class WorkerRecord:
    """What the manager remembers about a spawned process."""

    sim_id: str
    pid: int
    start_time: int | None = None
    concurrency: int = 1
    spawned_at: float = 0.0
    socket: str = ""
    #: True when this process picked up a run that had failed part-way.
    resumed: bool = False

    def alive(self) -> bool:
        """True only when the recorded process is still the one we spawned.

        A zombie counts as dead: it has exited and is merely waiting to be
        reaped, but it still has a ``/proc`` entry.
        """
        if self.pid <= 0:
            return False
        current = process_start_time(self.pid)
        if current is None:
            return False
        if self.start_time is not None and current != self.start_time:
            # Same number, different process. The kernel reused the PID.
            logger.warning("PID %d was reused; not treating it as %s",
                           self.pid, self.sim_id)
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SimulationManager:
    """Spawns, supervises and cleans up simulation processes."""

    def __init__(
        self,
        store: SimulationStore | None = None,
        *,
        config: Config | None = None,
    ) -> None:
        self.config = config or get_config()
        self.store = store or SimulationStore()
        self._processes: dict[str, multiprocessing.Process] = {}
        self._records: dict[str, WorkerRecord] = {}
        # spawn, never fork: see simulation_worker's module docstring.
        self._context = multiprocessing.get_context("spawn")

    # -- budget -------------------------------------------------------------

    @property
    def share(self) -> int:
        return worker_share(self.config)

    def capacity(self) -> int:
        """How many more simulations may start right now.

        Lingering workers do not count. One is holding its slot only so its
        agents can still be interviewed, and its run is already finished — so
        it yields the moment real work needs the room. Counting it would turn a
        convenience into "no capacity" on a machine doing nothing.
        """
        return max(0, self.config.MAX_CONCURRENT_SIMULATIONS
                   - len(self.running()) + len(self.lingering()))

    def lingering(self) -> list[str]:
        """Finished runs whose worker is still up for interviews.

        The state on disk is the authority: the worker records its outcome
        before it starts lingering, so a process that is alive for a run the
        store calls finished is one holding the interview window open.
        """
        out: list[str] = []
        for sim_id in self.running():
            try:
                state = self.store.load_meta(sim_id).state
            except SimulationNotFound:
                continue
            # Finished, not merely locked: LOCKED includes `running`, and
            # treating a live run as evictable would hand its slot away
            # mid-round. A finished run whose process is still alive is either
            # holding the interview window open or is an orphan; both yield.
            if state in FINISHED_STATES:
                out.append(sim_id)
        return out

    def is_lingering(self, sim_id: str) -> bool:
        return sim_id in self.lingering()

    def release_lingering(self, *, keep: str | None = None) -> list[str]:
        """Stop workers that are only holding an interview window open."""
        released: list[str] = []
        for sim_id in self.lingering():
            if sim_id == keep:
                continue
            logger.info("Releasing interview window on %s to free a slot", sim_id)
            with contextlib.suppress(Exception):
                self.stop(sim_id)
            released.append(sim_id)
        return released

    def budget(self) -> dict[str, Any]:
        """The arithmetic, so an operator can see where the budget went."""
        alive = self.running()
        lingering = self.lingering()
        # A lingering worker issues no requests: its run is over and it is only
        # waiting to be asked something. Counting it in the worst case would
        # overstate the GPU load an operator is looking at.
        working = [sim_id for sim_id in alive if sim_id not in lingering]
        return {
            "llm_concurrency": self.config.LLM_CONCURRENCY,
            "api_reserve": self.config.API_LLM_RESERVE,
            "max_concurrent_simulations": self.config.MAX_CONCURRENT_SIMULATIONS,
            "per_worker": self.share,
            "running": len(working),
            "lingering": len(lingering),
            "in_flight_worst_case": (self.share * len(working)
                                     + self.config.API_LLM_RESERVE),
            "capacity": self.capacity(),
        }

    # -- records ------------------------------------------------------------

    def worker_path(self, sim_id: str) -> Path:
        return self.store.sim_dir(sim_id) / WORKER_FILE

    def _save_record(self, record: WorkerRecord) -> None:
        path = self.worker_path(record.sim_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        self._records[record.sim_id] = record

    def _load_record(self, sim_id: str) -> WorkerRecord | None:
        if sim_id in self._records:
            return self._records[sim_id]
        path = self.worker_path(sim_id)
        if not path.is_file():
            return None
        try:
            return WorkerRecord(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            logger.warning("Unreadable worker record for %s: %s", sim_id, exc)
            return None

    def _forget(self, sim_id: str) -> None:
        self._processes.pop(sim_id, None)
        self._records.pop(sim_id, None)
        with contextlib.suppress(OSError, SimulationNotFound):
            self.worker_path(sim_id).unlink(missing_ok=True)

    def client(self, sim_id: str) -> ControlClient:
        return ControlClient(socket_path(self.store.sim_dir(sim_id)))

    # -- lifecycle ----------------------------------------------------------

    def _reap(self, sim_id: str) -> None:
        """Poll our own child so an exited one leaves the process table.

        Only applies to processes this manager spawned; an adopted orphan
        belongs to a parent that no longer exists and is reaped by init.
        """
        process = self._processes.get(sim_id)
        if process is not None:
            with contextlib.suppress(Exception):
                process.join(timeout=0)

    def running(self) -> list[str]:
        """Simulations with a process we can still account for."""
        alive: list[str] = []
        for sim_id in list(self._records):
            self._reap(sim_id)
            record = self._records[sim_id]
            if record.alive():
                alive.append(sim_id)
            else:
                # Gone without recording an outcome. The worker marks its own
                # completion, so silence here means it died rather than
                # finished -- if it had finished, the state would no longer be
                # running and _reconcile_finished leaves it alone.
                logger.warning("Worker for %s vanished (pid %d)", sim_id, record.pid)
                self._reconcile_finished(sim_id, failed=True)
        return alive

    def is_running(self, sim_id: str) -> bool:
        self._reap(sim_id)
        record = self._load_record(sim_id)
        return bool(record and record.alive())

    def start(self, sim_id: str) -> WorkerRecord:
        """Spawn a process for one simulation and mark the run started."""
        meta = self.store.load_meta(sim_id)
        if self.is_running(sim_id):
            raise CapacityError(f"Simulation {sim_id} is already running")
        if meta.state not in (SimulationState.DRAFT, SimulationState.FAILED):
            raise CapacityError(
                f"Simulation {sim_id} is {meta.state}; only a draft or a failed "
                f"run can be started"
            )
        # A failed run has a database and checkpoints, and the worker continues
        # from them on its own. Starting it is the supported way to resume:
        # Step 3 built the machinery and nothing else exposed it.
        resuming = meta.state == SimulationState.FAILED
        if self.capacity() < 1:
            # Reclaim any interview window before refusing: those runs are over.
            self.release_lingering(keep=sim_id)
        if self.capacity() < 1:
            raise CapacityError(
                f"Already running {self.config.MAX_CONCURRENT_SIMULATIONS} "
                f"simulation(s), the configured maximum"
            )

        # Checked here rather than only in the route handler. A rule enforced
        # in one caller is a rule every other caller walks past, and the cost
        # of finding out later is a spawned process that dies on a missing
        # file with the run recorded as having started.
        if not self.store.prepared(sim_id):
            raise CapacityError(
                f"Simulation {sim_id} has no config.json; prepare it before starting"
            )
        if not (self.store.profiles_dir(sim_id) / "profiles.json").is_file():
            raise CapacityError(
                f"Simulation {sim_id} has no population; prepare it before starting"
            )

        sim_dir = self.store.sim_dir(sim_id)
        share = self.share
        # Passed through the environment, as the spec requires, so the worker
        # cannot end up guessing its own share.
        os.environ[CONCURRENCY_ENV] = str(share)
        process = self._context.Process(
            target=worker_main, args=(sim_id, str(sim_dir), share),
            name=f"crowdsight-sim-{sim_id}", daemon=False,
        )
        process.start()

        record = WorkerRecord(
            sim_id=sim_id, pid=process.pid or -1,
            start_time=process_start_time(process.pid or -1),
            concurrency=share, spawned_at=time.time(),
            socket=str(socket_path(sim_dir)), resumed=resuming,
        )
        self._processes[sim_id] = process
        self._save_record(record)
        self.store.mark_started(sim_id)
        logger.info("%s %s as pid %s with concurrency %d",
                    "Resumed" if resuming else "Started", sim_id, process.pid, share)
        return record

    def status(self, sim_id: str) -> dict[str, Any]:
        """Ask the worker where it has got to."""
        record = self._load_record(sim_id)
        if record is None or not record.alive():
            meta = self.store.load_meta(sim_id)
            return {"sim_id": sim_id, "running": False, "state": meta.state}
        try:
            snapshot = self.client(sim_id).request("status")
        except IPCError as exc:
            return {"sim_id": sim_id, "running": True, "unreachable": str(exc),
                    "pid": record.pid}
        return {"sim_id": sim_id, "running": True, **snapshot}

    def stop(self, sim_id: str, *, timeout: float = GRACEFUL_STOP_SECONDS) -> str:
        """Ask, then insist. Returns how it ended.

        Raises for a simulation that does not exist: stopping a typo should
        say so rather than report success.
        """
        self.store.load_meta(sim_id)
        record = self._load_record(sim_id)
        if record is None or not record.alive():
            self._reconcile_finished(sim_id)
            return "not running"

        outcome = "killed"
        try:
            self.client(sim_id).request("stop")
            if self._wait(record, timeout):
                outcome = "stopped"
            else:
                outcome = self._escalate(record)
        except IPCError:
            logger.warning("%s did not accept a graceful stop; escalating", sim_id)
            outcome = self._escalate(record)

        self._reconcile_finished(sim_id, failed=outcome == "killed")
        logger.info("Stop of %s ended: %s", sim_id, outcome)
        return outcome

    def _escalate(self, record: WorkerRecord) -> str:
        if not record.alive():
            return "stopped"
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(record.pid, signal.SIGTERM)
        if self._wait(record, SIGTERM_SECONDS):
            return "terminated"
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(record.pid, signal.SIGKILL)
        self._wait(record, 5.0)
        return "killed"

    def _wait(self, record: WorkerRecord, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._reap(record.sim_id)
            if not record.alive():
                return True
            time.sleep(0.05)
        return not record.alive()

    def _reconcile_finished(self, sim_id: str, *, failed: bool = False) -> None:
        """Bring the stored state into line with a process that has ended."""
        try:
            meta = self.store.load_meta(sim_id)
        except SimulationNotFound:
            self._forget(sim_id)
            return
        if meta.state == SimulationState.RUNNING:
            self.store.mark_finished(sim_id, failed=failed)
        self._forget(sim_id)

    # -- health -------------------------------------------------------------

    def env_status(self, sim_id: str, *, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
        """Is the environment alive and accepting commands?

        Deliberately short-timeout: a health check that takes five seconds to
        report a problem is a poor health check, and this is meant to be polled
        from a monitor. The three answers are genuinely different and are kept
        apart — a wedged worker is neither running normally nor gone, and
        collapsing it into either would hide the case worth knowing about.
        """
        meta = self.store.load_meta(sim_id)
        record = self._load_record(sim_id)
        alive = bool(record and record.alive())

        payload: dict[str, Any] = {
            "sim_id": sim_id,
            "state": meta.state,
            "process_alive": alive,
            "pid": record.pid if record else None,
            "socket": str(socket_path(self.store.sim_dir(sim_id))),
        }

        if not alive:
            payload.update(status="closed", accepting_commands=False,
                           detail="No process is holding this environment")
            return payload

        client = ControlClient(socket_path(self.store.sim_dir(sim_id)),
                               timeout=timeout)
        started = time.monotonic()
        try:
            answer = client.request("ping")
        except IPCError as exc:
            payload.update(
                status="unresponsive", accepting_commands=False,
                detail=f"Process {record.pid} is alive but did not answer "
                       f"within {timeout}s: {exc}",
                probe_seconds=round(time.monotonic() - started, 3),
            )
            return payload

        payload.update(
            status="running", accepting_commands=True,
            detail="The environment answered",
            probe_seconds=round(time.monotonic() - started, 3),
            worker_pid=answer.get("pid") if isinstance(answer, dict) else None,
        )
        return payload

    def close_env(self, sim_id: str, *,
                  timeout: float = GRACEFUL_STOP_SECONDS) -> dict[str, Any]:
        """Stop the run, then verify the environment was genuinely released.

        ``stop`` returns as soon as the process is gone. This answers the
        question you actually have before archiving or deleting a run: was
        anything left behind? A socket file nobody is listening on, or a
        database still held open, are the two that bite later.
        """
        meta = self.store.load_meta(sim_id)
        outcome = self.stop(sim_id, timeout=timeout)

        path = socket_path(self.store.sim_dir(sim_id))
        record = self._load_record(sim_id)
        leftovers: list[str] = []

        if record is not None and record.alive():
            leftovers.append(f"process {record.pid} is still running")
        if path.exists():
            # A killed worker never reaches its cleanup, so this is the normal
            # residue of an escalated stop rather than a fault. Removed here so
            # a later run on the same id does not trip over it.
            with contextlib.suppress(OSError):
                path.unlink()
            if path.exists():
                leftovers.append(f"control socket {path} could not be removed")

        database = self.store.sim_dir(sim_id) / "simulation.db"
        database_readable = self._database_released(database)
        if database.exists() and not database_readable:
            leftovers.append("the run database is still locked")

        return {
            "sim_id": sim_id,
            "outcome": outcome,
            "closed": not leftovers,
            "state": self.store.load_meta(sim_id).state,
            "was": meta.state,
            "released": {
                "process": not (record is not None and record.alive()),
                "socket": not path.exists(),
                "database": database_readable,
            },
            "leftovers": leftovers,
        }

    @staticmethod
    def _database_released(path: Path) -> bool:
        """True when the run's database can be opened and read.

        A worker still holding a write transaction shows up here as a locked
        database, which is the difference between "the process exited" and
        "the environment is closed".
        """
        if not path.exists():
            return True
        try:
            connection = sqlite3.connect(path, timeout=1.0)
            try:
                connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return False
        return True

    # -- restart ------------------------------------------------------------

    def reap_orphans(self) -> dict[str, str]:
        """Reconcile with reality at startup. Returns what happened to each run.

        Called once when the API comes up. Anything recorded as running belongs
        to a process this manager did not spawn — a previous incarnation did.
        """
        outcomes: dict[str, str] = {}
        for meta in self.store.list(limit=1000):
            if meta.state != SimulationState.RUNNING:
                continue
            outcomes[meta.sim_id] = self._reap_one(meta.sim_id)
        if outcomes:
            logger.info("Reaped %d orphaned simulation(s): %s", len(outcomes), outcomes)
        return outcomes

    def _reap_one(self, sim_id: str) -> str:
        record = self._load_record(sim_id)

        # The socket outlives the parent, so a healthy worker still answers.
        try:
            if self.client(sim_id).ping():
                if record is not None:
                    self._records[sim_id] = record
                    logger.info("Adopted orphaned simulation %s (pid %d)",
                                sim_id, record.pid)
                    return "adopted"
                # Answering but unrecorded: supervisable only by socket, which
                # is enough to stop it, but there is no PID to escalate to.
                logger.warning("%s answers but has no worker record", sim_id)
                return "adopted-unrecorded"
        except IPCError:
            pass

        if record is not None and record.alive():
            # Alive but not answering: wedged, or stuck before it could listen.
            logger.warning("%s is alive at pid %d but unreachable; terminating",
                           sim_id, record.pid)
            self._escalate(record)
            self._reconcile_finished(sim_id, failed=True)
            return "killed"

        self._reconcile_finished(sim_id, failed=True)
        with contextlib.suppress(OSError, IPCError, SimulationNotFound):
            socket_path(self.store.sim_dir(sim_id)).unlink(missing_ok=True)
        return "failed"

    def shutdown(self) -> None:  # pragma: no cover - process teardown
        """Stop supervising. Does not kill runs: they outlive the API by design."""
        self._processes.clear()
        self._records.clear()
