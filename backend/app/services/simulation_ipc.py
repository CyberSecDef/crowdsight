"""Phase 6 Step 2 — the control plane between the API and a running simulation.

A Unix socket in the run's own directory, carrying newline-delimited JSON.

Chosen over a ``multiprocessing.Queue`` pair because the queues live and die
with the parent. A simulation is a multi-hour, GPU-bound process; if the API
restarts — a crash, a deploy — a queue-based worker becomes permanently
unreachable while still consuming the GPU. A socket file outlives the parent, so
a manager coming back up can knock on it and find out whether anybody is home.

The protocol is deliberately plain text: one JSON object per line, request and
response. It can be driven from ``socat`` when something has gone wrong at three
in the morning, which a pickle stream cannot.

Two asymmetries are on purpose:

* The **server** is asyncio, because it lives inside the worker's event loop
  alongside the simulation.
* The **client** is blocking, because it is called from Flask request handlers
  that have no event loop of their own. Every call carries a timeout; the API
  must never hang because a worker is wedged mid-round.

Because the client blocks, it must never be called from the same event loop the
server runs in — the loop would be unable to serve the request it is waiting
for, and the call would time out. In production they are in different
processes, so this cannot arise; in a test that starts both, drive the client
through ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "SOCKET_NAME",
    "ControlClient",
    "ControlServer",
    "IPCError",
    "Request",
    "WorkerUnreachable",
    "socket_path",
]

SOCKET_NAME = "control.sock"

#: Linux's ``sun_path`` is 108 bytes including the terminating NUL, so 107
#: usable characters. Exceeding it fails at bind() with a confusing error about
#: the address rather than the length, and the simulation root is configurable —
#: a deeply nested data directory would hit this. Checked here so the message
#: names the real problem.
MAX_SOCKET_PATH = 107

#: A control call must never block an API request for long. A worker mid-round
#: still answers promptly: the server runs in the same loop as the simulation,
#: which awaits on I/O constantly.
DEFAULT_TIMEOUT = 5.0

Handler = Callable[["Request"], Awaitable[Any]]


class IPCError(RuntimeError):
    """The control channel could not be used."""


class WorkerUnreachable(IPCError):
    """Nobody answered. The worker is gone, wedged, or never started."""


def socket_path(sim_dir: str | Path) -> Path:
    path = Path(sim_dir) / SOCKET_NAME
    if len(str(path.resolve() if path.parent.exists() else path)) > MAX_SOCKET_PATH:
        raise IPCError(
            f"Control socket path is too long for the kernel "
            f"({MAX_SOCKET_PATH} characters): {path}"
        )
    return path


@dataclass
class Request:
    command: str
    args: dict[str, Any]


# --------------------------------------------------------------------------
# Server — inside the worker
# --------------------------------------------------------------------------


class ControlServer:
    """Answers control requests for one simulation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handlers: dict[str, Handler] = {}
        self._server: asyncio.AbstractServer | None = None

    def handle(self, command: str, handler: Handler) -> None:
        self._handlers[command] = handler

    async def start(self) -> None:
        # A socket file left by a previous process would make bind() fail with
        # "address already in use" even though nobody is listening.
        self._remove_stale()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        # Readable only by the owner: this channel can stop a run.
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        logger.info("Control socket listening at %s", self.path)

    def _remove_stale(self) -> None:
        if not self.path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.5)
            probe.connect(str(self.path))
        except OSError:
            logger.warning("Removing stale control socket %s", self.path)
            with contextlib.suppress(OSError):
                self.path.unlink()
        else:
            raise IPCError(f"Another worker is already listening on {self.path}")
        finally:
            probe.close()

    async def _serve(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                await self._respond(line, writer)
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _respond(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            payload = json.loads(line.decode("utf-8"))
            command = str(payload.get("command") or "")
            handler = self._handlers.get(command)
            if handler is None:
                reply = {"ok": False, "error": f"Unknown command {command!r}"}
            else:
                result = await handler(Request(command, payload.get("args") or {}))
                reply = {"ok": True, "result": result}
        except json.JSONDecodeError as exc:
            reply = {"ok": False, "error": f"Malformed request: {exc}"}
        except Exception as exc:  # noqa: BLE001 - a bad request must not kill the run
            logger.exception("Control command failed")
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        writer.write((json.dumps(reply) + "\n").encode("utf-8"))
        await writer.drain()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(OSError):
            self.path.unlink()


# --------------------------------------------------------------------------
# Client — in the API process
# --------------------------------------------------------------------------


class ControlClient:
    """Blocking client. Safe to call from a Flask handler."""

    def __init__(self, path: str | Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.path = Path(path)
        self.timeout = timeout

    def request(self, command: str, **args: Any) -> Any:
        """Send one command and return its result, or raise."""
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.path))
            connection.sendall(
                (json.dumps({"command": command, "args": args}) + "\n").encode("utf-8")
            )
            reply = self._read_line(connection)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise WorkerUnreachable(f"No worker listening at {self.path}") from exc
        except socket.timeout as exc:
            raise WorkerUnreachable(
                f"Worker at {self.path} did not answer within {self.timeout}s"
            ) from exc
        except OSError as exc:
            raise WorkerUnreachable(f"Control channel {self.path} failed: {exc}") from exc
        finally:
            connection.close()

        if not reply.get("ok"):
            raise IPCError(str(reply.get("error") or "Unknown control error"))
        return reply.get("result")

    def _read_line(self, connection: socket.socket) -> dict[str, Any]:
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        raw = b"".join(chunks).split(b"\n", 1)[0]
        if not raw:
            raise WorkerUnreachable(f"Worker at {self.path} closed without replying")
        return json.loads(raw.decode("utf-8"))

    def ping(self) -> bool:
        """True when a worker is alive on the other end. Never raises."""
        try:
            self.request("ping")
            return True
        except IPCError:
            return False
