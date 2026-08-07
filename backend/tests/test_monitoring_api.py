"""Phase 7 Step 1 — run status, timeline and per-agent statistics.

Named in the spec under Step 5; written here because it is what proves Step 1
works. Two properties carry most of the weight:

**A finished run reads exactly like a live one.** Most of a run's life is after
it ends, and that is when the results are actually examined. Every endpoint
therefore answers from the database on disk, with live worker fields as an
enrichment — so a run whose worker is long gone is fully readable, and a run
whose worker is wedged still answers rather than hanging.

**The broadcaster is not a member of the public.** It posts, and it will
therefore appear in any naive per-agent aggregate as the loudest participant in
the simulation. It is flagged so influence statistics can exclude it.

The fixtures build a database with OASIS's real schema rather than a
convenient one, because the aggregation depends on its actual column names —
`follow(follower_id, followee_id)`, `like(user_id, post_id)` — which no
hand-written stand-in would get wrong in the same way.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from app.services.run_reader import RunNotReadable, RunReader, identifier
from app.services.simulation_config_generator import SimulationConfig
from app.services.simulation_persistence import RoundRecord, RunLedger
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
     "provenance": "synthetic", "occupation": "carpenter", "activity_level": "high"},
    {"user_id": 1, "username": "ray_nkemelu", "name": "Ray Nkemelu",
     "provenance": "synthetic", "occupation": "bus driver", "activity_level": "low"},
    {"user_id": 2, "username": "jane_doe", "name": "Councillor Jane Doe",
     "provenance": "named", "occupation": "councillor", "activity_level": "moderate"},
]


# --------------------------------------------------------------------------
# A run on disk, with OASIS's real schema
# --------------------------------------------------------------------------


@pytest.fixture
def sim_dir(tmp_path):
    directory = tmp_path / "sim-20260101-000000-abcdef"
    (directory / "profiles").mkdir(parents=True)
    (directory / "profiles" / "profiles.json").write_text(json.dumps(POPULATION))
    return directory


@pytest.fixture
def populated(sim_dir):
    """Three agents, a broadcaster, and three rounds of recorded activity."""
    from oasis.social_platform.database import create_db

    path = sim_dir / "simulation.db"
    connection, _ = create_db(str(path))
    connection.close()

    ledger = RunLedger(path)
    ledger.ensure_schema()

    def user(user_id, name):
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO user (user_id, agent_id, user_name, name, bio,"
                " created_at, num_followings, num_followers)"
                " VALUES (?, ?, ?, ?, '', '0', 0, 0)", (user_id, user_id, name, name))

    def post(user_id, content, likes=0, shares=0):
        with sqlite3.connect(path) as db:
            return db.execute(
                "INSERT INTO post (user_id, content, created_at, num_likes,"
                " num_shares) VALUES (?, ?, '0', ?, ?)",
                (user_id, content, likes, shares)).lastrowid

    def comment(user_id, post_id, content):
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO comment (post_id, user_id, content, created_at)"
                       " VALUES (?, ?, ?, '0')", (post_id, user_id, content))

    def like(user_id, post_id):
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO like (user_id, post_id, created_at)"
                       " VALUES (?, ?, '0')", (user_id, post_id))

    def follow(follower, followee):
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO follow (follower_id, followee_id, created_at)"
                       " VALUES (?, ?, '0')", (follower, followee))

    counter = iter(range(1, 9999))

    def trace(user_id, action):
        tick = next(counter)
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO trace (user_id, created_at, action, info)"
                       " VALUES (?, ?, ?, ?)",
                       (user_id, str(tick), action, json.dumps({"n": tick})))

    for user_id, name in ((0, "dawn_mercer"), (1, "ray_nkemelu"),
                          (2, "jane_doe"), (3, "riverbend_wire")):
        user(user_id, name)

    # Round 0 — the seed.
    seed = post(3, "Council publishes draft density policy.", likes=2, shares=1)
    trace(3, "create_post")
    ledger.record_round(RoundRecord(round=0, invoked=1, acted=1,
                                    action_counts={"create_post": 1}))

    # Round 1 — two agents act, one is quiet.
    first = post(0, "This will ruin the corridor.", likes=3)
    trace(0, "create_post")
    like(2, seed)
    trace(2, "like_post")
    comment(2, first, "I disagree.")
    trace(2, "create_comment")
    ledger.record_round(RoundRecord(
        round=1, invoked=3, acted=2, failed=0, skipped=1,
        action_counts={"create_post": 1, "like_post": 1, "create_comment": 1}))

    # Round 2 — a follow and a failure.
    post(2, "The consultation runs twenty-one days.")
    trace(2, "create_post")
    follow(0, 2)
    trace(0, "follow")
    ledger.record_round(RoundRecord(
        round=2, invoked=3, acted=2, failed=1, skipped=0,
        action_counts={"create_post": 1, "follow": 1},
        failures=["ray_nkemelu: TimeoutError: too slow"]))

    return sim_dir


@pytest.fixture
def reader(populated):
    return RunReader(populated)


class Meta:
    state = "complete"
    started_at = "2026-01-01T00:00:00+00:00"
    finished_at = "2026-01-01T01:00:00+00:00"


# --------------------------------------------------------------------------
# Identifier validation
# --------------------------------------------------------------------------


def test_a_bare_identifier_is_accepted():
    assert identifier("user_id") == "user_id"


@pytest.mark.parametrize("bad", ["user_id; DROP TABLE user", "", "1abc", "a-b",
                                 "user id", "*"])
def test_ANYTHING_THAT_IS_NOT_AN_IDENTIFIER_IS_REFUSED(bad):
    """SQL cannot parameterise a table name, so this is what stands in for it."""
    with pytest.raises(ValueError):
        identifier(bad)


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


def test_status_reports_progress_against_the_total(reader):
    status = reader.status(meta=Meta(), total_rounds=3)
    assert status["round"] == 2
    assert status["rounds_completed"] == 2
    assert status["total_rounds"] == 3
    assert status["percent"] == pytest.approx(66.7, abs=0.1)


def test_status_accumulates_action_counts_across_rounds(reader):
    counts = reader.status(meta=Meta(), total_rounds=3)["action_counts"]
    assert counts["create_post"] == 3
    assert counts["like_post"] == 1
    assert counts["follow"] == 1


def test_status_reports_the_last_rounds_agent_activity(reader):
    agents = reader.status(meta=Meta(), total_rounds=3)["agents"]
    assert agents["active_last_round"] == 2
    assert agents["failed_last_round"] == 1


def test_A_FINISHED_RUN_NEEDS_NO_WORKER(reader):
    """Most of a run's life is after it ends."""
    status = reader.status(meta=Meta(), total_rounds=3, live=None)
    assert status["live"] is None
    assert status["has_data"] is True
    assert status["rounds_completed"] == 2


