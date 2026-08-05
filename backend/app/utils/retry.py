"""Retry with backoff, and the concurrency bound on local inference.

Two concerns that belong together because they both protect the same scarce
resource: one Ollama instance in front of one GPU.

**Retry.** Transient failures — a connection reset, a read timeout, a 5xx
while the model is loading — should cost a pause, not a run. Permanent ones —
a malformed request, an unknown model — should fail immediately, because
retrying them wastes minutes and hides the real error. :func:`is_transient`
draws that line.

Backoff uses **full jitter**: ``uniform(0, min(cap, base * multiplier**n))``
rather than a fixed exponential. When a local Ollama restarts, every queued
agent turn fails at once; undithered backoff would send them all back
simultaneously, repeatedly, in a thundering herd.

**Concurrency.** A single Ollama serialises internally, so unbounded
concurrency does not increase throughput — it just queues requests inside the
server, inflates latency, and risks a GPU OOM as more model contexts are held
open at once. :class:`ConcurrencyGate` bounds in-flight requests.

The gate is **per process, per event loop**. Phase 6 runs each simulation in
its own OS process, so a process-local bound does not cap total load on
Ollama by itself: three concurrent runs at 4 each would put 12 requests in
flight. Phase 6's simulation manager must therefore divide ``LLM_CONCURRENCY``
across the workers it spawns and pass each its share. The arithmetic is
deliberately explicit rather than hidden behind a cross-process semaphore,
whose blocking ``acquire`` would park one thread per waiting coroutine —
hundreds of them during a 300-agent round.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

__all__ = [
    "ConcurrencyGate",
    "RetryPolicy",
    "get_llm_gate",
    "is_transient",
    "reset_llm_gate",
    "retry_async",
    "retry_sync",
]

T = TypeVar("T")

# HTTP statuses worth another attempt. 429 is rate limiting, 5xx is the server
# having a bad moment — commonly Ollama loading a model into VRAM. Everything
# else in the 4xx range is our mistake and will fail identically next time.
TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """How many times, and how long to wait between."""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must not be negative")

    def ceiling_for(self, attempt: int) -> float:
        """Undithered upper bound for the delay after ``attempt`` (1-based)."""
        return min(self.max_delay, self.base_delay * (self.multiplier ** (attempt - 1)))

    def delay_for(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        """Full jitter: anywhere in ``[0, ceiling]``.

        Spreading retries across the whole interval, rather than clustering
        them at the top of it, is what actually breaks up a herd.
        """
        return self.ceiling_for(attempt) * rng()


def is_transient(exc: BaseException) -> bool:
    """True when another attempt could plausibly succeed.

    Imports are local and guarded so this module does not require the openai,
    httpx or neo4j packages to be installed — it is used by all three layers
    and should not couple them together.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status in TRANSIENT_STATUS

    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)):
        return True

    for module_name, transient_names in (
        ("openai", {"APIConnectionError", "APITimeoutError", "RateLimitError",
                    "InternalServerError", "APIConnectionTimeoutError"}),
        ("httpx", {"ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
                   "PoolTimeout", "RemoteProtocolError", "ReadError"}),
        ("neo4j.exceptions", {"ServiceUnavailable", "SessionExpired", "TransientError"}),
    ):
        module = _maybe_import(module_name)
        if module is None:
            continue
        for name in transient_names:
            candidate = getattr(module, name, None)
            if isinstance(candidate, type) and isinstance(exc, candidate):
                return True

    return False


