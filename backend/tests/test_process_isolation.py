"""Phase 6 Step 2 — process isolation, IPC, and the concurrency budget.

Named in the spec under Step 6. The properties worth testing are the ones that
only show up against real processes, so most of these spawn one.

Two are load-bearing:

* **The budget arithmetic.** Phase 2's gate is per process. Three runs each
  configured for the full budget put three times the requests in flight against
  one GPU, which is the exhaustion the bound exists to prevent.
* **PID identity.** A recorded PID is not proof of identity; the kernel reuses
  them. Reaping orphans means killing processes, and killing the wrong one
  because a number was recycled would be a serious bug.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from app.services.simulation_config_generator import SimulationConfig
from app.services.simulation_ipc import (
    ControlClient,
    ControlServer,
    IPCError,
    socket_path,
)
from app.services.simulation_manager import (
    CapacityError,
    SimulationManager,
    WorkerRecord,
    process_start_time,
    process_status,
    worker_share,
)
from app.services.simulation_store import SimulationState, SimulationStore

SCENARIO = {
    "graph_id": "g-1",
    "event": "The council published a draft housing density policy.",
    "rounds": 2,
    "broadcaster": {"name": "Riverbend Wire"},
    "seed_posts": [{"content": "Council publishes draft density policy."}],
}


# Spawned targets must be importable by name: `spawn` re-imports this module.


def _responsive_worker(sim_dir: str, marker: str) -> None:
    """A real process holding a real control socket, without a real simulation."""
    from app.services.simulation_ipc import ControlServer, socket_path

    async def main() -> None:
        server = ControlServer(socket_path(sim_dir))
        stop = asyncio.Event()

        async def on_ping(_request):
            return {"pid": os.getpid()}

        async def on_status(_request):
            return {"stage": "running", "round": 1}

        async def on_stop(_request):
            stop.set()
            return {"accepted": True}

        server.handle("ping", on_ping)
        server.handle("status", on_status)
        server.handle("stop", on_stop)
        await server.start()
        Path(marker).write_text("up")
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        await server.close()

    asyncio.run(main())


def _deaf_worker() -> None:
    """Alive, but never listens. The wedged case."""
    time.sleep(60)


def _immediate_exit() -> None:
    return None


@pytest.fixture
def short_tmp():
    """A shallow directory.

    Unix socket paths are limited to 107 characters, and pytest's `tmp_path`
    (`/tmp/pytest-of-<user>/pytest-N/<long test name>0/`) alone can exceed that
    before the simulation id is appended.
    """
    directory = Path(tempfile.mkdtemp(prefix="cs-", dir="/tmp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def store(short_tmp):
    return SimulationStore(short_tmp / "sims")


@pytest.fixture
def manager(store, config):
    return SimulationManager(store, config=config.model_copy(update={
        "LLM_CONCURRENCY": 4, "API_LLM_RESERVE": 1, "MAX_CONCURRENT_SIMULATIONS": 2,
    }))


@pytest.fixture
def sim(store):
    return store.create(SimulationConfig.model_validate(SCENARIO))


@pytest.fixture
def spawned(store, sim):
    """A running stand-in worker, cleaned up however the test ends."""
    context = multiprocessing.get_context("spawn")
    sim_dir = store.sim_dir(sim.sim_id)
    marker = sim_dir / "up.txt"
    process = context.Process(target=_responsive_worker,
                              args=(str(sim_dir), str(marker)))
    process.start()
    for _ in range(300):
        if marker.exists():
            break
        time.sleep(0.05)
    yield process
    if process.is_alive():
        process.terminate()
    process.join(timeout=10)


# --------------------------------------------------------------------------
# The budget
# --------------------------------------------------------------------------


def test_the_budget_is_divided_not_handed_out_whole(config):
    settings = config.model_copy(update={
        "LLM_CONCURRENCY": 4, "API_LLM_RESERVE": 1, "MAX_CONCURRENT_SIMULATIONS": 2})
    assert worker_share(settings) == 1


def test_THE_SPECS_OVERSUBSCRIPTION_CANNOT_HAPPEN(config):
    """Three runs at the full budget each would be 12 in flight against one GPU."""
    settings = config.model_copy(update={
        "LLM_CONCURRENCY": 4, "API_LLM_RESERVE": 1, "MAX_CONCURRENT_SIMULATIONS": 3})
    worst_case = worker_share(settings) * 3 + settings.API_LLM_RESERVE
    assert worst_case <= settings.LLM_CONCURRENCY


def test_the_api_keeps_its_reserve(config):
    """Without it the UI looks dead exactly while a run is worth watching."""
    settings = config.model_copy(update={
        "LLM_CONCURRENCY": 8, "API_LLM_RESERVE": 2, "MAX_CONCURRENT_SIMULATIONS": 2})
    assert worker_share(settings) == 3
    assert worker_share(settings) * 2 + 2 == 8


def test_a_worker_always_gets_at_least_one_request(config):
    """Zero would deadlock the run rather than merely slow it."""
    settings = config.model_copy(update={
        "LLM_CONCURRENCY": 1, "API_LLM_RESERVE": 1, "MAX_CONCURRENT_SIMULATIONS": 4})
    assert worker_share(settings) == 1


def test_the_arithmetic_is_inspectable(manager):
    budget = manager.budget()
    assert budget["per_worker"] == 1
    assert budget["capacity"] == 2
    assert set(budget) >= {"llm_concurrency", "api_reserve", "in_flight_worst_case"}


# --------------------------------------------------------------------------
# Process identity
# --------------------------------------------------------------------------


def test_a_live_process_reports_a_start_time():
    assert process_start_time(os.getpid()) is not None


def test_a_nonexistent_process_reports_nothing():
    assert process_start_time(999_999) is None


def test_a_record_for_this_process_is_alive():
    record = WorkerRecord(sim_id="s", pid=os.getpid(),
                          start_time=process_start_time(os.getpid()))
    assert record.alive()


def test_A_REUSED_PID_IS_NEVER_MISTAKEN_FOR_OURS():
    """Reaping kills processes. Killing the wrong one would be serious."""
    record = WorkerRecord(sim_id="s", pid=os.getpid(),
                          start_time=(process_start_time(os.getpid()) or 0) + 99)
    assert not record.alive()


def test_a_zombie_counts_as_dead():
    """It has exited but keeps a /proc entry until reaped."""
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_immediate_exit)
    process.start()
    pid = process.pid
    record = WorkerRecord(sim_id="s", pid=pid, start_time=process_start_time(pid))

    for _ in range(200):
        status = process_status(pid)
        if status is None or status[0] == "Z":
            break
        time.sleep(0.05)

    assert not record.alive(), "a zombie is not a running simulation"
    process.join(timeout=5)


def test_a_record_without_a_pid_is_dead():
    assert not WorkerRecord(sim_id="s", pid=0).alive()


# --------------------------------------------------------------------------
# IPC
# --------------------------------------------------------------------------


async def test_the_control_plane_carries_commands(short_tmp):
    path = socket_path(short_tmp)
    server = ControlServer(path)
    seen: list[str] = []

    async def on_ping(_request):
        return {"pong": True}

    async def on_stop(request):
        seen.append(request.command)
        return {"accepted": True}

    async def on_boom(_request):
        raise ValueError("handler exploded")

    server.handle("ping", on_ping)
    server.handle("stop", on_stop)
    server.handle("boom", on_boom)
    await server.start()

    client = ControlClient(path, timeout=5.0)
    try:
        # The client blocks and the server shares this loop, so it must run
        # off-loop here. In production they are separate processes.
        assert await asyncio.to_thread(client.ping)
        assert (await asyncio.to_thread(client.request, "stop"))["accepted"]
        assert seen == ["stop"]

        with pytest.raises(IPCError, match="Unknown command"):
            await asyncio.to_thread(client.request, "nonsense")

        with pytest.raises(IPCError, match="handler exploded"):
            await asyncio.to_thread(client.request, "boom")

        assert await asyncio.to_thread(client.ping), "a bad command did not kill it"
    finally:
        await server.close()

    assert not path.exists()
    assert not await asyncio.to_thread(client.ping)


def test_an_absent_worker_is_unreachable_not_a_hang(short_tmp):
    client = ControlClient(socket_path(short_tmp), timeout=1.0)
    started = time.monotonic()
    assert not client.ping()
    assert time.monotonic() - started < 1.0


async def test_a_stale_socket_file_does_not_block_a_restart(short_tmp):
    """A killed worker leaves one behind; bind() would fail on it."""
    path = socket_path(short_tmp)
    path.write_bytes(b"")

    async def on_ping(_request):
        return {"pong": True}

    server = ControlServer(path)
    server.handle("ping", on_ping)
    await server.start()
    try:
        assert await asyncio.to_thread(ControlClient(path).ping)
    finally:
        await server.close()


async def test_a_live_socket_is_never_clobbered(short_tmp):
    """Otherwise starting a run twice would orphan the first worker."""
    path = socket_path(short_tmp)

    async def on_ping(_request):
        return {"pong": True}

    first = ControlServer(path)
    first.handle("ping", on_ping)
    await first.start()
    try:
        with pytest.raises(IPCError, match="already listening"):
            await ControlServer(path).start()
    finally:
        await first.close()


def test_an_over_long_socket_path_is_refused_clearly(tmp_path):
    """The kernel's sun_path limit fails at bind() with a confusing error."""
    from app.services.simulation_ipc import IPCError as Err

    deep = tmp_path / ("x" * 90) / ("y" * 90)
    with pytest.raises(Err, match="too long"):
        socket_path(deep)


