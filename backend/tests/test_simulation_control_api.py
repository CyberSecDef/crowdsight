"""Phase 6 Step 5 — the simulation control API.

Route shapes and status codes against a stub runtime, in the style of
`test_graph_api.py`. Three things here are worth more than the happy path:

* **Ordering.** `/list`, `/budget` and `/prepare/status` sit under the same
  prefix as `/<sim_id>`, so a routing mistake makes them 404 as unknown
  simulation ids rather than fail loudly.
* **State guards.** Starting a simulation with no scenario, or no population,
  must be a clear 409 and not a worker that dies on a missing file minutes
  later.
* **Resume.** A failed run is restartable, and the response has to say that it
  resumed rather than started, because the two mean different things about
  what the resulting data covers.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.simulation_config_generator import SimulationConfig
from app.services.simulation_manager import CapacityError
from app.services.simulation_store import SimulationState, SimulationStore
from app.services.tasks import TaskStore
from app.storage.graph_storage import Page

SCENARIO = {
    "graph_id": "g-1",
    "event": "The council published a draft housing density policy.",
    "rounds": 3,
    "broadcaster": {"name": "Riverbend Wire"},
    "seed_posts": [{"content": "Council publishes draft density policy."}],
}

PROFILES = [
    {"user_id": 0, "username": "dawn_mercer", "name": "Dawn Mercer",
     "provenance": "synthetic", "activity_level": "high"},
    {"user_id": 1, "username": "jane_doe", "name": "Jane Doe",
     "provenance": "named", "activity_level": "moderate"},
]


class StubGraphs:
    async def get_graph(self, graph_id):
        return {"graph_id": graph_id} if graph_id == "g-1" else None

    async def list_entities(self, graph_id, *, types=None, search=None,
                            limit=50, offset=0):
        items = [{"uuid": "e-1", "name": "Councillor Jane Doe", "type": "Person",
                  "attributes": {}}]
        return Page(items=items, total=1, limit=limit, offset=offset)


class StubBuilder:
    def load_document(self, graph_id):
        return "Riverbend Council published a draft density policy."

    def ontology_path(self, graph_id):
        from pathlib import Path

        return Path("/nonexistent/ontology.json")


class StubManager:
    def __init__(self, store):
        self.store = store
        self.started: list[str] = []
        self.stopped: list[str] = []
        self._running: set[str] = set()
        self.capacity_error: str | None = None

    def is_running(self, sim_id):
        return sim_id in self._running

    def running(self):
        return sorted(self._running)

    def start(self, sim_id):
        if self.capacity_error:
            raise CapacityError(self.capacity_error)
        from app.services.simulation_manager import WorkerRecord

        self.started.append(sim_id)
        self._running.add(sim_id)
        self.store.mark_started(sim_id)
        return WorkerRecord(sim_id=sim_id, pid=4242, concurrency=1)

    def stop(self, sim_id, **kwargs):
        self.stopped.append(sim_id)
        if sim_id not in self._running:
            return "not running"
        self._running.discard(sim_id)
        self.store.mark_finished(sim_id)
        return "stopped"

    def status(self, sim_id):
        return {"sim_id": sim_id, "running": sim_id in self._running,
                "stage": "running" if sim_id in self._running else "idle"}

    def budget(self):
        return {"llm_concurrency": 4, "api_reserve": 1, "per_worker": 1,
                "capacity": 2, "running": len(self._running)}


class StubRuntime:
    def __init__(self, tmp_path, config):
        self.config = config
        self.tasks = TaskStore(tmp_path / "tasks.db")
        self.sims = SimulationStore(tmp_path / "simulations")
        self.manager = StubManager(self.sims)
        self.graphs = StubGraphs()
        self.builder = StubBuilder()
        self.llm = None
        self.submitted: list[Any] = []
        self.runner = self

    def submit(self, task, job):
        self.submitted.append((task, job))
        return task

    def run(self, coro, timeout=60.0):
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        # The real runtime submits to its own loop and blocks. Called from
        # inside that loop -- which is where every background job runs -- it
        # deadlocks until the timeout fires. A stub that quietly worked here
        # let exactly that bug reach a live run.
        coro.close()
        raise AssertionError(
            "runtime.run() was called from inside the event loop; a background "
            "job must await directly")


@pytest.fixture
def client(tmp_path, config, monkeypatch):
    from app.main import create_app

    runtime = StubRuntime(tmp_path, config)
    monkeypatch.setattr("app.api.simulation.get_runtime", lambda **_: runtime)
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        test_client.runtime = runtime  # type: ignore[attr-defined]
        yield test_client


@pytest.fixture
def created(client):
    response = client.post("/api/simulation/create", json={"graph_id": "g-1"})
    return response.get_json()["sim_id"]


def prepare_fully(client, sim_id, *, config=None):
    """Give a simulation everything `start` requires."""
    runtime = client.runtime
    runtime.sims.save_config(sim_id, SimulationConfig.model_validate(
        config or SCENARIO))
    directory = runtime.sims.profiles_dir(sim_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profiles.json").write_text(json.dumps(PROFILES))
    return sim_id


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/simulation/list", "/api/simulation/budget"])
def test_static_routes_are_not_shadowed_by_the_id_route(client, path):
    """`/<sim_id>` would otherwise swallow these and 404 them."""
    assert client.get(path).status_code == 200


def test_prepare_status_is_not_read_as_a_simulation_id(client):
    response = client.get("/api/simulation/prepare/status")
    assert response.status_code == 400
    assert "task_id" in response.get_json()["error"]


def test_every_endpoint_the_spec_names_exists(client):
    from app.main import create_app

    rules = {str(rule) for rule in create_app().url_map.iter_rules()}
    for path in ("/api/simulation/create", "/api/simulation/prepare",
                 "/api/simulation/prepare/status", "/api/simulation/<sim_id>/config",
                 "/api/simulation/<sim_id>/profiles", "/api/simulation/start",
                 "/api/simulation/stop", "/api/simulation/list"):
        assert path in rules, f"{path} is missing"


async def test_A_BACKGROUND_JOB_NEVER_BLOCKS_ON_THE_RUNTIME_LOOP(client):
    """The deadlock a live run found, and the stubs previously hid.

    A job already runs on the runner's event loop. Calling the synchronous
    `runtime.run` facade from inside one submits to the loop that is waiting
    for it, and the job dies on the 60-second timeout. The stub raises here,
    so the async path has to be genuinely async.
    """
    from app.api.simulation import graph_context

    document, names = await graph_context("g-1")
    assert document
    assert names == ["Councillor Jane Doe"]


def test_the_blocking_wrapper_still_works_for_handlers(client):
    """Request handlers have no loop of their own and must keep working."""
    from app.api.simulation import _graph_context

    document, names = _graph_context("g-1")
    assert document and names


def test_the_phase_5_routes_still_work(client, created):
    """The edit-and-fork flow has no equivalent in the spec's list."""
    prepare_fully(client, created)
    assert client.get("/api/simulations").status_code == 200
    assert client.get(f"/api/simulations/{created}/config").status_code == 200


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


