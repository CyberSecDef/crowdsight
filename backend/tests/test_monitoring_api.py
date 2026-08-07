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


# ==========================================================================
# Phase 7 Step 2 — content access
#
# The spec asks for sane page limits because a large run holds tens of
# thousands of rows, so the boundaries get more attention than the happy path:
# a cap the caller cannot raise, an offset past the end that is empty rather
# than an error, and filters that compose instead of overriding one another.
# ==========================================================================


def test_ENGINE_BOOKKEEPING_IS_NOT_AGENT_ACTIVITY(reader, populated):
    """A three-hundred agent run opens with three hundred sign-ups.

    Found by reading a real run: the trace records registration alongside
    decisions, and `sign_up` was the oldest entry in the feed.
    """
    with sqlite3.connect(populated / "simulation.db") as db:
        for user_id in range(4):
            db.execute("INSERT INTO trace (user_id, created_at, action, info)"
                       " VALUES (?, ?, 'sign_up', ?)",
                       (user_id, f"s{user_id}", "{}"))

    assert "sign_up" not in {a["action"] for a in reader.actions()["actions"]}
    assert reader.actions()["total"] == 6, "the six real decisions"

    everything = reader.actions(include_engine=True)
    assert everything["total"] == 10
    assert "sign_up" in {a["action"] for a in everything["actions"]}


def test_the_recent_log_also_excludes_engine_bookkeeping(reader, populated):
    with sqlite3.connect(populated / "simulation.db") as db:
        db.execute("INSERT INTO trace (user_id, created_at, action, info)"
                   " VALUES (0, 'z', 'sign_up', '{}')")
    assert "sign_up" not in {a["action"] for a in reader.recent_actions()}


def test_actions_are_paged_with_a_total(reader):
    page = reader.actions(limit=2, offset=0)
    assert page["count"] == 2
    assert page["total"] == 6
    assert page["has_more"] is True
    assert page["next_offset"] == 2


def test_paging_through_actions_visits_each_row_once(reader):
    seen = []
    offset = 0
    while True:
        page = reader.actions(limit=2, offset=offset)
        seen.extend(a["created_at"] for a in page["actions"])
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 6


def test_THE_PAGE_LIMIT_CANNOT_BE_RAISED_BY_A_CALLER(reader):
    """Otherwise limit=999999 is a way to ask for one enormous response."""
    from app.services.run_reader import MAX_PAGE

    assert reader.actions(limit=10_000)["limit"] == MAX_PAGE
    assert reader.posts(limit=10_000)["limit"] == MAX_PAGE
    assert reader.comments(limit=10_000)["limit"] == MAX_PAGE


def test_an_offset_past_the_end_is_empty_not_an_error(reader):
    page = reader.actions(offset=500)
    assert page["actions"] == []
    assert page["has_more"] is False
    assert page["next_offset"] is None


def test_actions_order_both_ways(reader):
    newest = reader.actions(order="newest")["actions"]
    oldest = reader.actions(order="oldest")["actions"]
    assert newest[0]["action"] == "follow"
    assert oldest[0]["action"] == "create_post"
    assert [a["created_at"] for a in newest] == list(
        reversed([a["created_at"] for a in oldest]))


def test_actions_filter_by_agent(reader):
    page = reader.actions(agent=2)
    assert page["total"] == 3
    assert {a["user_id"] for a in page["actions"]} == {2}


def test_actions_filter_by_round(reader):
    assert [a["action"] for a in reader.actions(round_index=0)["actions"]] == [
        "create_post"]
    assert reader.actions(round_index=1)["total"] == 3


def test_actions_filter_by_type(reader):
    page = reader.actions(action_types=["like_post", "follow"])
    assert page["total"] == 2
    assert {a["action"] for a in page["actions"]} == {"like_post", "follow"}


def test_FILTERS_COMPOSE_RATHER_THAN_OVERRIDE(reader):
    page = reader.actions(agent=2, round_index=1)
    assert page["total"] == 2, "agent 2 acted twice in round one"
    assert all(a["user_id"] == 2 and a["round"] == 1 for a in page["actions"])

    narrower = reader.actions(agent=2, round_index=1, action_types=["like_post"])
    assert narrower["total"] == 1


def test_a_round_that_never_ran_returns_nothing(reader):
    """Not everything, which is what a missing filter would return."""
    assert reader.actions(round_index=99)["total"] == 0


def test_posts_carry_their_author_and_round(reader):
    posts = reader.posts(order="oldest")["posts"]
    assert posts[0]["username"] == "riverbend_wire"
    assert posts[0]["round"] == 0
    assert posts[1]["username"] == "dawn_mercer"
    assert posts[1]["round"] == 1


def test_posts_report_the_engagement_they_drew(reader):
    seed = reader.posts(order="oldest")["posts"][0]
    assert seed["likes"] == 2
    assert seed["reposts"] == 1
    assert seed["comments"] == 0
    assert seed["engagement"] == 3


