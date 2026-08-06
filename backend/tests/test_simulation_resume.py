"""Phase 6 Step 3 — resuming a run that was killed mid-flight.

The requirement is "resumes from its last checkpoint without duplicating
rounds", and the hard part is the round that was in progress when the process
died. It is half-applied: some agents acted and are in the database, the rest
never got a turn. Continuing past it bakes in a permanently lopsided round;
re-running it without cleaning up lets the agents who already acted act twice.

Neither is acceptable, so resume rolls the database back to the last completed
round's boundary and re-runs the interrupted round from a clean state. These
tests exercise that against a database with OASIS's real schema, and the
integration test at the bottom kills an actual simulation process.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.services.simulation_persistence import RoundRecord, RunLedger


@pytest.fixture
def oasis_db(tmp_path):
    from oasis.social_platform.database import create_db

    path = tmp_path / "simulation.db"
    connection, _ = create_db(str(path))
    connection.close()
    return path


@pytest.fixture
def ledger(oasis_db):
    ledger = RunLedger(oasis_db)
    ledger.ensure_schema()
    return ledger


def add_user(path, user_id, name="agent"):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO user (user_id, agent_id, user_name, name, bio, created_at,"
            " num_followings, num_followers) VALUES (?, ?, ?, ?, '', '0', 0, 0)",
            (user_id, user_id, f"{name}{user_id}", name))


def act(path, user_id, content):
    """One agent taking one action, as OASIS would record it."""
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO post (user_id, content, created_at) VALUES (?, ?, '0')",
            (user_id, content))
        connection.execute(
            "INSERT INTO trace (user_id, created_at, action, info)"
            " VALUES (?, ?, 'create_post', ?)",
            (user_id, f"t{content}", content))


def posts(path):
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute("SELECT content FROM post")]


def simulate_interrupted_run(ledger, oasis_db, *, agents=3, completed_rounds=2):
    """Rounds that finished and checkpointed, then one that did not."""
    for user_id in range(agents):
        add_user(oasis_db, user_id)

    act(oasis_db, 0, "seed")
    ledger.record_round(RoundRecord(round=0, invoked=1, acted=1))

    for index in range(1, completed_rounds + 1):
        for user_id in range(agents):
            act(oasis_db, user_id, f"r{index}-a{user_id}")
        ledger.record_round(RoundRecord(round=index, invoked=agents, acted=agents))

    # The round that was in flight when the process died: only some agents got
    # their turn, and no checkpoint was written.
    act(oasis_db, 0, "partial-a0")
    act(oasis_db, 1, "partial-a1")


# --------------------------------------------------------------------------
# What a resume finds
# --------------------------------------------------------------------------


def test_the_checkpoint_is_the_last_completed_round(ledger, oasis_db):
    simulate_interrupted_run(ledger, oasis_db)
    assert ledger.last_completed_round() == 2


def test_the_partial_round_was_never_checkpointed(ledger, oasis_db):
    """A checkpoint for an unfinished round would be a lie the resume trusts."""
    simulate_interrupted_run(ledger, oasis_db)
    assert [r.round for r in ledger.rounds()] == [0, 1, 2]


def test_the_half_applied_work_is_in_the_database(ledger, oasis_db):
    simulate_interrupted_run(ledger, oasis_db)
    assert "partial-a0" in posts(oasis_db)


# --------------------------------------------------------------------------
# Rolling back to the boundary
# --------------------------------------------------------------------------


def test_THE_HALF_APPLIED_ROUND_IS_DISCARDED(ledger, oasis_db):
    simulate_interrupted_run(ledger, oasis_db)
    ledger.rollback_to(ledger.checkpoint())

    remaining = posts(oasis_db)
    assert "partial-a0" not in remaining
    assert "partial-a1" not in remaining


def test_completed_rounds_are_untouched(ledger, oasis_db):
    simulate_interrupted_run(ledger, oasis_db)
    ledger.rollback_to(ledger.checkpoint())

    remaining = posts(oasis_db)
    assert "seed" in remaining
    assert "r1-a0" in remaining and "r2-a2" in remaining
    assert len(remaining) == 1 + 3 + 3


def test_the_trace_is_rolled_back_with_the_posts(ledger, oasis_db):
    """Otherwise the action counts would keep the dead round's actions."""
    simulate_interrupted_run(ledger, oasis_db)
    ledger.rollback_to(ledger.checkpoint())

    with sqlite3.connect(oasis_db) as connection:
        traces = [row[0] for row in connection.execute("SELECT info FROM trace")]
    assert "partial-a0" not in traces