def test_live_fields_enrich_a_running_run(reader):
    status = reader.status(meta=Meta(), total_rounds=3, live={
        "running": True, "stage": "running", "round": 3, "pid": 42})
    assert status["live"]["stage"] == "running"
    assert status["live"]["round_in_flight"] == 3
    assert status["live_stale"] is False


def test_AN_UNREACHABLE_WORKER_STILL_RETURNS_A_STATUS(reader):
    """A poll must never hang because a worker is wedged."""
    status = reader.status(meta=Meta(), total_rounds=3,
                           live={"unreachable": "timed out after 5.0s"})
    assert status["live"] is None
    assert status["live_stale"] is True
    assert "timed out" in status["live_error"]
    assert status["rounds_completed"] == 2, "the disk answer is still there"


def test_status_on_a_run_that_never_started(sim_dir):
    status = RunReader(sim_dir).status(meta=Meta(), total_rounds=3)
    assert status["has_data"] is False
    assert status["percent"] == 0.0
    assert status["action_counts"] == {}


def test_percent_never_exceeds_a_hundred(reader):
    """A resumed run can record more rounds than the config now says."""
    assert reader.status(meta=Meta(), total_rounds=1)["percent"] == 100.0


# --------------------------------------------------------------------------
# The action log
# --------------------------------------------------------------------------