# --------------------------------------------------------------------------
# Isolation: the API survives whatever happens to a worker
# --------------------------------------------------------------------------


def test_a_spawned_worker_is_visible_and_reachable(manager, store, sim, spawned):
    manager._save_record(WorkerRecord(
        sim_id=sim.sim_id, pid=spawned.pid,
        start_time=process_start_time(spawned.pid)))
    store.mark_started(sim.sim_id)

    assert manager.is_running(sim.sim_id)
    assert manager.status(sim.sim_id)["stage"] == "running"
    assert manager.capacity() == 1


def test_KILLING_A_SIMULATION_DOES_NOT_AFFECT_THE_API(manager, store, sim, spawned):
    """The whole reason runs are processes rather than threads."""
    api_pid = os.getpid()
    manager._save_record(WorkerRecord(
        sim_id=sim.sim_id, pid=spawned.pid,
        start_time=process_start_time(spawned.pid)))
    store.mark_started(sim.sim_id)

    spawned.kill()
    spawned.join(timeout=10)

    assert os.getpid() == api_pid
    assert manager.budget()["capacity"] == 2, "the API is still serving"
    assert not manager.is_running(sim.sim_id)


def test_a_dead_worker_reconciles_the_stored_state(manager, store, sim, spawned):
    manager._save_record(WorkerRecord(
        sim_id=sim.sim_id, pid=spawned.pid,
        start_time=process_start_time(spawned.pid)))
    store.mark_started(sim.sim_id)

    spawned.kill()
    spawned.join(timeout=10)
    manager.running()

    assert store.load_meta(sim.sim_id).state == SimulationState.FAILED