def test_create_reserves_a_simulation_without_doing_any_work(client):
    response = client.post("/api/simulation/create", json={"graph_id": "g-1"})
    body = response.get_json()

    assert response.status_code == 201
    assert body["sim_id"].startswith("sim-")
    assert body["prepared"] is False
    assert client.runtime.submitted == [], "creating must not start a job"


def test_create_needs_a_graph(client):
    assert client.post("/api/simulation/create", json={}).status_code == 400


def test_create_rejects_an_unknown_graph_with_404(client):
    response = client.post("/api/simulation/create", json={"graph_id": "nope"})
    assert response.status_code == 404


@pytest.mark.parametrize("payload", [
    {"graph_id": "g-1", "platform": "mastodon"},
    {"graph_id": "g-1", "rounds": 0},
    {"graph_id": "g-1", "rounds": "lots"},
    {"graph_id": "g-1", "total_agents": -5},
    {"graph_id": "g-1", "named_ratio": 1.5},
    {"graph_id": "g-1", "named_ratio": "most"},
])
def test_nonsense_parameters_are_refused(client, payload):
    assert client.post("/api/simulation/create", json=payload).status_code == 400


def test_a_population_beyond_the_cap_is_refused(client, config):
    response = client.post("/api/simulation/create", json={
        "graph_id": "g-1", "total_agents": config.MAX_AGENTS + 1})
    assert response.status_code == 400
    assert "MAX_AGENTS" in response.get_json()["error"]