def test_a_posts_reply_count_comes_from_the_comments(reader):
    first = [p for p in reader.posts()["posts"] if p["user_id"] == 0][0]
    assert first["comments"] == 1
    assert first["engagement"] == 4, "three likes and a reply"


def test_posts_are_classified_by_kind(reader):
    assert {p["kind"] for p in reader.posts()["posts"]} == {"original"}


def test_posts_filter_by_agent_and_round(reader):
    assert reader.posts(agent=2)["total"] == 1
    assert reader.posts(round_index=2)["total"] == 1
    assert reader.posts(agent=0, round_index=1)["total"] == 1
    assert reader.posts(agent=0, round_index=2)["total"] == 0


def test_posts_filter_by_engagement(reader):
    assert reader.posts(min_engagement=3)["total"] == 2
    assert reader.posts(min_engagement=100)["total"] == 0


def test_the_broadcasters_posts_can_be_excluded(reader):
    """Its announcement is the loudest thing in most runs."""
    everyone = reader.posts()["total"]
    public = reader.posts(population_only=True)
    assert public["total"] == everyone - 1
    assert all(p["population"] for p in public["posts"])


def test_comments_can_be_filtered_to_one_post(reader):
    target = [p for p in reader.posts()["posts"] if p["user_id"] == 0][0]
    page = reader.comments(post_id=target["post_id"])
    assert page["total"] == 1
    assert page["comments"][0]["content"] == "I disagree."
    assert page["post_id"] == target["post_id"]


def test_comments_on_a_post_with_no_replies(reader):
    assert reader.comments(post_id=999)["total"] == 0


def test_comments_carry_their_author_and_round(reader):
    comment = reader.comments()["comments"][0]
    assert comment["username"] == "jane_doe"
    assert comment["round"] == 1


def test_content_on_a_run_that_never_started(sim_dir):
    empty = RunReader(sim_dir)
    with pytest.raises(RunNotReadable):
        empty.actions()


# -- over HTTP -------------------------------------------------------------


def test_actions_over_http(client):
    body = client.get(f"/api/simulation/{client.sim_id}/actions?limit=3").get_json()
    assert body["count"] == 3
    assert body["total"] == 6
    assert body["actions"][0]["action"] == "follow"


def test_action_filters_over_http(client):
    body = client.get(
        f"/api/simulation/{client.sim_id}/actions?agent=2&round=1").get_json()
    assert body["total"] == 2


def test_engine_actions_can_be_asked_for_over_http(client):
    body = client.get(
        f"/api/simulation/{client.sim_id}/actions?include_engine=true").get_json()
    assert body["include_engine"] is True


def test_the_action_type_filter_accepts_a_list(client):
    body = client.get(
        f"/api/simulation/{client.sim_id}/actions?action=like_post,follow").get_json()
    assert body["total"] == 2


def test_posts_over_http(client):
    body = client.get(f"/api/simulation/{client.sim_id}/posts").get_json()
    assert body["total"] == 3, "the seed plus one post each from two agents"
    assert all("engagement" in p for p in body["posts"])


def test_comments_over_http(client):
    body = client.get(f"/api/simulation/{client.sim_id}/comments").get_json()
    assert body["total"] == 1


def test_THE_PLATFORM_FILTER_IS_VALIDATED_NOT_IGNORED(client):
    """A run has one platform; filtering by another is a caller bug."""
    good = client.get(f"/api/simulation/{client.sim_id}/posts?platform=twitter")
    assert good.status_code == 200

    bad = client.get(f"/api/simulation/{client.sim_id}/posts?platform=reddit")
    assert bad.status_code == 400
    assert "twitter" in bad.get_json()["error"]


@pytest.mark.parametrize("query", ["limit=x", "offset=y", "limit=0", "offset=-1",
                                   "order=sideways", "agent=nope", "round=nope"])
def test_nonsense_paging_and_filters_are_refused(client, query):
    response = client.get(f"/api/simulation/{client.sim_id}/actions?{query}")
    assert response.status_code == 400


@pytest.mark.parametrize("endpoint", ["actions", "posts", "comments"])
def test_the_cap_is_enforced_over_http(client, endpoint):
    from app.services.run_reader import MAX_PAGE

    body = client.get(
        f"/api/simulation/{client.sim_id}/{endpoint}?limit=99999").get_json()
    assert body["limit"] == MAX_PAGE


@pytest.mark.parametrize("endpoint", ["actions", "posts", "comments"])
def test_content_endpoints_404_on_an_unknown_simulation(client, endpoint):
    response = client.get(f"/api/simulation/sim-20260101-000000-999999/{endpoint}")
    assert response.status_code == 404


# ==========================================================================
# Phase 7 Step 5 — the documented shape
#
# The spec asks that every endpoint return the documented shape. Asserting
# fields piecemeal, as the sections above do, proves the values are right but
# not that the contract is whole: a key quietly dropped in a refactor breaks a
# frontend and nothing here would notice. These name the required keys.
#
# Required, not exact: adding a field is a compatible change, removing or
# renaming one is not.
# ==========================================================================