def test_agents_survive_the_rollback(ledger, oasis_db):
    """They signed up before round one; removing them would orphan every post."""
    simulate_interrupted_run(ledger, oasis_db)
    ledger.rollback_to(ledger.checkpoint())

    with sqlite3.connect(oasis_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user").fetchone()[0] == 3


def test_resuming_twice_is_idempotent(ledger, oasis_db):
    """A resume that itself gets killed must leave the same clean state."""
    simulate_interrupted_run(ledger, oasis_db)
    ledger.rollback_to(ledger.checkpoint())
    first = posts(oasis_db)

    ledger.rollback_to(ledger.checkpoint())
    assert posts(oasis_db) == first


def test_a_run_killed_before_any_round_completed_starts_over(ledger, oasis_db):
    add_user(oasis_db, 0)
    act(oasis_db, 0, "partial-seed")

    assert ledger.checkpoint() is None
    assert ledger.last_completed_round() == -1, "the seed round has not completed"


def test_A_SEED_PUBLISHED_WITHOUT_A_CHECKPOINT_IS_ROLLED_BACK(ledger, oasis_db):
    """Killed between publishing the seed and recording round zero.

    Found by a flaky integration run: with no checkpoint the resume seeded
    again, so the event was announced twice and every agent saw it twice.
    """
    add_user(oasis_db, 0)
    act(oasis_db, 0, "seed")
    assert ledger.checkpoint() is None

    ledger.rollback_to(RoundRecord(round=-1, marks={}))
    assert posts(oasis_db) == [], "the unfinished seed must not survive"


def test_the_round_to_run_next_follows_the_checkpoint(ledger, oasis_db):
    simulate_interrupted_run(ledger, oasis_db, completed_rounds=2)
    assert ledger.last_completed_round() + 1 == 3


# --------------------------------------------------------------------------
# Re-running the interrupted round
# --------------------------------------------------------------------------


def test_the_reran_round_appears_once(ledger, oasis_db):
    simulate_interrupted_run(ledger, oasis_db)
    ledger.rollback_to(ledger.checkpoint())

    # The resumed process re-runs round 3, this time to completion.
    for user_id in range(3):
        act(oasis_db, user_id, f"r3-a{user_id}")
    ledger.record_round(RoundRecord(round=3, invoked=3, acted=3))

    assert [r.round for r in ledger.rounds()] == [0, 1, 2, 3]
    assert len([p for p in posts(oasis_db) if p.startswith("r3-")]) == 3


def test_no_agent_acts_twice_in_the_reran_round(ledger, oasis_db):
    """The failure the rollback exists to prevent."""
    simulate_interrupted_run(ledger, oasis_db)
    ledger.rollback_to(ledger.checkpoint())
    for user_id in range(3):
        act(oasis_db, user_id, f"r3-a{user_id}")
    ledger.record_round(RoundRecord(round=3))

    with sqlite3.connect(oasis_db) as connection:
        rows = connection.execute(
            "SELECT user_id, COUNT(*) FROM post WHERE content LIKE 'r3-%'"
            " GROUP BY user_id").fetchall()
    assert all(count == 1 for _, count in rows)


def test_round_attribution_is_intact_after_a_resume(ledger, oasis_db):
    simulate_interrupted_run(ledger, oasis_db)
    ledger.rollback_to(ledger.checkpoint())
    for user_id in range(3):
        act(oasis_db, user_id, f"r3-a{user_id}")
    ledger.record_round(RoundRecord(round=3))

    by_round = ledger.posts_by_round()
    assert len(by_round[0]) == 1
    assert len(by_round[3]) == 3
    assert sum(len(v) for v in by_round.values()) == len(posts(oasis_db))


# --------------------------------------------------------------------------
# Against a real process
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_real_run_killed_mid_flight_resumes(integration_config, tmp_path):
    """The whole mechanism, end to end: kill a live simulation and restart it."""
    import os
    import shutil
    import signal
    import tempfile
    import time
    from pathlib import Path

    from app.services.oasis_profiles import write_profiles
    from app.services.profile_generator import PersonaProfile
    from app.services.simulation_config_generator import SimulationConfig
    from app.services.simulation_manager import SimulationManager
    from app.services.simulation_store import SimulationState, SimulationStore

    # Short path: the control socket has a 107-character limit.
    base = Path(tempfile.mkdtemp(prefix="cs-", dir="/tmp"))
    try:
        settings = integration_config.model_copy(update={
            "MAX_CONCURRENT_SIMULATIONS": 2, "API_LLM_RESERVE": 1})
        store = SimulationStore(base / "simulations")
        manager = SimulationManager(store, config=settings)

        people = [
            PersonaProfile(name="Dawn Mercer", age=41, occupation="carpenter",
                           activity_level="high", gender="female",
                           country="United Kingdom", background="Runs a joinery."),
            PersonaProfile(name="Ray Nkemelu", age=33, occupation="bus driver",
                           activity_level="high", gender="male",
                           country="United Kingdom", background="Drives a bus."),
        ]
        meta = store.create(SimulationConfig.model_validate({
            "graph_id": "resume", "rounds": 3,
            "event": "The council published a draft housing density policy.",
            "broadcaster": {"name": "Riverbend Wire"},
            "seed_posts": [{"content": "Council publishes draft density policy."}],
        }))
        sim_dir = store.sim_dir(meta.sim_id)
        write_profiles(people, sim_dir / "profiles", default_country="United Kingdom")
        ledger = RunLedger(sim_dir / "simulation.db")

        record = manager.start(meta.sim_id)
        deadline = time.time() + 300
        while time.time() < deadline and ledger.last_completed_round() < 1:
            time.sleep(0.5)
        assert ledger.last_completed_round() >= 1, "no checkpoint was ever written"

        os.kill(record.pid, signal.SIGKILL)
        for _ in range(100):
            if not record.alive():
                break
            time.sleep(0.1)
        manager.running()
        assert store.load_meta(meta.sim_id).state == SimulationState.FAILED

        # Restarting a failed run is the Step 5 API's job; here, just unlock it.
        failed = store.load_meta(meta.sim_id)
        failed.state = SimulationState.DRAFT
        store.save_meta(failed)

        resumed = SimulationManager(store, config=settings)
        resumed.start(meta.sim_id)
        deadline = time.time() + 420
        while time.time() < deadline:
            if not resumed.status(meta.sim_id).get("running"):
                break
            time.sleep(1.0)

        rounds = [r.round for r in ledger.rounds()]
        assert rounds == [0, 1, 2, 3], f"expected every round exactly once, got {rounds}"
        assert len(rounds) == len(set(rounds)), "a round was duplicated"

        with sqlite3.connect(sim_dir / "simulation.db") as connection:
            users = connection.execute("SELECT COUNT(*) FROM user").fetchone()[0]
        assert users == 3, "agents were signed up twice"

        # Counted by round rather than by content: agents paraphrase the
        # announcement in their own posts, so text matching cannot tell a
        # duplicated seed from a population echoing it.
        assert len(ledger.posts_by_round()[0]) == 1, "the seed was published twice"
        assert store.load_meta(meta.sim_id).state == SimulationState.COMPLETE
    finally:
        shutil.rmtree(base, ignore_errors=True)
