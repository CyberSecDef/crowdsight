"""Phase 6 Step 3 — round attribution and checkpoints.

OASIS records what happened but never *when*: there is no round column in its
schema, and `created_at` is a sandbox clock that restarts at zero in a fresh
process. Attribution is therefore ours, and it is derived from the high-water
marks recorded at each round boundary rather than by duplicating OASIS's rows.

These build a database with OASIS's real schema, because the marks depend on
every table having a monotonic rowid — a property of the schema, not of our
code, and one a version bump could quietly remove.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.services.simulation_persistence import (
    ROLLBACK_TABLES,
    ROUND_TABLE,
    RoundRecord,
    RunLedger,
)


@pytest.fixture
def oasis_db(tmp_path):
    """A database with OASIS's own schema, created the way OASIS creates it."""
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


def add_user(path, user_id, name):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO user (user_id, agent_id, user_name, name, bio, created_at,"
            " num_followings, num_followers) VALUES (?, ?, ?, ?, '', '0', 0, 0)",
            (user_id, user_id, name, name))


def add_post(path, user_id, content, created_at="0"):
    with sqlite3.connect(path) as connection:
        cursor = connection.execute(
            "INSERT INTO post (user_id, content, created_at) VALUES (?, ?, ?)",
            (user_id, content, created_at))
        return cursor.lastrowid


_trace_seq = iter(range(1, 10_000))


def add_trace(path, user_id, action, info=None, created_at=None):
    """One trace row.

    OASIS's trace table is keyed on (user_id, created_at, action, info), so two
    identical rows collide. Real rows differ by timestamp and payload; these
    are made unique the same way.
    """
    tick = next(_trace_seq)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO trace (user_id, created_at, action, info) VALUES (?, ?, ?, ?)",
            (user_id, created_at or str(tick), action, info or f'{{"n": {tick}}}'))


def add_like(path, post_id, user_id):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO like (post_id, user_id, created_at) VALUES (?, ?, '0')",
            (post_id, user_id))
        connection.execute(
            "UPDATE post SET num_likes = num_likes + 1 WHERE post_id = ?", (post_id,))


# --------------------------------------------------------------------------
# The schema assumption everything rests on
# --------------------------------------------------------------------------


@pytest.mark.parametrize("table", ROLLBACK_TABLES)
def test_every_tracked_table_has_a_usable_rowid(oasis_db, table):
    """Round attribution is by rowid range. WITHOUT ROWID would break it silently."""
    with sqlite3.connect(oasis_db) as connection:
        present = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in present:
            pytest.skip(f"{table} is not in this OASIS version")
        connection.execute(f"SELECT rowid FROM {table} LIMIT 1")


def test_our_table_does_not_collide_with_oasis(oasis_db, ledger):
    with sqlite3.connect(oasis_db) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert ROUND_TABLE in tables
    assert ROUND_TABLE.startswith("crowdsight_"), "namespaced away from OASIS's own"


def test_the_schema_can_be_applied_twice(ledger):
    ledger.ensure_schema()
    ledger.ensure_schema()


# --------------------------------------------------------------------------
# Marks and checkpoints
# --------------------------------------------------------------------------


def test_marks_start_at_zero_on_an_empty_run(ledger):
    assert ledger.marks()["post"] == 0


def test_marks_follow_the_data(ledger, oasis_db):
    add_user(oasis_db, 0, "dawn")
    add_post(oasis_db, 0, "hello")
    add_post(oasis_db, 0, "again")
    assert ledger.marks()["post"] == 2


def test_a_checkpoint_records_the_round_and_its_marks(ledger, oasis_db):
    add_user(oasis_db, 0, "dawn")
    add_post(oasis_db, 0, "hello")
    record = ledger.record_round(RoundRecord(round=0, invoked=1, acted=1))

    assert record.marks["post"] == 1
    stored = ledger.rounds()
    assert [r.round for r in stored] == [0]
    assert stored[0].acted == 1


def test_no_rounds_means_no_checkpoint(ledger):
    assert ledger.checkpoint() is None
    assert ledger.last_completed_round() == -1


