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
    from app.services.simulation_ipc import ControlServer, socket_path
    from app.services.simulation_runner import SimulationRunner
    from app.services.simulation_config_generator import SimulationConfig
    from app.services.simulation_store import SimulationStore

    sim_dir = Path(sim_dir)
    config = get_config()
    concurrency = concurrency or int(os.environ.get(CONCURRENCY_ENV, 0)) or None

    store = SimulationStore(sim_dir.parent)
    sim_config = SimulationConfig.load(sim_dir / "config.json")
    state = WorkerState(sim_id, sim_config.rounds)

    runner = SimulationRunner(
        sim_config,
        sim_dir / "profiles",
        sim_dir / "simulation.db",
        config=config,
        concurrency=concurrency,
    )

    server = ControlServer(socket_path(sim_dir))

    async def on_ping(_request: Any) -> dict[str, Any]:
        return {"sim_id": sim_id, "pid": os.getpid()}

    async def on_status(_request: Any) -> dict[str, Any]:
        return state.snapshot()

    async def on_stop(_request: Any) -> dict[str, Any]:
        # Graceful: the loop checks between rounds. Killing mid-round would
        # abandon a partly-applied round with no record of how far it got.
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
        state.stage = "preparing"
        await runner.setup()

        state.stage = "seeding"
        state.rounds.append((await runner.seed()).to_dict())

        state.stage = "running"
        for index in range(1, sim_config.rounds + 1):
            if state.stop_requested:
                state.stage = "stopped"
                logger.info("Stopping %s before round %d", sim_id, index)
                break
            state.round = index
            summary = await runner.run_round(index)
            state.rounds.append(summary.to_dict())
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
        await runner.close()
        await server.close()

    return state.snapshot()


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