def test_the_request_is_remembered_for_prepare(client):
    sim_id = client.post("/api/simulation/create", json={
        "graph_id": "g-1", "platform": "reddit", "total_agents": 12,
        "named_ratio": 0.5}).get_json()["sim_id"]

    stored = client.runtime.sims.request(sim_id)
    assert stored["platform"] == "reddit"
    assert stored["total_agents"] == 12


def test_a_reserved_simulation_describes_itself_as_unprepared(client, created):
    body = client.get(f"/api/simulation/{created}").get_json()
    assert body["prepared"] is False
    assert body["config"] is None
    assert body["summary"] == "not yet prepared"


# --------------------------------------------------------------------------
# Prepare
# --------------------------------------------------------------------------


def test_prepare_returns_a_task_to_poll(client, created):
    response = client.post("/api/simulation/prepare", json={"sim_id": created})
    body = response.get_json()

    assert response.status_code == 202
    assert body["task_id"]
    assert body["poll"].startswith("/api/simulation/prepare/status")
    assert len(client.runtime.submitted) == 1


def test_prepare_uses_what_create_recorded(client):
    sim_id = client.post("/api/simulation/create", json={
        "graph_id": "g-1", "total_agents": 7}).get_json()["sim_id"]
    body = client.post("/api/simulation/prepare", json={"sim_id": sim_id}).get_json()
    assert body["agents"] == 7


def test_prepare_needs_a_simulation(client):
    assert client.post("/api/simulation/prepare", json={}).status_code == 404


def test_preparing_an_unknown_simulation_is_a_404(client):
    response = client.post("/api/simulation/prepare",
                           json={"sim_id": "sim-20260101-000000-abcdef"})
    assert response.status_code == 404


def test_AN_ALREADY_PREPARED_SIMULATION_IS_NOT_REBUILT(client, created):
    """Regenerating a population is minutes of GPU an operator did not ask for."""
    prepare_fully(client, created)
    response = client.post("/api/simulation/prepare", json={"sim_id": created})

    assert response.status_code == 200
    assert response.get_json()["task_id"] is None
    assert client.runtime.submitted == []


def test_force_rebuilds_and_discards_the_old_population(client, created):
    prepare_fully(client, created)
    profiles = client.runtime.sims.profiles_dir(created) / "profiles.json"
    assert profiles.is_file()

    response = client.post("/api/simulation/prepare",
                           json={"sim_id": created, "force": True})
    assert response.status_code == 202
    assert not profiles.exists(), "a forced rebuild must not resume the old plan"


def test_preparing_a_running_simulation_is_refused(client, created):
    prepare_fully(client, created)
    client.runtime.manager._running.add(created)
    response = client.post("/api/simulation/prepare",
                           json={"sim_id": created, "force": True})
    assert response.status_code == 409


def test_preparing_a_finished_simulation_is_refused(client, created):
    prepare_fully(client, created)
    client.runtime.sims.mark_started(created)
    client.runtime.sims.mark_finished(created)
    response = client.post("/api/simulation/prepare",
                           json={"sim_id": created, "force": True})
    assert response.status_code == 409


def test_prepare_status_reports_a_task(client, created):
    task_id = client.post("/api/simulation/prepare",
                          json={"sim_id": created}).get_json()["task_id"]
    body = client.get(f"/api/simulation/prepare/status?task_id={task_id}").get_json()
    assert body["id"] == task_id


def test_prepare_status_on_an_unknown_task_is_a_404(client):
    assert client.get("/api/simulation/prepare/status?task_id=nope").status_code == 404


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def test_list_shows_whether_each_run_is_prepared_and_running(client, created):
    prepare_fully(client, created)
    client.runtime.manager._running.add(created)

    entry = client.get("/api/simulation/list").get_json()["simulations"][0]
    assert entry["prepared"] is True
    assert entry["running"] is True


def test_list_filters_by_graph(client, created):
    assert client.get("/api/simulation/list?graph_id=g-9").get_json()["simulations"] == []


def test_the_config_is_available_once_prepared(client, created):
    prepare_fully(client, created)
    body = client.get(f"/api/simulation/{created}/config").get_json()
    assert body["rounds"] == 3