def test_the_seed_round_is_a_real_checkpoint(ledger):
    """Round 0 is the seed, and must be distinguishable from 'nothing ran'."""
    ledger.record_round(RoundRecord(round=0))
    assert ledger.last_completed_round() == 0
    assert ledger.checkpoint().round == 0


def test_checkpoints_accumulate_in_order(ledger):
    for index in range(4):
        ledger.record_round(RoundRecord(round=index))
    assert [r.round for r in ledger.rounds()] == [0, 1, 2, 3]
    assert ledger.last_completed_round() == 3


def test_recording_the_same_round_twice_replaces_it(ledger):
    ledger.record_round(RoundRecord(round=1, acted=2))
    ledger.record_round(RoundRecord(round=1, acted=5))
    assert len(ledger.rounds()) == 1
    assert ledger.rounds()[0].acted == 5


def test_a_ledger_on_a_missing_database_is_empty(tmp_path):
    assert RunLedger(tmp_path / "nothing.db").rounds() == []


# --------------------------------------------------------------------------
# Action counts, from OASIS's own trace
# --------------------------------------------------------------------------


def test_actions_are_counted_from_the_trace(ledger, oasis_db):
    add_user(oasis_db, 0, "dawn")
    before = ledger.marks()
    add_trace(oasis_db, 0, "create_post")
    add_trace(oasis_db, 0, "like_post")
    add_trace(oasis_db, 0, "like_post")

    assert ledger.action_counts(before) == {"create_post": 1, "like_post": 2}


def test_only_the_round_in_question_is_counted(ledger, oasis_db):
    add_user(oasis_db, 0, "dawn")
    add_trace(oasis_db, 0, "create_post")
    after_first = ledger.marks()
    add_trace(oasis_db, 0, "repost")

    assert ledger.action_counts(after_first) == {"repost": 1}


def test_a_round_where_nobody_acted_counts_nothing(ledger, oasis_db):
    assert ledger.action_counts(ledger.marks()) == {}


# --------------------------------------------------------------------------
# Round attribution
# --------------------------------------------------------------------------


def test_posts_are_attributed_to_the_round_that_produced_them(ledger, oasis_db):
    add_user(oasis_db, 0, "dawn")
    seed = add_post(oasis_db, 0, "seed")
    ledger.record_round(RoundRecord(round=0))

    first = add_post(oasis_db, 0, "round one")
    ledger.record_round(RoundRecord(round=1))

    second = add_post(oasis_db, 0, "round two a")
    third = add_post(oasis_db, 0, "round two b")
    ledger.record_round(RoundRecord(round=2))

    by_round = ledger.posts_by_round()
    assert by_round[0] == [seed]
    assert by_round[1] == [first]
    assert by_round[2] == [second, third]


def test_a_round_with_no_posts_attributes_none(ledger, oasis_db):
    add_user(oasis_db, 0, "dawn")
    add_post(oasis_db, 0, "seed")
    ledger.record_round(RoundRecord(round=0))
    ledger.record_round(RoundRecord(round=1))

    assert ledger.posts_by_round()[1] == []


# --------------------------------------------------------------------------
# Structured progress
# --------------------------------------------------------------------------


def test_progress_reports_what_the_spec_asks_for(ledger):
    ledger.record_round(RoundRecord(round=0, action_counts={"create_post": 1}))
    ledger.record_round(RoundRecord(round=1, invoked=10, acted=8, failed=1, skipped=5,
                                    action_counts={"like_post": 4, "create_post": 2}))

    progress = ledger.progress(total_rounds=6)
    assert progress["round"] == 1
    assert progress["total_rounds"] == 6
    assert progress["agents_active"] == 8
    assert progress["agents_skipped"] == 5
    assert progress["agents_failed"] == 1
    assert progress["action_counts"] == {"create_post": 3, "like_post": 4}


def test_progress_on_a_run_that_has_not_started(ledger):
    progress = ledger.progress(total_rounds=6)
    assert progress["round"] == 0
    assert progress["action_counts"] == {}


