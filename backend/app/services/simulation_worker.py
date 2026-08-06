"""Phase 6 Step 2 — what runs inside a simulation process.

Kept in its own module because :func:`worker_main` is the target of a ``spawn``
start, and a spawned child re-imports the module by name. Anything at import
time here is paid once per run; anything unimportable breaks every run.

``spawn`` rather than ``fork`` deliberately. The API process holds an asyncio
loop on a background thread, a Neo4j driver with a live connection pool, and
SQLite handles. ``fork`` copies all of it into a child where the loop's thread
does not exist, and the results range from a duplicated socket to a silently
corrupt SQLite write. ``spawn`` starts clean and costs an interpreter startup —
irrelevant next to a run measured in hours.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["WorkerState", "run_simulation", "worker_main"]

#: Set by the manager. The worker's share of LLM_CONCURRENCY, computed by
#: explicit arithmetic in the parent rather than negotiated between processes.
CONCURRENCY_ENV = "CROWDSIGHT_WORKER_CONCURRENCY"


class WorkerState:
    """Everything the control plane can report or change."""

    def __init__(self, sim_id: str, total_rounds: int) -> None:
        self.sim_id = sim_id
        self.total_rounds = total_rounds
        self.round = 0
        self.stage = "starting"
        self.stop_requested = False
        self.finished = False
        self.error = ""
        self.rounds: list[dict[str, Any]] = []
        self.progress: dict[str, Any] = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "sim_id": self.sim_id,
            "pid": os.getpid(),
            "stage": self.stage,
            "round": self.round,
            "total_rounds": self.total_rounds,
            "stop_requested": self.stop_requested,
            "finished": self.finished,
            "error": self.error,
            "rounds": self.rounds[-5:],
        }


async def run_simulation(
    sim_id: str,
    sim_dir: str | Path,
    *,
    concurrency: int | None = None,
) -> dict[str, Any]:
    """Drive one simulation to completion, answering control requests throughout.

    Persistence and checkpointing between rounds are Step 3; this establishes
    the process, the control plane, and a clean stop.
    """
    from app.config import get_config
    from app.services.simulation_config_generator import SimulationConfig
    from app.services.simulation_ipc import ControlServer, socket_path
    from app.services.simulation_persistence import RoundRecord, RunLedger
    from app.services.simulation_runner import SimulationRunner
    from app.services.simulation_store import SimulationStore

    sim_dir = Path(sim_dir)
    config = get_config()
    concurrency = concurrency or int(os.environ.get(CONCURRENCY_ENV, 0)) or None

    store = SimulationStore(sim_dir.parent)
    sim_config = SimulationConfig.load(sim_dir / "config.json")
    state = WorkerState(sim_id, sim_config.rounds)

    database_path = sim_dir / "simulation.db"
    ledger = RunLedger(database_path)

    # Resume is decided from what is on disk, not from a flag a caller passes:
    # the checkpoint is the only thing that knows how far the last attempt got.
    checkpoint = ledger.checkpoint() if database_path.exists() else None
    resuming = database_path.exists()
    if resuming:
        if checkpoint is not None:
            # The interrupted round is half-applied -- some agents acted,
            # others never got a turn. Roll back so it can be re-run cleanly.
            discarded = ledger.rollback_to(checkpoint)
            state.rounds = [r.to_dict() for r in ledger.rounds()]
            logger.info("Resuming %s after round %d%s", sim_id, checkpoint.round,
                        f", discarding {discarded}" if discarded else "")
        else:
            # A database with no checkpoint at all: the previous attempt died
            # between publishing the seed and recording round zero. The seed
            # posts are already there, and seeding again would publish the
            # event twice. Roll the whole thing back to empty instead.
            from app.services.simulation_persistence import RoundRecord as _Empty

            discarded = ledger.rollback_to(_Empty(round=-1, marks={}))
            logger.warning(
                "Resuming %s from an unfinished seed; discarding %s",
                sim_id, discarded or "nothing")

    runner = SimulationRunner(
        sim_config,
        sim_dir / "profiles",
        database_path,
        config=config,
        concurrency=concurrency,
        resume=resuming,
    )

    # Graph feedback is optional and off by default. Everything it needs --
    # a Neo4j connection, the entity names to link against -- is built only
    # when it is on, so a normal run pays nothing for it.
    feedback = _build_feedback(config, sim_config, sim_dir) \
        if config.GRAPH_MEMORY_FEEDBACK else None
    if feedback:
        logger.info("Graph memory feedback is ON for %s: outcomes will be written "
                    "to the graph and fed back into agent prompts", sim_id)

    server = ControlServer(socket_path(sim_dir))

    async def on_ping(_request: Any) -> dict[str, Any]:
        return {"sim_id": sim_id, "pid": os.getpid()}

    async def on_status(_request: Any) -> dict[str, Any]:
        snapshot = state.snapshot()
        snapshot["progress"] = ledger.progress(sim_config.rounds)
        return snapshot

    async def on_stop(_request: Any) -> dict[str, Any]:
        # Graceful: the loop checks between rounds. Killing mid-round would
        # abandon a partly-applied round, which is exactly what the rollback
        # above exists to clean up.
        state.stop_requested = True
        logger.info("Stop requested for %s at round %d", sim_id, state.round)
        return {"accepted": True, "round": state.round}

    server.handle("ping", on_ping)
    server.handle("status", on_status)
    server.handle("stop", on_stop)

    # SIGTERM is the manager escalating. Treat it as a stop request so the
    # round in flight still finishes rather than dying half-applied.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        _install_stop_signal(loop, sig, state)

    await server.start()
    try:
        state.stage = "resuming" if resuming else "preparing"
        await runner.setup()
        ledger.ensure_schema()

        done = checkpoint.round if checkpoint else -1
        runner.advance_clock(max(0, done))

        if done < 0:
            state.stage = "seeding"
            marks_before = ledger.marks()
            summary = await runner.seed()
            summary.action_counts = ledger.action_counts(marks_before)
            _checkpoint(ledger, summary)
            state.rounds.append(summary.to_dict())

        state.stage = "running"
        for index in range(max(1, done + 1), sim_config.rounds + 1):
            if state.stop_requested:
                state.stage = "stopped"
                logger.info("Stopping %s before round %d", sim_id, index)
                break
            state.round = index
            if feedback is not None:
                await _refresh_graph_memory(feedback, runner, sim_id,
                                            sim_config.graph_id, index)
            marks_before = ledger.marks()
            summary = await runner.run_round(index)
            summary.action_counts = ledger.action_counts(marks_before)
            # Written only after the round completes: a checkpoint for a round
            # that did not finish would be a lie the resume then trusts.
            _checkpoint(ledger, summary)
            state.rounds.append(summary.to_dict())
            if feedback is not None:
                await _write_graph_memory(feedback, ledger, runner, sim_id,
                                          sim_config.graph_id, index,
                                          database_path)
        else:
            state.stage = "complete"
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        state.stage = "failed"
        state.error = f"{type(exc).__name__}: {exc}"
        logger.exception("Simulation %s failed", sim_id)
        _record_outcome(store, sim_id, failed=True)
        raise
    else:
        # Only the worker knows whether it finished or merely stopped existing.
        # A manager that finds a vanished process cannot tell the difference,
        # so it assumes the worse; this is what makes the good case good.
        _record_outcome(store, sim_id, failed=False)
    finally:
        state.finished = True
        state.progress = ledger.progress(sim_config.rounds)
        await runner.close()
        await server.close()
        if feedback is not None:
            await feedback["storage"].aclose()

    return state.snapshot()


def _build_feedback(config: Any, sim_config: Any, sim_dir: Path) -> dict[str, Any] | None:
    """Everything the graph feedback needs, or nothing if it cannot be had."""
    from app.services.graph_memory_updater import GraphMemoryUpdater
    from app.storage.neo4j_storage import Neo4jStorage

    if not sim_config.graph_id:
        logger.warning("Graph memory feedback is on but this scenario has no "
                       "graph_id; skipping it")
        return None
    try:
        storage = Neo4jStorage(config)
        return {
            "storage": storage,
            "updater": GraphMemoryUpdater(storage, config=config),
        }
    except Exception:  # noqa: BLE001 - a run must not die for want of feedback
        logger.exception("Could not start graph memory feedback; continuing without")
        return None


async def _refresh_graph_memory(feedback: dict[str, Any], runner: Any, sim_id: str,
                                graph_id: str, round_index: int) -> None:
    """Fetch what the population remembers, once, for the whole round."""
    try:
        text = await feedback["updater"].context_for(
            sim_id=sim_id, graph_id=graph_id, before_round=round_index)
        runner.graph_memory["text"] = text
    except Exception:  # noqa: BLE001 - feedback is optional; the run is not
        logger.exception("Could not read graph memory for round %d", round_index)


async def _write_graph_memory(feedback: dict[str, Any], ledger: Any, runner: Any,
                              sim_id: str, graph_id: str, round_index: int,
                              database_path: Path) -> None:
    """Write one round's notable outcomes into the graph."""
    try:
        usernames = {agent_id: (record.get("username") or "")
                     for agent_id, record in runner.profiles.items()}
        post_ids = ledger.posts_by_round().get(round_index, [])
        entities = await feedback["updater"].entity_names(graph_id)
        outcome = feedback["updater"].collect(
            database_path, round_index=round_index, post_ids=post_ids,
            usernames=usernames, entity_names=entities)
        await feedback["updater"].write_round(
            outcome, sim_id=sim_id, graph_id=graph_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not write graph memory for round %d", round_index)


def _checkpoint(ledger: Any, summary: Any) -> None:
    """Persist one completed round and the boundary it ended at."""
    from app.services.simulation_persistence import RoundRecord

    ledger.record_round(RoundRecord(
        round=summary.index,
        ended_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        invoked=summary.invoked, acted=summary.acted, failed=summary.failed,
        skipped=summary.skipped, events_fired=summary.events_fired,
        action_counts=summary.action_counts, failures=summary.failures,
    ))


def _record_outcome(store: Any, sim_id: str, *, failed: bool) -> None:
    """Write the run's ending to the store, so the manager need not guess."""
    try:
        store.mark_finished(sim_id, failed=failed)
    except Exception:  # noqa: BLE001 - a bookkeeping failure must not mask the run
        logger.exception("Could not record the outcome of %s", sim_id)


def _install_stop_signal(loop: asyncio.AbstractEventLoop, sig: int,
                         state: WorkerState) -> None:
    """Install a signal handler that asks for a stop instead of dying."""
    try:
        loop.add_signal_handler(sig, lambda: setattr(state, "stop_requested", True))
    except (NotImplementedError, RuntimeError):  # pragma: no cover - platform dependent
        logger.debug("Signal %s not installable on this platform", sig)


def worker_main(sim_id: str, sim_dir: str, concurrency: int | None = None) -> None:
    """Process entry point. Must be importable by name for ``spawn``."""
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{sim_id}] %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(run_simulation(sim_id, sim_dir, concurrency=concurrency))
    except Exception:  # noqa: BLE001 - the exit code is what the manager reads
        logger.exception("Worker for %s exited with an error", sim_id)
        raise SystemExit(1)