def test_the_action_log_is_newest_first(reader):
    actions = reader.recent_actions(limit=10)
    assert actions[0]["action"] == "follow"
    assert [a["action"] for a in actions][:2] == ["follow", "create_post"]


def test_each_action_names_the_agent_that_took_it(reader):
    actions = reader.recent_actions(limit=10)
    assert actions[0]["username"] == "dawn_mercer"
    assert actions[0]["name"] == "Dawn Mercer"


def test_each_action_is_attributed_to_its_round(reader):
    by_action = {(a["action"], a["user_id"]): a["round"]
                 for a in reader.recent_actions(limit=20)}
    assert by_action[("create_post", 3)] == 0, "the seed"
    assert by_action[("follow", 0)] == 2


def test_the_broadcaster_is_flagged_in_the_log(reader):
    actions = reader.recent_actions(limit=20)
    broadcaster = [a for a in actions if a["user_id"] == 3]
    assert broadcaster and broadcaster[0]["population"] is False


def test_action_arguments_are_returned_as_data_not_a_string(reader):
    assert isinstance(reader.recent_actions(limit=1)[0]["info"], dict)


def test_the_log_respects_its_limit(reader):
    assert len(reader.recent_actions(limit=2)) == 2


def test_the_log_limit_is_bounded(reader):
    """A large run holds tens of thousands of rows."""
    from app.services.run_reader import MAX_PAGE

    assert len(reader.recent_actions(limit=10_000)) <= MAX_PAGE


def test_the_log_on_a_run_that_never_started(sim_dir):
    assert RunReader(sim_dir).recent_actions() == []


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------


def test_the_timeline_has_a_row_per_round(reader):
    assert [r["round"] for r in reader.timeline()] == [0, 1, 2]


def test_the_seed_round_is_marked_as_such(reader):
    assert reader.timeline()[0]["seed"] is True
    assert reader.timeline()[1]["seed"] is False


def test_the_timeline_counts_what_each_round_produced(reader):
    rounds = {r["round"]: r for r in reader.timeline()}
    assert rounds[0]["posts"] == 1
    assert rounds[1]["posts"] == 1
    assert rounds[1]["comments"] == 1
    assert rounds[2]["posts"] == 1


def test_the_timeline_carries_per_round_action_counts(reader):
    assert reader.timeline()[1]["action_counts"]["like_post"] == 1


def test_the_timeline_reports_failures(reader):
    assert "TimeoutError" in reader.timeline()[2]["failures"][0]


@pytest.mark.parametrize(("first", "last", "expected"), [
    (1, None, [1, 2]),
    (None, 1, [0, 1]),
    (1, 1, [1]),
    (0, 2, [0, 1, 2]),
    (5, None, []),
])
def test_the_timeline_range_filters(reader, first, last, expected):
    assert [r["round"] for r in
            reader.timeline(from_round=first, to_round=last)] == expected


def test_the_timeline_on_a_run_that_never_started(sim_dir):
    assert RunReader(sim_dir).timeline() == []


# --------------------------------------------------------------------------
# Per-agent statistics
# --------------------------------------------------------------------------


def test_agent_stats_covers_the_population_and_the_broadcaster(reader):
    stats = reader.agent_stats()
    assert stats["total"] == 4
    assert {a["user_id"] for a in stats["agents"]} == {0, 1, 2, 3}


def test_THE_BROADCASTER_IS_FLAGGED_NOT_COUNTED_AS_PUBLIC(reader):
    """It posts, so a naive aggregate makes it the loudest participant."""
    agents = {a["user_id"]: a for a in reader.agent_stats()["agents"]}
    assert agents[3]["population"] is False
    assert agents[3]["provenance"] == "broadcaster"
    assert all(agents[i]["population"] for i in (0, 1, 2))


