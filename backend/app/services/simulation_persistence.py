"""Phase 6 Step 3 — round bookkeeping, checkpoints, and rollback.

OASIS already writes posts, comments, likes, follows and a trace of every action
to the run's SQLite database. What it does not record is **which round any of it
happened in** — there is no round column anywhere in its schema, and
``created_at`` is a sandbox clock that restarts at zero in a fresh process. A
report that cannot say when something happened cannot describe a trajectory,
which is most of what Phase 7 is for.

So rather than duplicating OASIS's data into tables of our own, this records the
**boundaries between rounds**: the high-water mark of every table at the moment
each round finished. Every id in the schema is a monotonic rowid, so a row
belongs to the round whose range contains it. One extra table, no duplication,
and OASIS's own tables stay the source of truth.

The same marks make rollback exact. A run killed mid-round leaves that round
half-applied — some agents acted, others never got their turn — and simply
continuing would bake a permanently lopsided round into the data. Deleting
everything above the last completed round's marks puts the database back to a
round boundary, so the interrupted round can be re-run cleanly and "resumes
without duplicating rounds" means what it says.

Denormalised counters (``post.num_likes`` and friends) are recomputed after a
rollback. Deleting a like row does not decrement the counter it fed, and a
resumed run whose like counts disagree with its like rows would mislead every
downstream analysis.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "ROLLBACK_TABLES",
    "ROUND_TABLE",
    "RoundRecord",
    "RunLedger",
]

ROUND_TABLE = "crowdsight_round"

#: SQL cannot parameterise a table name. Every one used here comes from
#: ROLLBACK_TABLES or a caller inside this package, and is checked before use.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(name: str) -> str:
    if not _IDENTIFIER.match(name or ""):
        raise ValueError(f"Not a usable SQL identifier: {name!r}")
    return name

#: Tables holding per-round activity, in dependency order for deletion.
#:
#: ``user`` is excluded: agents sign up once, before round one, and removing
#: them would orphan every post. ``rec`` is excluded because OASIS rebuilds it
#: at the start of every round anyway. ``product`` belongs to a scenario we do
#: not run.
ROLLBACK_TABLES: tuple[str, ...] = (
    "comment_like", "comment_dislike", "comment",
    "like", "dislike", "report", "post",
    "follow", "mute",
    "group_messages", "group_members", "chat_group",
    "trace",
)

CREATE_ROUND_TABLE = f"""
CREATE TABLE IF NOT EXISTS {ROUND_TABLE} (
    round        INTEGER PRIMARY KEY,
    started_at   TEXT,
    ended_at     TEXT,
    invoked      INTEGER DEFAULT 0,
    acted        INTEGER DEFAULT 0,
    failed       INTEGER DEFAULT 0,
    skipped      INTEGER DEFAULT 0,
    events_fired INTEGER DEFAULT 0,
    action_counts TEXT DEFAULT '{{}}',
    marks        TEXT DEFAULT '{{}}',
    failures     TEXT DEFAULT '[]'
)
"""


@dataclass
class RoundRecord:
    """One completed round, and the checkpoint it represents."""

    round: int
    started_at: str = ""
    ended_at: str = ""
    invoked: int = 0
    acted: int = 0
    failed: int = 0
    skipped: int = 0
    events_fired: int = 0
    action_counts: dict[str, int] = field(default_factory=dict)
    marks: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round, "started_at": self.started_at,
            "ended_at": self.ended_at, "invoked": self.invoked,
            "acted": self.acted, "failed": self.failed, "skipped": self.skipped,
            "events_fired": self.events_fired, "action_counts": self.action_counts,
            "failures": self.failures[:20],
        }


class RunLedger:
    """Round boundaries and checkpoints for one run's database."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)

    # -- connection ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def ensure_schema(self) -> None:
        """Add our table to the run's database, and put it in WAL mode.

        Two processes write this file: the OASIS engine as agents act, and this
        ledger as rounds are checkpointed. SQLite's default rollback journal
        makes a writer block *everything* for the length of its transaction, so
        a checkpoint can sit behind a round's worth of engine writes and give
        up — `sqlite3.OperationalError: database is locked` mid-run, losing the
        round boundary that resume depends on. It happened twice in one
        afternoon on a loaded machine.

        WAL lets readers and the writer proceed together and shortens the
        windows in which anyone is blocked. The setting is stored in the file,
        so it survives every later connection including the engine's own.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            # Not inside a transaction: journal_mode is a no-op within one.
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                logger.warning(
                    "Could not put %s into WAL mode (got %r); concurrent "
                    "checkpoints may contend with the engine", self.path, mode)
            connection.execute(CREATE_ROUND_TABLE)

    # -- marks --------------------------------------------------------------

    def _tables(self, connection: sqlite3.Connection) -> set[str]:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def marks(self) -> dict[str, int]:
        """The current high-water rowid of every tracked table."""
        with self._connect() as connection:
            present = self._tables(connection)
            out: dict[str, int] = {}
            for table in ROLLBACK_TABLES:
                if table not in present:
                    continue
                row = connection.execute(
                    f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()
                out[table] = int(row[0])
            return out

    # -- recording ----------------------------------------------------------

    def action_counts(self, since: dict[str, int]) -> dict[str, int]:
        """What the agents actually did, from OASIS's own trace table.

        Counted rather than inferred: an agent may take several actions in one
        turn, or none, and the trace is the only record of what really landed.
        """
        with self._connect() as connection:
            if "trace" not in self._tables(connection):
                return {}
            rows = connection.execute(
                "SELECT action, COUNT(*) FROM trace WHERE rowid > ? GROUP BY action",
                (since.get("trace", 0),),
            ).fetchall()
        return {str(action): int(count) for action, count in rows if action}

    def record_round(self, record: RoundRecord) -> RoundRecord:
        """Write the checkpoint for a completed round."""
        self.ensure_schema()
        record.marks = record.marks or self.marks()
        with self._connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO {ROUND_TABLE} "
                "(round, started_at, ended_at, invoked, acted, failed, skipped, "
                " events_fired, action_counts, marks, failures) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.round, record.started_at, record.ended_at, record.invoked,
                 record.acted, record.failed, record.skipped, record.events_fired,
                 json.dumps(record.action_counts), json.dumps(record.marks),
                 json.dumps(record.failures[:50])),
            )
        logger.info("Checkpoint written for round %d: %s", record.round,
                    record.action_counts or "no actions")
        return record

    # -- reading ------------------------------------------------------------

    def rounds(self) -> list[RoundRecord]:
        if not self.path.is_file():
            return []
        with self._connect() as connection:
            if ROUND_TABLE not in self._tables(connection):
                return []
            rows = connection.execute(
                f"SELECT * FROM {ROUND_TABLE} ORDER BY round").fetchall()
        return [self._to_record(row) for row in rows]

    @staticmethod
    def _to_record(row: sqlite3.Row) -> RoundRecord:
        return RoundRecord(
            round=row["round"], started_at=row["started_at"] or "",
            ended_at=row["ended_at"] or "", invoked=row["invoked"],
            acted=row["acted"], failed=row["failed"], skipped=row["skipped"],
            events_fired=row["events_fired"],
            action_counts=json.loads(row["action_counts"] or "{}"),
            marks=json.loads(row["marks"] or "{}"),
            failures=json.loads(row["failures"] or "[]"),
        )

    def last_completed_round(self) -> int:
        """The checkpoint to resume from. ``-1`` when nothing has run.

        Round 0 is the seed, so it is a real checkpoint and must be
        distinguishable from "no rounds at all".
        """
        records = self.rounds()
        return records[-1].round if records else -1

    def checkpoint(self) -> RoundRecord | None:
        records = self.rounds()
        return records[-1] if records else None

    def progress(self, total_rounds: int) -> dict[str, Any]:
        """Structured progress, as the spec asks: rounds, counts, agents active."""
        records = self.rounds()
        totals: dict[str, int] = {}
        for record in records:
            for action, count in record.action_counts.items():
                totals[action] = totals.get(action, 0) + count
        last = records[-1] if records else None
        return {
            "round": last.round if last else 0,
            "total_rounds": total_rounds,
            "rounds_completed": max(0, len(records) - 1),  # round 0 is the seed
            "agents_active": last.acted if last else 0,
            "agents_skipped": last.skipped if last else 0,
            "agents_failed": last.failed if last else 0,
            "action_counts": totals,
            "last_round_actions": last.action_counts if last else {},
        }

    # -- rollback -----------------------------------------------------------

    def rollback_to(self, record: RoundRecord) -> dict[str, int]:
        """Delete everything written after ``record``, returning what was removed.

        This is what makes an interrupted round safe to re-run. Without it the
        agents who acted before the crash keep their actions and the rest never
        get a turn, and no amount of care later can tell the two apart.
        """
        removed: dict[str, int] = {}
        with self._connect() as connection:
            present = self._tables(connection)
            for table in ROLLBACK_TABLES:
                if table not in present:
                    continue
                mark = record.marks.get(table)
                if mark is None:
                    # No mark recorded for a table that exists: it was empty at
                    # the checkpoint, so everything in it came afterwards.
                    mark = 0
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE rowid > ?", (mark,))
                if cursor.rowcount > 0:
                    removed[table] = cursor.rowcount

            connection.execute(
                f"DELETE FROM {ROUND_TABLE} WHERE round > ?", (record.round,))
            self._recount(connection, present)

        if removed:
            logger.warning("Rolled back to round %d, discarding %s",
                           record.round, removed)
        return removed

    @staticmethod
    def _recount(connection: sqlite3.Connection, present: set[str]) -> None:
        """Rebuild denormalised counters from the rows that remain.

        Deleting a like does not decrement ``post.num_likes``; left alone, a
        resumed run's counters would disagree with its own rows.
        """
        if "post" in present:
            if "like" in present:
                connection.execute(
                    "UPDATE post SET num_likes = "
                    "(SELECT COUNT(*) FROM like WHERE like.post_id = post.post_id)")
            if "dislike" in present:
                connection.execute(
                    "UPDATE post SET num_dislikes = (SELECT COUNT(*) FROM dislike "
                    "WHERE dislike.post_id = post.post_id)")
            connection.execute(
                "UPDATE post SET num_shares = (SELECT COUNT(*) FROM post AS child "
                "WHERE child.original_post_id = post.post_id)")
        if "comment" in present:
            if "comment_like" in present:
                connection.execute(
                    "UPDATE comment SET num_likes = (SELECT COUNT(*) FROM comment_like "
                    "WHERE comment_like.comment_id = comment.comment_id)")
            if "comment_dislike" in present:
                connection.execute(
                    "UPDATE comment SET num_dislikes = (SELECT COUNT(*) FROM "
                    "comment_dislike WHERE "
                    "comment_dislike.comment_id = comment.comment_id)")

    # -- attribution --------------------------------------------------------

    def rows_by_round(self, table: str, id_column: str) -> dict[int, list[int]]:
        """Which rows of ``table`` belong to which round.

        The general form of round attribution: OASIS stamps no round on
        anything, so a row's round is the one whose recorded rowid range
        contains it. Posts, comments and traces all work this way.
        """
        records = self.rounds()
        if not records or not self.path.is_file():
            return {}
        with self._connect() as connection:
            if table not in self._tables(connection):
                return {}
            table, id_column = _identifier(table), _identifier(id_column)
            rows = connection.execute(
                f"SELECT rowid AS rid, {id_column} AS ident FROM {table}").fetchall()

        out: dict[int, list[int]] = {}
        previous = 0
        for record in records:
            mark = record.marks.get(table, previous)
            out[record.round] = [int(r["ident"]) for r in rows
                                 if previous < int(r["rid"]) <= mark]
            previous = mark
        return out

    def posts_by_round(self) -> dict[int, list[int]]:
        """Which posts belong to which round, from the recorded boundaries."""
        return self.rows_by_round("post", "post_id")

    def comments_by_round(self) -> dict[int, list[int]]:
        """Which comments belong to which round."""
        return self.rows_by_round("comment", "comment_id")

    def actions_by_round(self) -> dict[int, list[int]]:
        """Which trace rows — that is, which agent actions — belong to which round."""
        return self.rows_by_round("trace", "rowid")
