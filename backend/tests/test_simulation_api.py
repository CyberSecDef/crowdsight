"""Phase 5 Step 3 — the HTTP surface for scenario review and editing.

Route shapes and status codes against a stub runtime, in the style of
`test_graph_api.py`. The behaviour worth asserting at this layer is that the
verification in the store is actually wired into the endpoint — an edit posted
over HTTP must come back corrected, with the corrections named, and an edit to a
started run must land somewhere else and say so.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.simulation_config_generator import SimulationConfig
from app.services.simulation_store import SimulationStore
from app.services.tasks import TaskStore
from app.storage.graph_storage import Page

DOC = """Riverbend City Council — Draft Housing Density Policy 2026

Councillor Jane Doe, who chairs the committee, said the proposal would permit
four-storey development along the Eastgate corridor."""

ENTITIES = [
    {"uuid": "e-1", "name": "Councillor Jane Doe", "type": "Person"},
    {"uuid": "e-2", "name": "Riverbend Gazette", "type": "Organisation"},
]

BASE = {
    "graph_id": "g-1",
    "event": "The council published a draft housing density policy.",
    "rounds": 6,
    "broadcaster": {"name": "Riverbend Wire"},
    "seed_posts": [{"content": "Council publishes draft density policy for Eastgate."}],
}


class StubGraphs:
    async def get_graph(self, graph_id):
        return {"graph_id": graph_id} if graph_id == "g-1" else None

    async def list_entities(self, graph_id, *, types=None, search=None,
                            limit=50, offset=0):
        return Page(items=[dict(e) for e in ENTITIES], total=len(ENTITIES),
                    limit=limit, offset=offset)


class StubBuilder:
    def load_document(self, graph_id):
        if graph_id != "g-1":
            raise LookupError(f"No document for {graph_id}")
        return DOC


class StubRuntime:
    def __init__(self, tmp_path, config):
        self.config = config
        self.tasks = TaskStore(tmp_path / "tasks.db")
        self.sims = SimulationStore(tmp_path / "simulations")
        self.graphs = StubGraphs()
        self.builder = StubBuilder()
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
def sim(client):
    return client.runtime.sims.create(SimulationConfig.model_validate(BASE))


# --------------------------------------------------------------------------
# Creating
# --------------------------------------------------------------------------


def test_creating_a_scenario_returns_a_task(client):
    response = client.post("/api/simulations", json={"graph_id": "g-1"})
    assert response.status_code == 202
    assert response.get_json()["task_id"]
    assert len(client.runtime.submitted) == 1


def test_creating_without_a_graph_is_a_400(client):
    assert client.post("/api/simulations", json={}).status_code == 400


def test_creating_from_an_unknown_graph_is_a_404_not_a_500(client):
    """A client cannot tell "does not exist" from "we broke" if both are 500."""
    assert client.post("/api/simulations", json={"graph_id": "nope"}).status_code == 404


def test_an_unsupported_platform_is_refused(client):
    response = client.post("/api/simulations",
                           json={"graph_id": "g-1", "platform": "mastodon"})
    assert response.status_code == 400


@pytest.mark.parametrize("rounds", ["many", 0, -1])
def test_a_nonsense_round_count_is_refused(client, rounds):
    response = client.post("/api/simulations", json={"graph_id": "g-1", "rounds": rounds})
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def test_listing_is_empty_before_anything_is_made(client):
    assert client.get("/api/simulations").get_json() == {"simulations": []}


def test_a_simulation_can_be_read_back(client, sim):
    body = client.get(f"/api/simulations/{sim.sim_id}").get_json()
    assert body["meta"]["sim_id"] == sim.sim_id
    assert body["config"]["event"].startswith("The council")
    assert body["editable"] is True


def test_the_config_is_available_on_its_own(client, sim):
    body = client.get(f"/api/simulations/{sim.sim_id}/config").get_json()
    assert body["rounds"] == 6
    assert "CREATE_POST" in body["action_space"]["actions"]


def test_listing_filters_by_graph(client, sim):
    body = client.get("/api/simulations?graph_id=g-1").get_json()
    assert [m["sim_id"] for m in body["simulations"]] == [sim.sim_id]
    assert client.get("/api/simulations?graph_id=g-9").get_json()["simulations"] == []


@pytest.mark.parametrize("path", ["/api/simulations/sim-20260314-090000-abcdef",
                                  "/api/simulations/nonsense",
                                  "/api/simulations/sim-20260314-090000-abcdef/config"])
def test_an_unknown_simulation_is_a_404(client, path):
    assert client.get(path).status_code == 404


# --------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------


def test_an_edit_is_saved(client, sim):
    response = client.put(f"/api/simulations/{sim.sim_id}/config",
                          json={**BASE, "event": "A revised policy was published."})
    assert response.status_code == 200
    assert response.get_json()["changes"] == []
    assert client.runtime.sims.load_config(sim.sim_id).event.startswith("A revised")


def test_an_empty_body_is_refused(client, sim):
    assert client.put(f"/api/simulations/{sim.sim_id}/config", json={}).status_code == 400


def test_an_invalid_edit_is_a_400_with_a_reason(client, sim):
    response = client.put(f"/api/simulations/{sim.sim_id}/config",
                          json={**BASE, "seed_posts": []})
    assert response.status_code == 400
    assert "seed post" in response.get_json()["error"]


def test_AN_INVENTED_QUOTE_POSTED_OVER_HTTP_IS_STILL_DEMOTED(client, sim):
    """The verification has to be wired into the endpoint, not merely to exist."""
    response = client.put(f"/api/simulations/{sim.sim_id}/config", json={
        **BASE, "seed_posts": [{
            "content": "Jane Doe said this policy will ruin the city",
            "attribution": "named_quote", "speaker": "Councillor Jane Doe"}]})

    body = response.get_json()
    assert body["config"]["seed_posts"][0]["attribution"] == "broadcaster"
    assert body["changes"], "and the operator is told"


def test_a_real_quote_posted_over_http_is_kept(client, sim):
    response = client.put(f"/api/simulations/{sim.sim_id}/config", json={
        **BASE, "seed_posts": [{
            "content": "the proposal would permit four-storey development",
            "attribution": "named_quote", "speaker": "Councillor Jane Doe"}]})

    post = response.get_json()["config"]["seed_posts"][0]
    assert post["attribution"] == "named_quote"
    assert post["source_start"] is not None


def test_an_operator_cannot_impersonate_a_named_organisation(client, sim):
    response = client.put(f"/api/simulations/{sim.sim_id}/config",
                          json={**BASE, "broadcaster": {"name": "Riverbend Gazette"}})
    assert response.get_json()["config"]["broadcaster"]["name"] != "Riverbend Gazette"


def test_an_edit_to_a_started_run_forks_and_says_so(client, sim):
    client.runtime.sims.mark_started(sim.sim_id)
    response = client.put(f"/api/simulations/{sim.sim_id}/config",
                          json={**BASE, "event": "Changed after the run began."})

    body = response.get_json()
    assert response.status_code == 201, "a new resource was created"
    assert body["forked"] is True
    assert body["forked_from"] == sim.sim_id
    assert body["sim_id"] != sim.sim_id
    assert client.runtime.sims.load_config(sim.sim_id).event.startswith("The council")


def test_a_started_run_reports_itself_uneditable(client, sim):
    client.runtime.sims.mark_started(sim.sim_id)
    assert client.get(f"/api/simulations/{sim.sim_id}").get_json()["editable"] is False


def test_editing_an_unknown_simulation_is_a_404(client):
    response = client.put("/api/simulations/sim-20260314-090000-abcdef/config", json=BASE)
    assert response.status_code == 404