PAGE_KEYS = {"sim_id", "total", "limit", "offset", "count", "has_more",
             "next_offset"}


def assert_has(payload, keys, where):
    missing = set(keys) - set(payload)
    assert not missing, f"{where} is missing {sorted(missing)}"


def test_run_status_shape(client):
    body = client.get(f"/api/simulation/{client.sim_id}/run-status").get_json()
    assert_has(body, {"sim_id", "state", "round", "rounds_completed",
                      "total_rounds", "percent", "action_counts",
                      "last_round_actions", "agents", "started_at",
                      "finished_at", "has_data", "live"}, "run-status")
    assert_has(body["agents"], {"active_last_round", "skipped_last_round",
                                "failed_last_round"}, "run-status.agents")


def test_run_status_live_shape_while_running(client):
    client.runtime.manager.running_ids.add(client.sim_id)
    body = client.get(f"/api/simulation/{client.sim_id}/run-status").get_json()
    assert_has(body["live"], {"running", "stage", "round_in_flight", "pid",
                              "stop_requested"}, "run-status.live")
    assert "live_stale" in body


def test_run_status_detail_shape(client):
    body = client.get(
        f"/api/simulation/{client.sim_id}/run-status/detail").get_json()
    assert_has(body, {"sim_id", "count", "actions"}, "run-status/detail")
    assert_has(body["actions"][0], {"user_id", "username", "name", "population",
                                    "action", "round", "created_at", "info"},
               "run-status/detail.actions[]")


def test_timeline_shape(client):
    body = client.get(f"/api/simulation/{client.sim_id}/timeline").get_json()
    assert_has(body, {"sim_id", "count", "from_round", "to_round", "rounds"},
               "timeline")
    assert_has(body["rounds"][0], {"round", "seed", "invoked", "acted", "failed",
                                   "skipped", "events_fired", "action_counts",
                                   "posts", "comments", "ended_at", "failures"},
               "timeline.rounds[]")


def test_agent_stats_shape(client):
    body = client.get(f"/api/simulation/{client.sim_id}/agent-stats").get_json()
    assert_has(body, {"sim_id", "total", "limit", "offset", "has_more", "sort",
                      "agents", "silent"}, "agent-stats")
    assert_has(body["agents"][0],
               {"user_id", "username", "name", "provenance", "occupation",
                "activity_level", "population", "posts", "comments",
                "likes_given", "dislikes_given", "actions", "following",
                "followers", "likes_received", "reposts_received",
                "engagement_received"}, "agent-stats.agents[]")


def test_actions_shape(client):
    body = client.get(f"/api/simulation/{client.sim_id}/actions").get_json()
    assert_has(body, PAGE_KEYS | {"order", "include_engine", "actions"}, "actions")
    assert_has(body["actions"][0], {"user_id", "username", "name", "population",
                                    "action", "round", "created_at", "info"},
               "actions.actions[]")


def test_posts_shape(client):
    body = client.get(f"/api/simulation/{client.sim_id}/posts").get_json()
    assert_has(body, PAGE_KEYS | {"order", "posts"}, "posts")
    assert_has(body["posts"][0],
               {"post_id", "user_id", "username", "name", "population",
                "content", "quote_content", "original_post_id", "kind", "round",
                "created_at", "likes", "dislikes", "reposts", "comments",
                "engagement"}, "posts.posts[]")


def test_comments_shape(client):
    body = client.get(f"/api/simulation/{client.sim_id}/comments").get_json()
    assert_has(body, PAGE_KEYS | {"order", "post_id", "comments"}, "comments")
    assert_has(body["comments"][0],
               {"comment_id", "post_id", "user_id", "username", "name",
                "population", "content", "round", "created_at", "likes",
                "dislikes"}, "comments.comments[]")


@pytest.mark.parametrize("endpoint", ["actions", "posts", "comments"])
def test_every_paged_endpoint_shares_one_envelope(client, endpoint):
    """A caller should not have to learn three pagination dialects."""
    body = client.get(f"/api/simulation/{client.sim_id}/{endpoint}").get_json()
    assert_has(body, PAGE_KEYS, endpoint)


@pytest.mark.parametrize("endpoint", ["run-status", "run-status/detail",
                                      "timeline", "agent-stats", "actions",
                                      "posts", "comments"])
def test_every_endpoint_names_its_simulation(client, endpoint):
    body = client.get(f"/api/simulation/{client.sim_id}/{endpoint}").get_json()
    assert body["sim_id"] == client.sim_id


@pytest.mark.parametrize("endpoint", ["run-status/detail", "timeline",
                                      "actions", "posts", "comments"])
def test_an_empty_result_keeps_its_shape(client, endpoint):
    """A UI binding to these fields must not have to special-case emptiness."""
    body = client.get(
        f"/api/simulation/{client.sim_id}/{endpoint}?round=99&offset=999").get_json()
    assert isinstance(body, dict) and body["sim_id"] == client.sim_id