def test_the_population_can_be_isolated(reader):
    stats = reader.agent_stats(population_only=True)
    assert stats["total"] == 3
    assert all(a["population"] for a in stats["agents"])


def test_activity_is_counted_per_agent(reader):
    agents = {a["user_id"]: a for a in reader.agent_stats()["agents"]}
    assert agents[0]["posts"] == 1
    assert agents[0]["actions"] == 2
    assert agents[2]["comments"] == 1
    assert agents[2]["likes_given"] == 1
    assert agents[2]["posts"] == 1


def test_engagement_received_is_separate_from_activity(reader):
    """What an agent drew, as opposed to what it did."""
    agents = {a["user_id"]: a for a in reader.agent_stats()["agents"]}
    assert agents[0]["likes_received"] == 3
    assert agents[3]["likes_received"] == 2
    assert agents[3]["reposts_received"] == 1
    assert agents[3]["engagement_received"] == 3


def test_the_follow_graph_is_counted_in_both_directions(reader):
    agents = {a["user_id"]: a for a in reader.agent_stats()["agents"]}
    assert agents[0]["following"] == 1
    assert agents[2]["followers"] == 1


def test_a_silent_agent_is_reported_rather_than_omitted(reader):
    """An agent that never acted is a finding, not an absence."""
    agents = {a["user_id"]: a for a in reader.agent_stats()["agents"]}
    assert agents[1]["actions"] == 0
    assert reader.agent_stats()["silent"] == 1


def test_the_persona_travels_with_the_statistics(reader):
    agents = {a["user_id"]: a for a in reader.agent_stats()["agents"]}
    assert agents[2]["provenance"] == "named"
    assert agents[2]["occupation"] == "councillor"


@pytest.mark.parametrize("sort", ["actions", "posts", "engagement_received",
                                  "followers", "user_id"])
def test_sorting_is_stable_and_ordered(reader, sort):
    agents = reader.agent_stats(sort=sort)["agents"]
    values = [a[sort] if sort != "user_id" else -a["user_id"] for a in agents]
    assert values == sorted(values, reverse=True)


def test_an_unknown_sort_falls_back_rather_than_failing(reader):
    assert reader.agent_stats(sort="nonsense")["sort"] == "actions"


def test_agent_stats_paginate(reader):
    first = reader.agent_stats(limit=2, offset=0)
    second = reader.agent_stats(limit=2, offset=2)

    assert len(first["agents"]) == 2 and first["has_more"] is True
    assert len(second["agents"]) == 2 and second["has_more"] is False
    assert not ({a["user_id"] for a in first["agents"]}
                & {a["user_id"] for a in second["agents"]})


def test_an_offset_past_the_end_is_empty_not_an_error(reader):
    assert reader.agent_stats(offset=999)["agents"] == []


def test_the_page_size_is_bounded(reader):
    from app.services.run_reader import MAX_PAGE

    assert reader.agent_stats(limit=100_000)["limit"] == MAX_PAGE


def test_agent_stats_on_a_run_that_never_started(sim_dir):
    with pytest.raises(RunNotReadable):
        RunReader(sim_dir).agent_stats()


# --------------------------------------------------------------------------
# Indexes
# --------------------------------------------------------------------------


def test_the_missing_indexes_are_created(reader, populated):
    assert reader.ensure_indexes() > 0
    with sqlite3.connect(populated / "simulation.db") as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "crowdsight_post_user" in names
    assert "crowdsight_follow_followee" in names


def test_indexing_happens_once(reader):
    reader.ensure_indexes()
    assert reader.ensure_indexes() == 0


def test_indexing_a_run_that_never_started_is_harmless(sim_dir):
    assert RunReader(sim_dir).ensure_indexes() == 0


# ==========================================================================
# Over HTTP
# ==========================================================================


class StubManager:
    def __init__(self):
        self.running_ids: set[str] = set()
        self.live: dict[str, Any] = {}

    def is_running(self, sim_id):
        return sim_id in self.running_ids

    def running(self):
        return sorted(self.running_ids)

    def status(self, sim_id):
        return self.live.get(sim_id, {"running": True, "stage": "running"})