def _maybe_import(name: str) -> Any | None:
    try:
        import importlib

        return importlib.import_module(name)
    except ImportError:  # pragma: no cover - depends on install profile
        return None


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    retryable: Callable[[BaseException], bool] = is_transient,
    description: str = "operation",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
) -> T:
    """Await ``func()``, retrying transient failures with jittered backoff.

    Re-raises the *original* exception once the budget is exhausted rather
    than wrapping it — callers such as the LLM client match on concrete SDK
    types, and a wrapper would break that.

    ``sleep`` and ``rng`` are injectable so tests can assert timing without
    actually waiting.
    """
    policy = policy or RetryPolicy()
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await func()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if attempt >= policy.max_attempts or not retryable(exc):
                if attempt > 1:
                    logger.error(
                        "%s failed after %d attempt(s): %s", description, attempt, exc
                    )
                raise
            delay = policy.delay_for(attempt, rng)
            logger.warning(
                "%s failed (attempt %d/%d): %s. Retrying in %.2fs",
                description, attempt, policy.max_attempts, exc, delay,
            )
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def retry_sync(
    func: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    retryable: Callable[[BaseException], bool] = is_transient,
    description: str = "operation",
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
) -> T:
    """Blocking counterpart, for the synchronous Neo4j driver."""
    policy = policy or RetryPolicy()
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return func()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if attempt >= policy.max_attempts or not retryable(exc):
                if attempt > 1:
                    logger.error(
                        "%s failed after %d attempt(s): %s", description, attempt, exc
                    )
                raise
            delay = policy.delay_for(attempt, rng)
            logger.warning(
                "%s failed (attempt %d/%d): %s. Retrying in %.2fs",
                description, attempt, policy.max_attempts, exc, delay,
            )
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class ConcurrencyGate:
    """Bounds in-flight requests to one backend.

    Semaphores are created per running event loop. ``SyncLLMClient`` runs its
    own loop on a background thread, and a semaphore awaited from a loop other
    than the one it was created on is undefined behaviour, so one gate object
    can safely serve both without them sharing a counter. In the normal case a
    process has a single loop doing inference work and there is exactly one.
    """

    def __init__(self, limit: int, name: str = "llm") -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self.name = name
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        self._lock = threading.Lock()
        # Observability, for the health endpoint and for tests that need to
        # prove the bound is actually respected rather than merely present.
        self.in_flight = 0
        self.peak_in_flight = 0
        self.total_acquired = 0
        self.total_waited = 0

    def _semaphore(self) -> asyncio.Semaphore:
        loop_id = id(asyncio.get_running_loop())
        with self._lock:
            semaphore = self._semaphores.get(loop_id)
            if semaphore is None:
                semaphore = asyncio.Semaphore(self.limit)
                self._semaphores[loop_id] = semaphore
            return semaphore

    @contextlib.asynccontextmanager
    async def __call__(self) -> AsyncIterator[None]:
        semaphore = self._semaphore()
        if semaphore.locked():
            self.total_waited += 1
        await semaphore.acquire()
        self.in_flight += 1
        self.total_acquired += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            yield
        finally:
            self.in_flight -= 1
            semaphore.release()

    def stats(self) -> dict[str, int]:
        return {
            "limit": self.limit,
            "in_flight": self.in_flight,
            "peak_in_flight": self.peak_in_flight,
            "total_acquired": self.total_acquired,
            "total_waited": self.total_waited,
        }


_llm_gate: ConcurrencyGate | None = None
_llm_gate_lock = threading.Lock()


def get_llm_gate(limit: int | None = None) -> ConcurrencyGate:
    """The process-wide gate for Ollama requests.

    Chat completions and embeddings share it deliberately: they contend for
    the same GPU, and bounding them separately would let their sum exceed the
    limit that exists to prevent an OOM.
    """
    global _llm_gate
    with _llm_gate_lock:
        if _llm_gate is None:
            if limit is None:
                from app.config import get_config

                limit = get_config().LLM_CONCURRENCY
            _llm_gate = ConcurrencyGate(limit, name="ollama")
            logger.info("Ollama concurrency bounded to %d in-flight request(s)", limit)
        return _llm_gate


def reset_llm_gate() -> None:
    """Drop the process-wide gate. For tests, and for worker re-initialisation."""
    global _llm_gate
    with _llm_gate_lock:
        _llm_gate = None
