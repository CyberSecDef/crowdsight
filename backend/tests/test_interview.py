"""Phase 7 Step 3 — interviewing agents.

The spec asks for four properties, and each has a reason behind it:

* a single interview is attributed to the right agent — an answer credited to
  the wrong persona is worse than no answer;
* a batch returns one result per request, so a caller can line answers up
  against the questions that produced them;
* interviewing a non-existent agent errors cleanly, rather than as a transport
  failure that reads like the simulation broke;
* an interview against a stopped simulation fails fast rather than hanging,
  because the agents only exist inside the running process.

The last two are the ones that were actually wrong when first written: a real
run returned `502 The simulation did not answer: ValueError` for an unknown
agent id, which is indistinguishable from the worker having crashed.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from app.services.interview import (
    INTERVIEW_ACTION,
    InterviewError,
    SimulationNotLive,
    conduct,
    history,
    interviewable,
)
from app.services.simulation_config_generator import SimulationConfig
from app.services.simulation_ipc import IPCError, WorkerUnreachable
from app.services.simulation_store import SimulationStore
from app.services.tasks import TaskStore

SCENARIO = {
    "graph_id": "g-1",
    "event": "The council published a draft housing density policy.",
    "rounds": 3,
    "broadcaster": {"name": "Riverbend Wire"},
    "seed_posts": [{"content": "Council publishes draft density policy."}],
}

POPULATION = [
    {"user_id": 0, "username": "dawn_mercer", "name": "Dawn Mercer",
     "provenance": "synthetic", "activity_level": "high"},
    {"user_id": 1, "username": "ray_nkemelu", "name": "Ray Nkemelu",
     "provenance": "named", "activity_level": "moderate"},
]


# --------------------------------------------------------------------------
# A run with a database, and a stand-in worker
# --------------------------------------------------------------------------


@pytest.fixture
def sim_dir(tmp_path):
    from oasis.social_platform.database import create_db

    directory = tmp_path / "sim-20260101-000000-abcdef"
    (directory / "profiles").mkdir(parents=True)
    (directory / "profiles" / "profiles.json").write_text(json.dumps(POPULATION))

    connection, _ = create_db(str(directory / "simulation.db"))
    connection.close()
    with sqlite3.connect(directory / "simulation.db") as db:
        for user_id, name in ((0, "dawn_mercer"), (1, "ray_nkemelu"),
                              (2, "riverbend_wire")):
            db.execute(
                "INSERT INTO user (user_id, agent_id, user_name, name, bio,"
                " created_at, num_followings, num_followers)"
                " VALUES (?, ?, ?, ?, '', '0', 0, 0)", (user_id, user_id, name, name))
    return directory


def record_interview(sim_dir, user_id, question, response, tick):
    """An interview, written the way OASIS writes one."""
    with sqlite3.connect(sim_dir / "simulation.db") as db:
        db.execute(
            "INSERT INTO trace (user_id, created_at, action, info) VALUES (?, ?, ?, ?)",
            (user_id, str(tick), INTERVIEW_ACTION,
             json.dumps({"prompt": question, "response": response})))


class StubClient:
    """Stands in for the control socket."""

    def __init__(self, answers=None, error=None):
        self.answers = answers
        self.error = error
        self.calls: list[tuple[str, dict]] = []
        self.timeout = 5.0

    def request(self, command, **args):
        self.calls.append((command, args))
        if self.error:
            raise self.error
        wanted = args.get("agents")
        answers = self.answers if self.answers is not None else [
            {"user_id": 0, "username": "dawn_mercer",
             "question": args["question"], "response": "I am against it.",
             "recorded": True}]
        if wanted is not None:
            answers = [a for a in answers if a["user_id"] in set(wanted)]
        return {"sim_id": "sim", "round": 2, "count": len(answers),
                "answers": answers}


class StubManager:
    def __init__(self, running=True, client=None):
        self._running = running
        self._client = client or StubClient()

    def is_running(self, sim_id):
        return self._running

    def running(self):
        return ["sim"] if self._running else []

    def client(self, sim_id):
        return self._client

    def status(self, sim_id):
        return {"running": self._running, "stage": "running"}


# --------------------------------------------------------------------------
# Who can be interviewed
# --------------------------------------------------------------------------


def test_the_population_is_interviewable(sim_dir):
    assert interviewable(sim_dir) == {0: "dawn_mercer", 1: "ray_nkemelu"}


def test_THE_BROADCASTER_IS_NOT_INTERVIEWABLE(sim_dir):
    """A synthetic news account has no persona and nothing to say about itself."""
    assert 2 not in interviewable(sim_dir)


def test_a_run_with_no_population_yields_nobody(tmp_path):
    assert interviewable(tmp_path / "nothing") == {}


# --------------------------------------------------------------------------
# A single interview
# --------------------------------------------------------------------------


def test_an_interview_reaches_the_worker(sim_dir):
    client = StubClient()
    result = conduct(StubManager(client=client), "sim", "How do you feel?", agents=[0])

    assert client.calls[0][0] == "interview"
    assert client.calls[0][1]["question"] == "How do you feel?"
    assert result["answers"][0]["response"] == "I am against it."


def test_the_answer_is_attributed_to_the_agent_asked(sim_dir):
    client = StubClient(answers=[
        {"user_id": 0, "username": "dawn_mercer", "question": "q", "response": "a"},
        {"user_id": 1, "username": "ray_nkemelu", "question": "q", "response": "b"},
    ])
    result = conduct(StubManager(client=client), "sim", "q", agents=[1])

    assert [a["user_id"] for a in result["answers"]] == [1]
    assert result["answers"][0]["username"] == "ray_nkemelu"


def test_AN_INTERVIEW_AGAINST_A_STOPPED_SIMULATION_FAILS_FAST(sim_dir):
    """The agents live in the process; there is nobody to ask."""
    with pytest.raises(SimulationNotLive, match="not running"):
        conduct(StubManager(running=False), "sim", "How do you feel?", agents=[0])


def test_the_refusal_says_where_history_still_lives(sim_dir):
    with pytest.raises(SimulationNotLive, match="history"):
        conduct(StubManager(running=False), "sim", "q", agents=[0])


def test_an_empty_question_is_refused_before_any_round_trip():
    client = StubClient()
    with pytest.raises(InterviewError, match="needs a question"):
        conduct(StubManager(client=client), "sim", "   ", agents=[0])
    assert client.calls == []


def test_an_enormous_question_is_refused():
    with pytest.raises(InterviewError, match="limit is"):
        conduct(StubManager(), "sim", "x" * 5000, agents=[0])


def test_a_wedged_worker_surfaces_as_an_interview_error():
    manager = StubManager(client=StubClient(error=WorkerUnreachable("timed out")))
    with pytest.raises(InterviewError, match="did not answer"):
        conduct(manager, "sim", "q", agents=[0])


def test_the_interview_timeout_is_generous(sim_dir):
    """It shares the GPU with a round; a completion can take minutes."""
    from app.services.interview import INTERVIEW_TIMEOUT

    client = StubClient()
    conduct(StubManager(client=client), "sim", "q", agents=[0])
    assert client.timeout == INTERVIEW_TIMEOUT
    assert INTERVIEW_TIMEOUT >= 300


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def test_history_is_empty_before_anything_is_asked(sim_dir):
    assert history(sim_dir)["total"] == 0


def test_history_returns_question_and_answer(sim_dir):
    record_interview(sim_dir, 0, "How do you feel?", "Concerned.", 1)
    entry = history(sim_dir)["interviews"][0]

    assert entry["question"] == "How do you feel?"
    assert entry["response"] == "Concerned."
    assert entry["username"] == "dawn_mercer"


def test_history_survives_the_run(sim_dir):
    """Recorded by OASIS as it happens, so no live process is needed."""
    record_interview(sim_dir, 0, "q", "a", 1)
    assert history(sim_dir)["total"] == 1


def test_history_filters_by_agent(sim_dir):
    record_interview(sim_dir, 0, "q", "dawn's answer", 1)
    record_interview(sim_dir, 1, "q", "ray's answer", 2)

    assert history(sim_dir, agent=1)["total"] == 1
    assert history(sim_dir, agent=1)["interviews"][0]["response"] == "ray's answer"


def test_history_pages_and_orders(sim_dir):
    for index in range(5):
        record_interview(sim_dir, 0, f"question {index}", f"answer {index}", index + 1)

    page = history(sim_dir, limit=2)
    assert page["count"] == 2 and page["total"] == 5 and page["has_more"] is True
    assert page["interviews"][0]["question"] == "question 4"
    assert history(sim_dir, order="oldest")["interviews"][0]["question"] == "question 0"


def test_history_ignores_ordinary_actions(sim_dir):
    with sqlite3.connect(sim_dir / "simulation.db") as db:
        db.execute("INSERT INTO trace (user_id, created_at, action, info)"
                   " VALUES (0, '9', 'create_post', '{}')")
    record_interview(sim_dir, 0, "q", "a", 1)

    assert history(sim_dir)["total"] == 1


def test_a_malformed_history_row_does_not_break_the_page(sim_dir):
    """The info column is free text; a row from another version may not be JSON."""
    with sqlite3.connect(sim_dir / "simulation.db") as db:
        db.execute("INSERT INTO trace (user_id, created_at, action, info)"
                   " VALUES (0, '1', 'interview', 'not json at all')")
    entry = history(sim_dir)["interviews"][0]
    assert entry["response"] == "not json at all"


def test_history_on_a_run_that_never_started(tmp_path):
    assert history(tmp_path / "nothing")["interviews"] == []


# ==========================================================================
# Over HTTP
# ==========================================================================


class StubRuntime:
    def __init__(self, tmp_path, config, store, manager):
        self.config = config
        self.tasks = TaskStore(tmp_path / "tasks.db")
        self.sims = store
        self.manager = manager
        self.submitted: list[Any] = []
        self.runner = self

    def submit(self, task, job):
        self.submitted.append((task, job))
        return task

    def run(self, coro, timeout=60.0):
        import asyncio

        return asyncio.run(coro)


@pytest.fixture
def manager():
    return StubManager()


@pytest.fixture
def client(tmp_path, config, monkeypatch, sim_dir, manager):
    from app.main import create_app

    store = SimulationStore(sim_dir.parent)
    store.save_config(sim_dir.name, SimulationConfig.model_validate(SCENARIO))
    store.mark_started(sim_dir.name)

    runtime = StubRuntime(tmp_path, config, store, manager)
    monkeypatch.setattr("app.api.simulation.get_runtime", lambda **_: runtime)
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        test_client.runtime = runtime  # type: ignore[attr-defined]
        test_client.sim_id = sim_dir.name  # type: ignore[attr-defined]
        test_client.sim_dir = sim_dir  # type: ignore[attr-defined]
        yield test_client


def test_a_single_interview_answers_inline(client):
    response = client.post("/api/simulation/interview", json={
        "sim_id": client.sim_id, "agent": 0, "question": "How do you feel?"})
    body = response.get_json()

    assert response.status_code == 200
    assert body["user_id"] == 0
    assert body["response"] == "I am against it."
    assert body["round"] == 2


def test_INTERVIEWING_A_NON_EXISTENT_AGENT_IS_A_CLEAN_404(client):
    """A real run answered this as a 502 transport error, which reads as a crash."""
    response = client.post("/api/simulation/interview", json={
        "sim_id": client.sim_id, "agent": 9999, "question": "Anything?"})

    assert response.status_code == 404
    assert "9999" in response.get_json()["error"]


def test_the_broadcaster_cannot_be_interviewed_over_http(client):
    response = client.post("/api/simulation/interview", json={
        "sim_id": client.sim_id, "agent": 2, "question": "Anything?"})
    assert response.status_code == 404


def test_an_interview_needs_an_agent(client):
    response = client.post("/api/simulation/interview", json={
        "sim_id": client.sim_id, "question": "Anything?"})
    assert response.status_code == 400
    assert "interview/all" in response.get_json()["error"]


def test_an_interview_needs_a_question(client):
    response = client.post("/api/simulation/interview", json={
        "sim_id": client.sim_id, "agent": 0})
    assert response.status_code == 400


def test_A_STOPPED_SIMULATION_IS_A_409_NOT_A_HANG(client, manager):
    manager._running = False
    response = client.post("/api/simulation/interview", json={
        "sim_id": client.sim_id, "agent": 0, "question": "How do you feel?"})

    assert response.status_code == 409
    assert "not running" in response.get_json()["error"]


def test_a_wedged_worker_is_a_502_not_a_404(client, manager):
    """A transport failure and an unknown agent must not look the same."""
    manager._client = StubClient(error=IPCError("did not answer within 600s"))
    response = client.post("/api/simulation/interview", json={
        "sim_id": client.sim_id, "agent": 0, "question": "q"})
    assert response.status_code == 502


def test_batch_returns_a_task_rather_than_blocking(client):
    response = client.post("/api/simulation/interview/batch", json={
        "sim_id": client.sim_id, "agents": [0, 1], "question": "q"})
    body = response.get_json()

    assert response.status_code == 202
    assert body["task_id"] and body["agents"] == 2
    assert len(client.runtime.submitted) == 1


def test_batch_rejects_an_unknown_agent_before_starting(client):
    response = client.post("/api/simulation/interview/batch", json={
        "sim_id": client.sim_id, "agents": [0, 9999], "question": "q"})

    assert response.status_code == 404
    assert client.runtime.submitted == [], "no task should have been created"


@pytest.mark.parametrize("agents", [[], "everyone", None, {}])
def test_batch_needs_a_list_of_agents(client, agents):
    response = client.post("/api/simulation/interview/batch", json={
        "sim_id": client.sim_id, "agents": agents, "question": "q"})
    assert response.status_code == 400


def test_all_returns_a_task(client):
    response = client.post("/api/simulation/interview/all", json={
        "sim_id": client.sim_id, "question": "q"})
    body = response.get_json()

    assert response.status_code == 202
    assert body["agents"] == "all"


@pytest.mark.parametrize("path", ["/api/simulation/interview/batch",
                                  "/api/simulation/interview/all"])
def test_bulk_interviews_need_a_live_simulation(client, manager, path):
    manager._running = False
    payload = {"sim_id": client.sim_id, "question": "q", "agents": [0]}
    assert client.post(path, json=payload).status_code == 409


def test_history_over_http(client):
    record_interview(client.sim_dir, 0, "How do you feel?", "Concerned.", 1)
    body = client.post("/api/simulation/interview/history",
                       json={"sim_id": client.sim_id}).get_json()

    assert body["total"] == 1
    assert body["interviews"][0]["question"] == "How do you feel?"


def test_history_is_readable_after_the_run_stops(client, manager):
    record_interview(client.sim_dir, 0, "q", "a", 1)
    manager._running = False

    response = client.post("/api/simulation/interview/history",
                           json={"sim_id": client.sim_id})
    assert response.status_code == 200
    assert response.get_json()["total"] == 1


def test_history_filters_by_agent_over_http(client):
    record_interview(client.sim_dir, 0, "q", "dawn", 1)
    record_interview(client.sim_dir, 1, "q", "ray", 2)

    body = client.post("/api/simulation/interview/history",
                       json={"sim_id": client.sim_id, "agent": 1}).get_json()
    assert body["total"] == 1


@pytest.mark.parametrize("payload", [{"limit": "x"}, {"offset": -1}, {"limit": 0},
                                     {"order": "sideways"}, {"agent": "nope"}])
def test_nonsense_history_arguments_are_refused(client, payload):
    response = client.post("/api/simulation/interview/history",
                           json={"sim_id": client.sim_id, **payload})
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/api/simulation/interview",
                                  "/api/simulation/interview/batch",
                                  "/api/simulation/interview/all",
                                  "/api/simulation/interview/history"])
def test_every_interview_endpoint_needs_a_simulation(client, path):
    assert client.post(path, json={"question": "q"}).status_code == 404


@pytest.mark.parametrize("path", ["/api/simulation/interview",
                                  "/api/simulation/interview/history"])
def test_an_unknown_simulation_is_a_404(client, path):
    response = client.post(path, json={
        "sim_id": "sim-20260101-000000-999999", "agent": 0, "question": "q"})
    assert response.status_code == 404