def test_the_seed_round_is_not_counted_as_a_simulated_round(ledger):
    ledger.record_round(RoundRecord(round=0))
    assert ledger.progress(total_rounds=6)["rounds_completed"] == 0
    ledger.record_round(RoundRecord(round=1))
    assert ledger.progress(total_rounds=6)["rounds_completed"] == 1


# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------


def test_rollback_removes_only_what_came_after_the_checkpoint(ledger, oasis_db):
    add_user(oasis_db, 0, "dawn")
    kept = add_post(oasis_db, 0, "before the crash")
    add_trace(oasis_db, 0, "create_post")
    checkpoint = ledger.record_round(RoundRecord(round=1))

    add_post(oasis_db, 0, "half-applied")
    add_trace(oasis_db, 0, "create_post")

    removed = ledger.rollback_to(checkpoint)

    assert removed == {"post": 1, "trace": 1}
    with sqlite3.connect(oasis_db) as connection:
        remaining = [row[0] for row in connection.execute("SELECT post_id FROM post")]
    assert remaining == [kept]


def test_rollback_leaves_the_users_alone(ledger, oasis_db):
    """Deleting signups would orphan every post that survived."""
    add_user(oasis_db, 0, "dawn")
    checkpoint = ledger.record_round(RoundRecord(round=0))
    add_user(oasis_db, 1, "ray")

    ledger.rollback_to(checkpoint)
    with sqlite3.connect(oasis_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user").fetchone()[0] == 2


def test_rollback_discards_later_checkpoints(ledger):
    checkpoint = ledger.record_round(RoundRecord(round=1))
    ledger.record_round(RoundRecord(round=2))

    ledger.rollback_to(checkpoint)
    assert [r.round for r in ledger.rounds()] == [1]


def test_ROLLBACK_REBUILDS_DENORMALISED_COUNTERS(ledger, oasis_db):
    """Deleting a like does not decrement the counter it fed."""
    add_user(oasis_db, 0, "dawn")
    add_user(oasis_db, 1, "ray")
    post_id = add_post(oasis_db, 0, "hello")
    add_like(oasis_db, post_id, 1)
    checkpoint = ledger.record_round(RoundRecord(round=1))

    add_like(oasis_db, post_id, 0)
    with sqlite3.connect(oasis_db) as connection:
        assert connection.execute(
            "SELECT num_likes FROM post WHERE post_id = ?", (post_id,)).fetchone()[0] == 2

    ledger.rollback_to(checkpoint)
    with sqlite3.connect(oasis_db) as connection:
        likes = connection.execute("SELECT COUNT(*) FROM like").fetchone()[0]
        counter = connection.execute(
            "SELECT num_likes FROM post WHERE post_id = ?", (post_id,)).fetchone()[0]
    assert likes == 1
    assert counter == 1, "the counter must agree with the rows that remain"


def test_rollback_with_nothing_to_remove_is_quiet(ledger, oasis_db):
    add_user(oasis_db, 0, "dawn")
    checkpoint = ledger.record_round(RoundRecord(round=1))
    assert ledger.rollback_to(checkpoint) == {}


def test_a_table_absent_from_the_checkpoint_is_cleared(ledger, oasis_db):
    """A table empty at the checkpoint has no mark; everything in it is new."""
    add_user(oasis_db, 0, "dawn")
    checkpoint = RoundRecord(round=1, marks={"post": 0})
    add_post(oasis_db, 0, "after")
    add_trace(oasis_db, 0, "create_post")

    ledger.rollback_to(checkpoint)
    with sqlite3.connect(oasis_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM trace").fetchone()[0] == 0


def test_marks_survive_a_round_trip_as_json(ledger, oasis_db):
    add_user(oasis_db, 0, "dawn")
    add_post(oasis_db, 0, "hello")
    ledger.record_round(RoundRecord(round=1))

    with sqlite3.connect(oasis_db) as connection:
        raw = connection.execute(
            f"SELECT marks FROM {ROUND_TABLE} WHERE round = 1").fetchone()[0]
    assert json.loads(raw)["post"] == 1
