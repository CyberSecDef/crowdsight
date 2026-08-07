"""Phase 7 Step 1 — reading a run, live or finished.

Everything here answers from the run's own SQLite database and the round
boundaries Step 3 of Phase 6 recorded. That matters more than it sounds:

**A finished run has no worker to ask.** Most of a run's life is *after* it
ends — that is when a report is written and the results are picked over — so the
only source that always exists is the database. Reading it first means one code
path serves a live run and a year-old one alike.

**A live run is enriched, not depended on.** When the store says a simulation is
running, the worker is asked for its in-flight stage over the control socket, on
a short timeout. If it does not answer, the answer is still returned, marked
stale, rather than a poll hanging for as long as the worker is wedged. A UI
polling every second must never be slower than its own interval.

**The database is being written while it is read.** OASIS holds its own
connection and writes throughout a round. Reads therefore use a busy timeout and
tolerate contention rather than assuming exclusive access.

**OASIS indexes nothing but its primary keys.** Per-agent aggregation over tens
of thousands of rows means a full scan of every table, per request, which is too
slow to poll. The missing indexes are created on first read — additive only,
touching no data and no existing index, and cheap enough to be worth it.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.services.simulation_persistence import RunLedger

logger = logging.getLogger(__name__)

__all__ = ["RunReader", "RunNotReadable"]

#: How long a read waits for a writing worker before giving up.
BUSY_TIMEOUT_MS = 5_000

#: Indexes OASIS does not create. Every per-agent query needs one.
INDEXES: tuple[tuple[str, str, str], ...] = (
    ("crowdsight_post_user", "post", "user_id"),
    ("crowdsight_comment_user", "comment", "user_id"),
    ("crowdsight_comment_post", "comment", "post_id"),
    ("crowdsight_like_user", "like", "user_id"),
    ("crowdsight_like_post", "like", "post_id"),
    ("crowdsight_dislike_user", "dislike", "user_id"),
    ("crowdsight_trace_user", "trace", "user_id"),
    ("crowdsight_follow_followee", "follow", "followee_id"),
    ("crowdsight_follow_follower", "follow", "follower_id"),
)

MAX_PAGE = 500

#: SQL cannot parameterise an identifier, so table and column names are
#: interpolated. Every one comes from a constant in this module and is checked
#: against this pattern before use, so no caller-supplied string can reach a
#: query. The same reasoning as `escape_identifier` in the Neo4j layer: validate
#: rather than quote, and refuse anything that is not plainly an identifier.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def identifier(name: str) -> str:
    """Return ``name`` if it is a bare SQL identifier, or raise."""
    if not _IDENTIFIER.match(name or ""):
        raise ValueError(f"Not a usable SQL identifier: {name!r}")
    return name


class RunNotReadable(LookupError):
    """The run has no database yet, or it cannot be opened."""


@dataclass
class AgentIdentity:
    """Who an agent is, from our own record rather than the lossy OASIS files."""

    user_id: int
    username: str = ""
    name: str = ""
    provenance: str = ""
    occupation: str = ""
    activity_level: str = ""
    #: False for the broadcaster: it posts, but it is not a member of the public
    #: and must not land in sentiment or influence statistics as though it were.
    population: bool = True


class RunReader:
    """Read-only access to one simulation's results."""

    def __init__(self, sim_dir: str | Path) -> None:
        self.sim_dir = Path(sim_dir)
        self.database_path = self.sim_dir / "simulation.db"
        self.ledger = RunLedger(self.database_path)
        self._indexed = False

    # -- plumbing -----------------------------------------------------------

    @property
    def exists(self) -> bool:
        return self.database_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        if not self.exists:
            raise RunNotReadable(
                f"{self.database_path} does not exist; the run has not started")
        connection = sqlite3.connect(self.database_path, timeout=BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return connection

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def ensure_indexes(self) -> int:
        """Add the indexes OASIS omits. Additive, idempotent, best-effort.

        Skipped silently if the database is locked: a missing index makes a
        query slow, and failing a status poll because a round is mid-write
        would be the worse outcome.
        """
        if self._indexed or not self.exists:
            return 0
        created = 0
        try:
            with self._connect() as connection:
                present = self._tables(connection)
                for name, table, column in INDEXES:
                    if table not in present:
                        continue
                    # cypher-audit: ok — SQLite DDL, not Cypher, and SQL cannot
                    # parameterise an index or table name. All three come from
                    # the INDEXES constant above and are validated as bare
                    # identifiers, so nothing a caller supplies can get in here.
                    connection.execute(
                        f"CREATE INDEX IF NOT EXISTS {identifier(name)} "
                        f"ON {identifier(table)}({identifier(column)})")
                    created += 1
        except sqlite3.Error as exc:
            logger.debug("Could not index %s: %s", self.database_path, exc)
            return 0
        self._indexed = True
        return created

    # -- identities ---------------------------------------------------------

    def identities(self) -> dict[int, AgentIdentity]:
        """Every agent in the run, by OASIS id.

        Built from our own ``profiles.json`` where possible, falling back to the
        ``user`` table. Anyone in the run but not in the population file is the
        broadcaster, and is flagged as such.
        """
        out: dict[int, AgentIdentity] = {}
        path = self.sim_dir / "profiles" / "profiles.json"
        if path.is_file():
            try:
                for entry in json.loads(path.read_text(encoding="utf-8")):
                    user_id = int(entry.get("user_id", -1))
                    if user_id < 0:
                        continue
                    out[user_id] = AgentIdentity(
                        user_id=user_id,
                        username=str(entry.get("username") or ""),
                        name=str(entry.get("name") or ""),
                        provenance=str(entry.get("provenance") or ""),
                        occupation=str(entry.get("occupation") or ""),
                        activity_level=str(entry.get("activity_level") or ""),
                        population=True,
                    )
            except (ValueError, OSError) as exc:
                logger.warning("Unreadable population file for %s: %s",
                               self.sim_dir.name, exc)

        if not self.exists:
            return out
        try:
            with self._connect() as connection:
                if "user" not in self._tables(connection):
                    return out
                for row in connection.execute(
                        "SELECT user_id, user_name, name FROM user"):
                    user_id = int(row["user_id"])
                    if user_id in out:
                        out[user_id].username = (out[user_id].username
                                                 or str(row["user_name"] or ""))
                        continue
                    out[user_id] = AgentIdentity(
                        user_id=user_id,
                        username=str(row["user_name"] or ""),
                        name=str(row["name"] or ""),
                        provenance="broadcaster",
                        population=False,
                    )
        except sqlite3.Error as exc:
            logger.debug("Could not read identities: %s", exc)
        return out

    # -- status -------------------------------------------------------------

    def status(
        self,
        *,
        meta: Any,
        total_rounds: int,
        live: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Where the run has got to. Answers whether or not a worker is alive."""
        progress = self.ledger.progress(total_rounds) if self.exists else {}
        completed = int(progress.get("rounds_completed", 0))
        percent = round(100.0 * completed / total_rounds, 1) if total_rounds else 0.0

        payload: dict[str, Any] = {
            "sim_id": self.sim_dir.name,
            "state": getattr(meta, "state", "unknown"),
            "round": int(progress.get("round", 0)),
            "rounds_completed": completed,
            "total_rounds": total_rounds,
            "percent": min(percent, 100.0),
            "action_counts": progress.get("action_counts", {}),
            "last_round_actions": progress.get("last_round_actions", {}),
            "agents": {
                "active_last_round": progress.get("agents_active", 0),
                "skipped_last_round": progress.get("agents_skipped", 0),
                "failed_last_round": progress.get("agents_failed", 0),
            },
            "started_at": getattr(meta, "started_at", ""),
            "finished_at": getattr(meta, "finished_at", ""),
            "has_data": self.exists,
        }

        # Live fields are an enrichment, never a dependency: a wedged worker
        # must not make a status poll hang or fail.
        if live is None:
            payload["live"] = None
        elif live.get("unreachable"):
            payload["live"] = None
            payload["live_stale"] = True
            payload["live_error"] = live["unreachable"]
        else:
            payload["live"] = {
                "running": bool(live.get("running")),
                "stage": live.get("stage", ""),
                "round_in_flight": live.get("round", 0),
                "pid": live.get("pid"),
                "stop_requested": live.get("stop_requested", False),
            }
            payload["live_stale"] = False
        return payload

    # -- detail -------------------------------------------------------------

    def recent_actions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The most recent agent actions, newest first.

        Read from OASIS's trace table, which is the only record of what an
        agent actually did as opposed to what it was asked to do.
        """
        limit = max(1, min(int(limit), MAX_PAGE))
        if not self.exists:
            return []
        self.ensure_indexes()
        rounds = self._round_bounds("trace")
        identities = self.identities()
        try:
            with self._connect() as connection:
                if "trace" not in self._tables(connection):
                    return []
                rows = connection.execute(
                    "SELECT rowid AS rid, user_id, created_at, action, info "
                    "FROM trace ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.Error as exc:
            raise RunNotReadable(f"Could not read the action log: {exc}") from exc

        out = []
        for row in rows:
            identity = identities.get(int(row["user_id"]))
            out.append({
                "user_id": int(row["user_id"]),
                "username": identity.username if identity else "",
                "name": identity.name if identity else "",
                "population": identity.population if identity else True,
                "action": row["action"],
                "round": self._round_of(int(row["rid"]), rounds),
                "created_at": row["created_at"],
                "info": _maybe_json(row["info"]),
            })
        return out

    # -- timeline -----------------------------------------------------------

    def timeline(
        self, *, from_round: int | None = None, to_round: int | None = None
    ) -> list[dict[str, Any]]:
        """Per-round aggregates, optionally over a range."""
        records = self.ledger.rounds() if self.exists else []
        posts = self.ledger.posts_by_round() if self.exists else {}
        comments = self.ledger.comments_by_round() if self.exists else {}

        out = []
        for record in records:
            if from_round is not None and record.round < from_round:
                continue
            if to_round is not None and record.round > to_round:
                continue
            out.append({
                "round": record.round,
                "seed": record.round == 0,
                "invoked": record.invoked,
                "acted": record.acted,
                "failed": record.failed,
                "skipped": record.skipped,
                "events_fired": record.events_fired,
                "action_counts": record.action_counts,
                "posts": len(posts.get(record.round, [])),
                "comments": len(comments.get(record.round, [])),
                "ended_at": record.ended_at,
                "failures": record.failures[:5],
            })
        return out

    # -- per agent ----------------------------------------------------------

    def agent_stats(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        sort: str = "actions",
        population_only: bool = False,
    ) -> dict[str, Any]:
        """Per-agent activity across the whole run.

        Aggregated in SQL rather than in Python: a three-hundred agent run holds
        tens of thousands of rows, and pulling them into the API process to
        count them would be slower than the query and far heavier on memory.
        """
        if not self.exists:
            raise RunNotReadable("The run has no database yet")
        limit = max(1, min(int(limit), MAX_PAGE))
        offset = max(0, int(offset))
        self.ensure_indexes()
        identities = self.identities()

        try:
            with self._connect() as connection:
                present = self._tables(connection)
                counts = {
                    "posts": self._count_by(connection, present, "post", "user_id"),
                    "comments": self._count_by(connection, present, "comment", "user_id"),
                    "likes_given": self._count_by(connection, present, "like", "user_id"),
                    "dislikes_given": self._count_by(connection, present, "dislike",
                                                     "user_id"),
                    "actions": self._count_by(connection, present, "trace", "user_id"),
                    "following": self._count_by(connection, present, "follow",
                                                "follower_id"),
                    "followers": self._count_by(connection, present, "follow",
                                                "followee_id"),
                }
                received = self._engagement_received(connection, present)
        except sqlite3.Error as exc:
            raise RunNotReadable(f"Could not read agent statistics: {exc}") from exc

        rows = []
        for user_id, identity in sorted(identities.items()):
            if population_only and not identity.population:
                continue
            entry = {
                "user_id": user_id,
                "username": identity.username,
                "name": identity.name,
                "provenance": identity.provenance,
                "occupation": identity.occupation,
                "activity_level": identity.activity_level,
                "population": identity.population,
                **{key: table.get(user_id, 0) for key, table in counts.items()},
                "likes_received": received.get(user_id, {}).get("likes", 0),
                "reposts_received": received.get(user_id, {}).get("reposts", 0),
            }
            entry["engagement_received"] = (entry["likes_received"]
                                            + entry["reposts_received"])
            rows.append(entry)

        key = sort if sort in {"actions", "posts", "comments", "likes_given",
                               "followers", "engagement_received",
                               "user_id"} else "actions"
        rows.sort(key=lambda r: (-int(r.get(key, 0)), r["user_id"])
                  if key != "user_id" else (r["user_id"], 0))

        window = rows[offset:offset + limit]
        return {
            "sim_id": self.sim_dir.name,
            "total": len(rows),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(window) < len(rows),
            "sort": key,
            "agents": window,
            "silent": sum(1 for r in rows if r["population"] and not r["actions"]),
        }

    @staticmethod
    def _count_by(connection: sqlite3.Connection, present: set[str],
                  table: str, column: str) -> dict[int, int]:
        if table not in present:
            return {}
        table, column = identifier(table), identifier(column)
        return {int(row[0]): int(row[1]) for row in connection.execute(
            f"SELECT {column}, COUNT(*) FROM {table} "
            f"WHERE {column} IS NOT NULL GROUP BY {column}")}

    @staticmethod
    def _engagement_received(connection: sqlite3.Connection,
                             present: set[str]) -> dict[int, dict[str, int]]:
        """What each agent's posts drew, as opposed to what the agent did."""
        if "post" not in present:
            return {}
        out: dict[int, dict[str, int]] = {}
        for row in connection.execute(
                "SELECT user_id, SUM(COALESCE(num_likes, 0)) AS likes, "
                "SUM(COALESCE(num_shares, 0)) AS reposts FROM post "
                "WHERE user_id IS NOT NULL GROUP BY user_id"):
            out[int(row["user_id"])] = {"likes": int(row["likes"] or 0),
                                        "reposts": int(row["reposts"] or 0)}
        return out

    # -- round attribution --------------------------------------------------

    def _round_bounds(self, table: str) -> list[tuple[int, int, int]]:
        """``(round, lower_exclusive, upper_inclusive)`` rowid ranges."""
        bounds = []
        previous = 0
        for record in self.ledger.rounds():
            mark = record.marks.get(table, previous)
            bounds.append((record.round, previous, mark))
            previous = mark
        return bounds

    @staticmethod
    def _round_of(rowid: int, bounds: Iterable[tuple[int, int, int]]) -> int | None:
        """Which round a row belongs to, or ``None`` if the round is still open."""
        for round_index, lower, upper in bounds:
            if lower < rowid <= upper:
                return round_index
        return None


def _maybe_json(value: Any) -> Any:
    """OASIS stores action arguments as a JSON string, sometimes."""
    if not isinstance(value, str) or not value:
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value
