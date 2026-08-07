"""Phase 7 Step 4 — environment health.

Two endpoints that look trivial and are not, because the interesting state is
neither "running" nor "gone".

**A wedged worker is the case worth reporting.** A process that is alive but not
answering its socket is invisible to a check that only asks whether the process
exists, and indistinguishable from a healthy one to a check that only asks
whether the run is recorded as running. It gets its own answer, found with a
deliberately short probe — a health check that takes five seconds to report a
problem is a poor health check.

**`close-env` differs from `stop` by verifying.** `stop` returns as soon as the
process is gone. Before archiving or deleting a run the question is whether
anything was left behind: a socket file nobody is listening on, or a database
still held open. Both are the normal residue of an escalated kill, and both bite
later rather than now.

The wedged case is exercised against a real spawned process that never listens,
because it cannot be faked convincingly — the whole point is that the operating
system says the process is fine.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from app.services.simulation_config_generator import SimulationConfig
from app.services.simulation_ipc import socket_path
from app.services.simulation_manager import (
    PROBE_TIMEOUT,
    SimulationManager,
    WorkerRecord,
    process_start_time,
)
from app.services.simulation_store import (
    SimulationNotFound,
    SimulationState,
    SimulationStore,
)
from app.services.tasks import TaskStore

SCENARIO = {
    "graph_id": "g-1",
    "event": "The council published a draft housing density policy.",
    "rounds": 2,
    "broadcaster": {"name": "Riverbend Wire"},
    "seed_posts": [{"content": "Council publishes draft density policy."}],
}


# Spawn re-imports this module, so the targets must live at module level.


def _answering_worker(sim_dir: str, marker: str) -> None:
    """A process that holds a control socket and answers it."""
    from app.services.simulation_ipc import ControlServer, socket_path

    async def main() -> None:
        server = ControlServer(socket_path(sim_dir))
        stop = asyncio.Event()

        async def on_ping(_request):
            return {"pid": os.getpid()}

        async def on_stop(_request):
            stop.set()
            return {"accepted": True}

        server.handle("ping", on_ping)
        server.handle("stop", on_stop)
        await server.start()
        Path(marker).write_text("up")
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        await server.close()

    asyncio.run(main())


def _wedged_worker(marker: str) -> None:
    """Alive, and never listens. The case that hides from a naive check."""
    Path(marker).write_text("up")
    time.sleep(120)


@pytest.fixture
def short_tmp():
    """The control socket is limited to 107 characters."""
    directory = Path(tempfile.mkdtemp(prefix="cs-", dir="/tmp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def store(short_tmp):
    return SimulationStore(short_tmp / "sims")


@pytest.fixture
def manager(store, config):
    return SimulationManager(store, config=config)


@pytest.fixture
def sim(store):
    meta = store.create(SimulationConfig.model_validate(SCENARIO))
    directory = store.profiles_dir(meta.sim_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profiles.json").write_text("[]")
    return meta


def spawn(target, *args):
    process = multiprocessing.get_context("spawn").Process(target=target, args=args)
    process.start()
    return process


def wait_for(path: Path, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------
# env-status
# --------------------------------------------------------------------------


def test_a_simulation_that_never_ran_is_closed(manager, sim):
    status = manager.env_status(sim.sim_id)
    assert status["status"] == "closed"
    assert status["accepting_commands"] is False
    assert status["process_alive"] is False


def test_a_live_environment_reports_running(manager, store, sim, short_tmp):
    marker = short_tmp / "up.txt"
    process = spawn(_answering_worker, str(store.sim_dir(sim.sim_id)), str(marker))
    try:
        assert wait_for(marker), "the stand-in worker never started"
        manager._save_record(WorkerRecord(
            sim_id=sim.sim_id, pid=process.pid,
            start_time=process_start_time(process.pid)))
        store.mark_started(sim.sim_id)

        status = manager.env_status(sim.sim_id)
        assert status["status"] == "running"
        assert status["accepting_commands"] is True
        assert status["process_alive"] is True
        assert status["worker_pid"] == process.pid
        assert status["probe_seconds"] < PROBE_TIMEOUT
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=10)


def test_A_WEDGED_WORKER_IS_REPORTED_AS_UNRESPONSIVE(manager, store, sim, short_tmp):
    """Alive to the operating system, useless to us. Neither running nor gone."""
    marker = short_tmp / "wedged.txt"
    process = spawn(_wedged_worker, str(marker))
    try:
        assert wait_for(marker)
        manager._save_record(WorkerRecord(
            sim_id=sim.sim_id, pid=process.pid,
            start_time=process_start_time(process.pid)))
        store.mark_started(sim.sim_id)

        started = time.monotonic()
        status = manager.env_status(sim.sim_id)
        elapsed = time.monotonic() - started

        assert status["status"] == "unresponsive"
        assert status["accepting_commands"] is False
        assert status["process_alive"] is True, "the process really is there"
        assert str(process.pid) in status["detail"]
        assert elapsed < PROBE_TIMEOUT + 2, "the probe must not wait long"
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=10)


def test_the_probe_timeout_is_short_enough_to_poll():
    assert PROBE_TIMEOUT <= 3.0


def test_a_dead_process_reports_closed_not_unresponsive(manager, store, sim):
    manager._save_record(WorkerRecord(sim_id=sim.sim_id, pid=999_998, start_time=1))
    store.mark_started(sim.sim_id)

    status = manager.env_status(sim.sim_id)
    assert status["status"] == "closed"


def test_env_status_reports_the_recorded_state(manager, store, sim):
    store.mark_started(sim.sim_id)
    store.mark_finished(sim.sim_id)
    assert manager.env_status(sim.sim_id)["state"] == SimulationState.COMPLETE


def test_env_status_on_an_unknown_simulation(manager):
    with pytest.raises(SimulationNotFound):
        manager.env_status("sim-20260101-000000-abcdef")


# --------------------------------------------------------------------------
# close-env
# --------------------------------------------------------------------------


def test_closing_something_that_never_ran_is_already_closed(manager, sim):
    result = manager.close_env(sim.sim_id)
    assert result["closed"] is True
    assert result["outcome"] == "not running"
    assert result["leftovers"] == []


def test_closing_a_live_environment_releases_everything(manager, store, sim, short_tmp):
    marker = short_tmp / "up.txt"
    process = spawn(_answering_worker, str(store.sim_dir(sim.sim_id)), str(marker))
    try:
        assert wait_for(marker)
        manager._save_record(WorkerRecord(
            sim_id=sim.sim_id, pid=process.pid,
            start_time=process_start_time(process.pid)))
        store.mark_started(sim.sim_id)

        result = manager.close_env(sim.sim_id, timeout=20)

        assert result["closed"] is True
        assert result["outcome"] == "stopped"
        assert result["released"] == {"process": True, "socket": True,
                                      "database": True}
        assert result["was"] == SimulationState.RUNNING
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=10)


def test_A_KILLED_WORKERS_SOCKET_IS_CLEANED_UP(manager, store, sim):
    """An escalated kill never reaches the worker's own cleanup."""
    path = socket_path(store.sim_dir(sim.sim_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")           # the residue a SIGKILL leaves behind
    store.mark_started(sim.sim_id)

    result = manager.close_env(sim.sim_id)

    assert not path.exists(), "a stale socket would trip up a later run"
    assert result["released"]["socket"] is True
    assert result["closed"] is True


def test_closing_reports_the_database_as_released(manager, store, sim):
    database = store.sim_dir(sim.sim_id) / "simulation.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE t (x INTEGER)")

    result = manager.close_env(sim.sim_id)
    assert result["released"]["database"] is True


def test_A_LOCKED_DATABASE_IS_REPORTED_NOT_HIDDEN(manager, store, sim):
    """The difference between "the process exited" and "the environment closed"."""
    database = store.sim_dir(sim.sim_id) / "simulation.db"
    holder = sqlite3.connect(database, isolation_level="EXCLUSIVE")
    try:
        holder.execute("CREATE TABLE t (x INTEGER)")
        holder.execute("BEGIN EXCLUSIVE")

        result = manager.close_env(sim.sim_id)
        assert result["released"]["database"] is False
        assert result["closed"] is False
        assert any("locked" in item for item in result["leftovers"])
    finally:
        holder.rollback()
        holder.close()


def test_closing_a_finished_run_is_harmless(manager, store, sim):
    store.mark_started(sim.sim_id)
    store.mark_finished(sim.sim_id)

    result = manager.close_env(sim.sim_id)
    assert result["closed"] is True
    assert result["state"] == SimulationState.COMPLETE


def test_closing_is_idempotent(manager, sim):
    first = manager.close_env(sim.sim_id)
    second = manager.close_env(sim.sim_id)
    assert first["closed"] and second["closed"]


def test_close_env_on_an_unknown_simulation(manager):
    with pytest.raises(SimulationNotFound):
        manager.close_env("sim-20260101-000000-abcdef")


# ==========================================================================
# Over HTTP
# ==========================================================================


class StubManager:
    def __init__(self, store):
        self.store = store
        self.env_calls: list[tuple[str, dict]] = []
        self.close_calls: list[tuple[str, dict]] = []
        self.env_result: dict[str, Any] = {
            "sim_id": "s", "status": "closed", "accepting_commands": False,
            "process_alive": False, "state": "draft"}
        self.close_result: dict[str, Any] = {
            "sim_id": "s", "closed": True, "outcome": "not running",
            "released": {"process": True, "socket": True, "database": True},
            "leftovers": [], "state": "draft", "was": "draft"}

    def env_status(self, sim_id, **kwargs):
        self.env_calls.append((sim_id, kwargs))
        return self.env_result

    def close_env(self, sim_id, **kwargs):
        self.close_calls.append((sim_id, kwargs))
        return self.close_result

    def is_running(self, sim_id):
        return False

    def running(self):
        return []


class StubRuntime:
    def __init__(self, tmp_path, config, store):
        self.config = config
        self.tasks = TaskStore(tmp_path / "tasks.db")
        self.sims = store
        self.manager = StubManager(store)
        self.runner = self

    def submit(self, task, job):
        return task

    def run(self, coro, timeout=60.0):
        return asyncio.run(coro)


@pytest.fixture
def client(tmp_path, config, monkeypatch, store, sim):
    from app.main import create_app

    runtime = StubRuntime(tmp_path, config, store)
    monkeypatch.setattr("app.api.simulation.get_runtime", lambda **_: runtime)
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        test_client.runtime = runtime  # type: ignore[attr-defined]
        test_client.sim_id = sim.sim_id  # type: ignore[attr-defined]
        yield test_client


def test_env_status_over_http(client):
    response = client.post("/api/simulation/env-status",
                           json={"sim_id": client.sim_id})
    assert response.status_code == 200
    assert response.get_json()["status"] == "closed"


def test_env_status_passes_a_timeout_through(client):
    client.post("/api/simulation/env-status",
                json={"sim_id": client.sim_id, "timeout": 0.5})
    assert client.runtime.manager.env_calls[0][1] == {"timeout": 0.5}


@pytest.mark.parametrize("timeout", ["soon", 0, -1])
def test_a_nonsense_timeout_is_refused(client, timeout):
    response = client.post("/api/simulation/env-status",
                           json={"sim_id": client.sim_id, "timeout": timeout})
    assert response.status_code == 400


def test_close_env_over_http(client):
    response = client.post("/api/simulation/close-env",
                           json={"sim_id": client.sim_id})
    assert response.status_code == 200
    assert response.get_json()["closed"] is True


def test_AN_INCOMPLETE_CLOSE_IS_NOT_A_PLAIN_200(client):
    """A caller about to archive the run needs to know something survived."""
    client.runtime.manager.close_result = {
        **client.runtime.manager.close_result,
        "closed": False, "leftovers": ["process 42 is still running"],
        "released": {"process": False, "socket": True, "database": True}}

    response = client.post("/api/simulation/close-env",
                           json={"sim_id": client.sim_id})
    assert response.status_code == 207
    assert response.get_json()["leftovers"]


def test_close_env_passes_a_timeout_through(client):
    client.post("/api/simulation/close-env",
                json={"sim_id": client.sim_id, "timeout": 45})
    assert client.runtime.manager.close_calls[0][1] == {"timeout": 45.0}


@pytest.mark.parametrize("path", ["/api/simulation/env-status",
                                  "/api/simulation/close-env"])
def test_both_endpoints_need_a_simulation(client, path):
    assert client.post(path, json={}).status_code == 404


@pytest.mark.parametrize("path", ["/api/simulation/env-status",
                                  "/api/simulation/close-env"])
def test_an_unknown_simulation_is_a_404(client, path):
    response = client.post(path, json={"sim_id": "sim-20260101-000000-999999"})
    assert response.status_code == 404
