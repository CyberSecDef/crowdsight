"""Background jobs with pollable, restart-surviving status.

A graph build takes tens of seconds for a small document and much longer for a
real one, so it cannot happen inside a request. The client gets a task id and
polls.

**Task state lives in SQLite, not in a dict.** An in-memory registry loses every
task when the API restarts, and a client polling afterwards gets a 404 that is
indistinguishable from "no such task" — for a job that may have completed. On
startup, tasks still marked ``running`` are reaped to ``failed``: nothing is
executing them any more, and leaving them running means polling forever.

Jobs run on one background thread with its own event loop. The pipeline is
async throughout, and the Ollama concurrency gate already bounds what actually
runs at once, so a second loop would add contention without adding throughput.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid as uuidlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)

__all__ = ["Task", "TaskRunner", "TaskStatus", "TaskStore"]


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    TERMINAL = frozenset({SUCCEEDED, FAILED})
    #: Reached the end of its work but deliberately waiting on a human.
    PAUSED = frozenset({AWAITING_REVIEW})


@dataclass
class Task:
    id: str
    kind: str
    status: str = TaskStatus.PENDING
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    graph_id: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["progress"] = round(self.progress, 4)
        return data

    @property
    def finished(self) -> bool:
        return self.status in TaskStatus.TERMINAL


class TaskStore:
    """SQLite-backed task records."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS tasks (
            id         TEXT PRIMARY KEY,
            kind       TEXT NOT NULL,
            status     TEXT NOT NULL,
            stage      TEXT NOT NULL DEFAULT '',
            progress   REAL NOT NULL DEFAULT 0,
            message    TEXT NOT NULL DEFAULT '',
            graph_id   TEXT,
            result     TEXT NOT NULL DEFAULT '{}',
            error      TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_graph ON tasks(graph_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(self.SCHEMA)

    def create(self, kind: str, *, graph_id: str | None = None) -> Task:
        now = time.time()
        task = Task(
            id="t-" + uuidlib.uuid4().hex[:12],
            kind=kind,
            graph_id=graph_id,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._connection.execute(
                "INSERT INTO tasks (id, kind, status, stage, progress, message, "
                "graph_id, result, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task.id, task.kind, task.status, task.stage, task.progress,
                 task.message, task.graph_id, "{}", None, now, now),
            )
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def update(self, task_id: str, **fields: Any) -> None:
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], default=str)
        fields["updated_at"] = time.time()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._connection.execute(
                f"UPDATE tasks SET {columns} WHERE id = ?",  # noqa: S608 - keys are literals below
                (*fields.values(), task_id),
            )

    def list(self, *, graph_id: str | None = None, limit: int = 50) -> list[Task]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        if graph_id:
            query += " WHERE graph_id = ?"
            params.append(graph_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 200)))
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [_row_to_task(row) for row in rows]

    def reap_orphans(self) -> int:
        """Fail tasks left running by a crash or restart.

        Nothing is executing them any more. Leaving them ``running`` means a
        client polls forever for a job that will never finish.
        """
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE tasks SET status = ?, error = ?, updated_at = ? "
                "WHERE status IN (?, ?)",
                (TaskStatus.FAILED,
                 "Interrupted: the API restarted while this task was running.",
                 time.time(), TaskStatus.RUNNING, TaskStatus.PENDING),
            )
        if cursor.rowcount:
            logger.warning("Reaped %d orphaned task(s) on startup", cursor.rowcount)
        return cursor.rowcount

    def close(self) -> None:
        self._connection.close()


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"], kind=row["kind"], status=row["status"], stage=row["stage"],
        progress=row["progress"], message=row["message"], graph_id=row["graph_id"],
        result=json.loads(row["result"] or "{}"), error=row["error"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class TaskRunner:
    """Runs coroutines on a dedicated background loop, recording progress."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="crowdsight-tasks", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def submit(
        self,
        task: Task,
        job: Callable[["TaskProgress"], Awaitable[dict[str, Any]]],
    ) -> Task:
        """Schedule ``job`` and return immediately."""
        progress = TaskProgress(self.store, task.id)
        self.store.update(task.id, status=TaskStatus.RUNNING, stage="starting")

        async def wrapper() -> None:
            try:
                result = await job(progress)
                # A job may have parked itself awaiting review; do not override.
                current = self.store.get(task.id)
                if current and current.status in TaskStatus.PAUSED:
                    self.store.update(task.id, result=result)
                else:
                    self.store.update(
                        task.id, status=TaskStatus.SUCCEEDED, progress=1.0,
                        stage="done", result=result, error=None,
                    )
            except Exception as exc:  # noqa: BLE001 - recorded for the client
                logger.exception("Task %s failed", task.id)
                self.store.update(
                    task.id, status=TaskStatus.FAILED,
                    error=f"{exc.__class__.__name__}: {exc}",
                )

        asyncio.run_coroutine_threadsafe(wrapper(), self._loop)
        return task

    def run_sync(self, coro: Awaitable[Any], timeout: float | None = 60.0) -> Any:
        """Run a coroutine from a Flask request handler and wait for it."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def shutdown(self) -> None:  # pragma: no cover - process teardown
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


@dataclass
class TaskProgress:
    """Handle a job uses to report where it has got to."""

    store: TaskStore
    task_id: str

    def update(
        self, *, stage: str | None = None, progress: float | None = None,
        message: str | None = None, graph_id: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {}
        if stage is not None:
            fields["stage"] = stage
        if progress is not None:
            fields["progress"] = max(0.0, min(1.0, progress))
        if message is not None:
            fields["message"] = message
        if graph_id is not None:
            fields["graph_id"] = graph_id
        if fields:
            self.store.update(self.task_id, **fields)

    def await_review(
        self, result: dict[str, Any], message: str, *,
        stage: str = "ontology_review",
    ) -> None:
        """Park the task: its work is done and a human must act next."""
        self.store.update(
            self.task_id, status=TaskStatus.AWAITING_REVIEW, stage=stage,
            progress=0.5, message=message, result=result,
        )
