"""Phase 10 Step 4 — logging, in a form a machine can read when asked.

Text by default, JSON when `CROWDSIGHT_LOG_FORMAT=json`.

The default is deliberate. These logs are genuinely read by people: the worker
prefixes every line with its `sim_id` precisely so a human can follow one run
through a stack that is running two, and watching round-by-round output is how
a long run is actually monitored today. Making JSON the default would trade
that for a log shipper nobody has deployed.

What JSON adds is the thing text cannot do — fields that survive being grepped
apart. `sim_id` and `round` become queryable rather than a prefix somebody has
to parse back out.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

#: Attributes LogRecord always carries. Anything else on the record was put
#: there by a caller and is worth emitting.
_STANDARD = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
})

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def __init__(self, *, context: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.context = context or {}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **self.context,
        }
        # Whatever a caller attached via `extra=` — sim_id, round, task_id.
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # default=str so a log line can never be what kills a run: an
        # unserialisable value in `extra` would otherwise raise inside logging.
        return json.dumps(payload, default=str)


def json_logging_requested() -> bool:
    return os.environ.get("CROWDSIGHT_LOG_FORMAT", "").strip().lower() == "json"


def configure(*, level: int | str | None = None,
              context: dict[str, Any] | None = None,
              stream=None) -> None:
    """Install the chosen formatter on the root logger.

    Idempotent: replaces handlers rather than adding to them, so calling it
    twice does not double every line.
    """
    resolved = level or os.environ.get("CROWDSIGHT_LOG_LEVEL", "INFO")
    root = logging.getLogger()
    root.setLevel(resolved)

    handler = logging.StreamHandler(stream or sys.stderr)
    if json_logging_requested():
        handler.setFormatter(JsonFormatter(context=context))
    else:
        prefix = ""
        if context:
            # The human form keeps the context as a prefix, which is how these
            # logs have always been read.
            prefix = " ".join(f"[{value}]" for value in context.values()) + " "
        handler.setFormatter(logging.Formatter(
            f"%(asctime)s {prefix}%(levelname)s %(name)s: %(message)s"))

    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