def test_asking_for_the_config_before_preparing_is_a_409_not_a_404(client, created):
    """It exists; it is not ready. Those are different answers."""
    response = client.get(f"/api/simulation/{created}/config")
    assert response.status_code == 409


def test_the_population_is_available_once_prepared(client, created):
    prepare_fully(client, created)
    body = client.get(f"/api/simulation/{created}/profiles").get_json()
    assert body["count"] == 2
    assert {p["username"] for p in body["profiles"]} == {"dawn_mercer", "jane_doe"}


def test_the_population_can_be_filtered_by_provenance(client, created):
    prepare_fully(client, created)
    body = client.get(f"/api/simulation/{created}/profiles?provenance=named").get_json()
    assert [p["username"] for p in body["profiles"]] == ["jane_doe"]


def test_asking_for_a_population_that_does_not_exist_is_a_409(client, created):
    assert client.get(f"/api/simulation/{created}/profiles").status_code == 409


def test_live_status_comes_from_the_worker(client, created):
    prepare_fully(client, created)
    client.runtime.manager._running.add(created)
    assert client.get(f"/api/simulation/{created}/status").get_json()["stage"] == "running"


def test_the_budget_is_visible(client):
    assert client.get("/api/simulation/budget").get_json()["per_worker"] == 1


# --------------------------------------------------------------------------
# Start and stop
# --------------------------------------------------------------------------


def test_starting_a_prepared_simulation(client, created):
    prepare_fully(client, created)
    response = client.post("/api/simulation/start", json={"sim_id": created})
    body = response.get_json()

    assert response.status_code == 202
    assert body["resumed"] is False
    assert body["pid"] == 4242
    assert client.runtime.manager.started == [created]


def test_STARTING_WITHOUT_A_SCENARIO_IS_REFUSED_UP_FRONT(client, created):
    """Otherwise the worker dies on a missing file minutes later."""
    response = client.post("/api/simulation/start", json={"sim_id": created})
    assert response.status_code == 409
    assert "prepare" in response.get_json()["error"]
    assert client.runtime.manager.started == []


def test_starting_without_a_population_is_refused_up_front(client, created):
    client.runtime.sims.save_config(
        created, SimulationConfig.model_validate(SCENARIO))
    response = client.post("/api/simulation/start", json={"sim_id": created})

    assert response.status_code == 409
    assert "population" in response.get_json()["error"]


def test_A_FAILED_RUN_IS_RESUMED_AND_SAYS_SO(client, created):
    """Step 3 built the resume machinery; this is what exposes it."""
    prepare_fully(client, created)
    client.runtime.sims.mark_started(created)
    client.runtime.sims.mark_finished(created, failed=True)

    body = client.post("/api/simulation/start", json={"sim_id": created}).get_json()
    assert body["resumed"] is True
    assert client.runtime.manager.started == [created]


def test_no_capacity_is_a_409_that_explains_itself(client, created):
    prepare_fully(client, created)
    client.runtime.manager.capacity_error = "Already running 2 simulation(s)"

    response = client.post("/api/simulation/start", json={"sim_id": created})
    assert response.status_code == 409
    assert response.get_json()["budget"]["capacity"] == 2


def test_stopping_a_running_simulation(client, created):
    prepare_fully(client, created)
    client.post("/api/simulation/start", json={"sim_id": created})

    body = client.post("/api/simulation/stop", json={"sim_id": created}).get_json()
    assert body["outcome"] == "stopped"
    assert body["state"] == SimulationState.COMPLETE


def test_stopping_something_not_running_is_not_an_error(client, created):
    prepare_fully(client, created)
    body = client.post("/api/simulation/stop", json={"sim_id": created}).get_json()
    assert body["outcome"] == "not running"


def test_stopping_an_unknown_simulation_is_a_404(client):
    response = client.post("/api/simulation/stop",
                           json={"sim_id": "sim-20260101-000000-abcdef"})
    assert response.status_code == 404


def test_a_nonsense_timeout_is_refused(client, created):
    prepare_fully(client, created)
    response = client.post("/api/simulation/stop",
                           json={"sim_id": created, "timeout": "soon"})
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/api/simulation/start", "/api/simulation/stop",
                                  "/api/simulation/prepare"])
def test_every_action_needs_a_sim_id(client, path):
    assert client.post(path, json={}).status_code == 404