class StubRuntime:
    def __init__(self, tmp_path, config, store):
        self.config = config
        self.tasks = TaskStore(tmp_path / "tasks.db")
        self.sims = store
        self.manager = StubManager()
        self.submitted: list[Any] = []
        self.runner = self

    def submit(self, task, job):
        self.submitted.append((task, job))
        return task

    def run(self, coro, timeout=60.0):
        import asyncio

        return asyncio.run(coro)


@pytest.fixture
def client(tmp_path, config, monkeypatch, populated):
    from app.main import create_app

    store = SimulationStore(populated.parent)
    runtime = StubRuntime(tmp_path, config, store)
    monkeypatch.setattr("app.api.simulation.get_runtime", lambda **_: runtime)
    app = create_app()
    app.config.update(TESTING=True)
    store.save_config(populated.name, SimulationConfig.model_validate(SCENARIO))
    with app.test_client() as test_client:
        test_client.runtime = runtime  # type: ignore[attr-defined]
        test_client.sim_id = populated.name  # type: ignore[attr-defined]
        yield test_client


def test_run_status_over_http(client):
    body = client.get(f"/api/simulation/{client.sim_id}/run-status").get_json()
    assert body["total_rounds"] == 3
    assert body["rounds_completed"] == 2
    assert body["action_counts"]["create_post"] == 3


def test_run_status_includes_live_fields_while_running(client):
    client.runtime.manager.running_ids.add(client.sim_id)
    body = client.get(f"/api/simulation/{client.sim_id}/run-status").get_json()
    assert body["live"]["stage"] == "running"


def test_run_status_detail_over_http(client):
    body = client.get(
        f"/api/simulation/{client.sim_id}/run-status/detail?limit=3").get_json()
    assert body["count"] == 3
    assert body["actions"][0]["action"] == "follow"


def test_timeline_over_http(client):
    body = client.get(f"/api/simulation/{client.sim_id}/timeline").get_json()
    assert [r["round"] for r in body["rounds"]] == [0, 1, 2]


def test_timeline_range_over_http(client):
    body = client.get(
        f"/api/simulation/{client.sim_id}/timeline?from_round=1&to_round=1").get_json()
    assert [r["round"] for r in body["rounds"]] == [1]


def test_an_inverted_range_is_refused(client):
    response = client.get(
        f"/api/simulation/{client.sim_id}/timeline?from_round=3&to_round=1")
    assert response.status_code == 400


@pytest.mark.parametrize("query", ["from_round=x", "to_round=y"])
def test_a_nonsense_range_is_refused(client, query):
    assert client.get(
        f"/api/simulation/{client.sim_id}/timeline?{query}").status_code == 400


def test_agent_stats_over_http(client):
    body = client.get(f"/api/simulation/{client.sim_id}/agent-stats").get_json()
    assert body["total"] == 4
    assert body["silent"] == 1


def test_agent_stats_filters_to_the_population_over_http(client):
    body = client.get(
        f"/api/simulation/{client.sim_id}/agent-stats?population_only=true").get_json()
    assert body["total"] == 3


def test_agent_stats_paginates_over_http(client):
    body = client.get(
        f"/api/simulation/{client.sim_id}/agent-stats?limit=1&offset=1").get_json()
    assert len(body["agents"]) == 1
    assert body["has_more"] is True


@pytest.mark.parametrize("query", ["limit=x", "offset=y"])
def test_nonsense_pagination_is_refused(client, query):
    assert client.get(
        f"/api/simulation/{client.sim_id}/agent-stats?{query}").status_code == 400


@pytest.mark.parametrize("suffix", ["run-status", "run-status/detail", "timeline",
                                    "agent-stats"])
def test_an_unknown_simulation_is_a_404_everywhere(client, suffix):
    response = client.get(f"/api/simulation/sim-20260101-000000-999999/{suffix}")
    assert response.status_code == 404