def test_a_graceful_stop_lets_the_worker_finish(manager, store, sim, spawned):
    manager._save_record(WorkerRecord(
        sim_id=sim.sim_id, pid=spawned.pid,
        start_time=process_start_time(spawned.pid)))
    store.mark_started(sim.sim_id)

    assert manager.stop(sim.sim_id, timeout=20) == "stopped"
    spawned.join(timeout=10)
    assert not spawned.is_alive()
    assert store.load_meta(sim.sim_id).state == SimulationState.COMPLETE


def test_stopping_something_not_running_is_not_an_error(manager, sim):
    assert manager.stop(sim.sim_id) == "not running"


# --------------------------------------------------------------------------
# Orphans, as after an API restart
# --------------------------------------------------------------------------


def test_A_LIVE_ORPHAN_IS_ADOPTED_NOT_KILLED(manager, store, sim, spawned, config):
    """A healthy run must survive an API restart; it may be hours in."""
    manager._save_record(WorkerRecord(
        sim_id=sim.sim_id, pid=spawned.pid,
        start_time=process_start_time(spawned.pid)))
    store.mark_started(sim.sim_id)

    restarted = SimulationManager(store, config=manager.config)
    assert restarted.reap_orphans() == {sim.sim_id: "adopted"}
    assert spawned.is_alive()
    assert store.load_meta(sim.sim_id).state == SimulationState.RUNNING
    assert restarted.status(sim.sim_id)["stage"] == "running"


def test_an_adopted_orphan_can_still_be_stopped(manager, store, sim, spawned):
    manager._save_record(WorkerRecord(
        sim_id=sim.sim_id, pid=spawned.pid,
        start_time=process_start_time(spawned.pid)))
    store.mark_started(sim.sim_id)

    restarted = SimulationManager(store, config=manager.config)
    restarted.reap_orphans()
    assert restarted.stop(sim.sim_id, timeout=20) == "stopped"


def test_a_wedged_orphan_is_killed_rather_than_left_holding_the_gpu(manager, store, sim):
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_deaf_worker)
    process.start()
    try:
        manager._save_record(WorkerRecord(
            sim_id=sim.sim_id, pid=process.pid,
            start_time=process_start_time(process.pid)))
        store.mark_started(sim.sim_id)

        restarted = SimulationManager(store, config=manager.config)
        assert restarted.reap_orphans() == {sim.sim_id: "killed"}
        process.join(timeout=10)
        assert not process.is_alive()
        assert store.load_meta(sim.sim_id).state == SimulationState.FAILED
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=5)


def test_a_vanished_worker_is_marked_failed_for_resume(manager, store, sim):
    manager._save_record(WorkerRecord(sim_id=sim.sim_id, pid=999_998, start_time=1))
    store.mark_started(sim.sim_id)

    restarted = SimulationManager(store, config=manager.config)
    assert restarted.reap_orphans() == {sim.sim_id: "failed"}
    assert store.load_meta(sim.sim_id).state == SimulationState.FAILED


def test_reaping_leaves_drafts_and_finished_runs_alone(manager, store):
    draft = store.create(SimulationConfig.model_validate(SCENARIO))
    done = store.create(SimulationConfig.model_validate(SCENARIO))
    store.mark_started(done.sim_id)
    store.mark_finished(done.sim_id)

    assert manager.reap_orphans() == {}
    assert store.load_meta(draft.sim_id).state == SimulationState.DRAFT
    assert store.load_meta(done.sim_id).state == SimulationState.COMPLETE


def test_reaping_an_empty_store_is_quiet(manager):
    assert manager.reap_orphans() == {}


# --------------------------------------------------------------------------
# Starting
# --------------------------------------------------------------------------


def test_an_unknown_simulation_cannot_be_started(manager):
    from app.services.simulation_store import SimulationNotFound

    with pytest.raises(SimulationNotFound):
        manager.start("sim-20260101-000000-abcdef")


def test_a_started_simulation_cannot_be_started_again(manager, store, sim):
    store.mark_started(sim.sim_id)
    with pytest.raises(CapacityError, match="only a draft"):
        manager.start(sim.sim_id)


def test_capacity_is_enforced(manager, store, sim, spawned):
    manager._save_record(WorkerRecord(
        sim_id=sim.sim_id, pid=spawned.pid,
        start_time=process_start_time(spawned.pid)))
    store.mark_started(sim.sim_id)
    manager.config = manager.config.model_copy(
        update={"MAX_CONCURRENT_SIMULATIONS": 1})

    another = store.create(SimulationConfig.model_validate(SCENARIO))
    with pytest.raises(CapacityError, match="maximum"):
        manager.start(another.sim_id)
