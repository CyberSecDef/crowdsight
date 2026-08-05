"""Phase 2 Step 2 — backoff timing, the retry ceiling, and the gate.

``sleep`` and ``rng`` are injected throughout, so timing is asserted exactly
rather than by wall clock. A test that measured real backoff would take half a
minute and still only prove the delays were roughly right.
"""

from __future__ import annotations

import asyncio
import types

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from app.utils.retry import (
    ConcurrencyGate,
    RetryPolicy,
    get_llm_gate,
    is_transient,
    reset_llm_gate,
    retry_async,
    retry_sync,
)


def status_error(code: int) -> APIStatusError:
    return APIStatusError(
        "boom",
        response=types.SimpleNamespace(status_code=code, request=None, headers={}),
        body=None,
    )


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


def test_ceilings_grow_exponentially():
    policy = RetryPolicy(base_delay=1.0, max_delay=30.0, multiplier=2.0)
    assert [policy.ceiling_for(n) for n in range(1, 7)] == [1, 2, 4, 8, 16, 30]


def test_ceiling_is_capped():
    assert RetryPolicy(base_delay=1.0, max_delay=30.0).ceiling_for(50) == 30.0


def test_full_jitter_spans_zero_to_ceiling():
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0)
    assert policy.delay_for(3, lambda: 0.0) == 0.0
    assert policy.delay_for(3, lambda: 1.0) == 4.0


def test_jitter_actually_varies():
    """Undithered backoff sends a whole failed round back simultaneously."""
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0)
    samples = [policy.delay_for(3) for _ in range(300)]
    assert len(set(samples)) > 250
    assert all(0.0 <= s <= 4.0 for s in samples)


@pytest.mark.parametrize("kwargs", [{"max_attempts": 0}, {"base_delay": -1}, {"max_delay": -1}])
def test_invalid_policy_rejected(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


# --------------------------------------------------------------------------
# Classifier
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", [408, 409, 425, 429, 500, 502, 503, 504])
def test_transient_statuses(code):
    assert is_transient(status_error(code))


@pytest.mark.parametrize("code", [400, 401, 403, 404, 405, 422])
def test_permanent_statuses(code):
    """Retrying a malformed request wastes minutes and buries the real error."""
    assert not is_transient(status_error(code))


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("reset"),
        TimeoutError(),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.PoolTimeout("busy"),
        APIConnectionError(request=httpx.Request("POST", "http://ollama:11434")),
    ],
)
def test_transient_exception_types(exc):
    assert is_transient(exc)


@pytest.mark.parametrize("exc", [ValueError("bad schema"), TypeError(), KeyError("k")])
def test_permanent_exception_types(exc):
    assert not is_transient(exc)


def test_httpx_status_error_is_classified_by_response():
    """HTTPStatusError carries its status on .response, not on itself."""
    request = httpx.Request("POST", "http://ollama:11434/api/embed")
    transient = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(503, request=request)
    )
    permanent = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(400, request=request)
    )
    assert is_transient(transient)
    assert not is_transient(permanent)


# --------------------------------------------------------------------------
# retry_async
# --------------------------------------------------------------------------


async def test_retries_then_succeeds():
    slept: list[float] = []
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("refused")
        return "ok"

    result = await retry_async(
        flaky,
        policy=RetryPolicy(max_attempts=5, base_delay=1.0),
        sleep=lambda d: _record(slept, d),
        rng=lambda: 1.0,
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]


async def _record(bucket: list[float], delay: float) -> None:
    bucket.append(delay)


async def test_retry_ceiling_enforced():
    calls = {"n": 0}

    async def always_fails():
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        await retry_async(
            always_fails,
            policy=RetryPolicy(max_attempts=3, base_delay=0.0),
            sleep=asyncio.sleep,
        )
    assert calls["n"] == 3


async def test_original_exception_type_is_reraised():
    """Callers match on concrete SDK types; a wrapper would break them."""

    async def fails():
        raise httpx.ReadTimeout("slow")

    with pytest.raises(httpx.ReadTimeout):
        await retry_async(fails, policy=RetryPolicy(max_attempts=2, base_delay=0.0))


async def test_permanent_failure_is_not_retried():
    calls = {"n": 0}

    async def permanent():
        calls["n"] += 1
        raise status_error(400)

    with pytest.raises(APIStatusError):
        await retry_async(permanent, policy=RetryPolicy(max_attempts=5, base_delay=0.0))
    assert calls["n"] == 1


async def test_single_attempt_policy_never_retries():
    calls = {"n": 0}

    async def fails():
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        await retry_async(fails, policy=RetryPolicy(max_attempts=1))
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# retry_sync
# --------------------------------------------------------------------------


def test_retry_sync_matches_async_behaviour():
    slept: list[float] = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("reset")
        return "ok"

    result = retry_sync(
        flaky,
        policy=RetryPolicy(max_attempts=4, base_delay=1.0),
        sleep=slept.append,
        rng=lambda: 1.0,
    )
    assert result == "ok"
    assert slept == [1.0, 2.0]


def test_retry_sync_does_not_retry_permanent():
    calls = {"n": 0}

    def permanent():
        calls["n"] += 1
        raise ValueError("bad")

    with pytest.raises(ValueError):
        retry_sync(permanent, policy=RetryPolicy(max_attempts=3), sleep=lambda d: None)
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# ConcurrencyGate
# --------------------------------------------------------------------------


async def test_gate_bounds_concurrency():
    """The bound exists to stop a GPU OOM, so observe the peak, not the API."""
    gate = ConcurrencyGate(limit=4)
    live = {"now": 0, "peak": 0}

    async def work():
        async with gate():
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0.01)
            live["now"] -= 1

    await asyncio.gather(*[work() for _ in range(40)])
    assert live["peak"] == 4
    assert gate.peak_in_flight == 4
    assert gate.in_flight == 0
    assert gate.total_acquired == 40
    assert gate.total_waited > 0


async def test_gate_of_one_serialises():
    gate = ConcurrencyGate(limit=1)
    order: list[str] = []

    async def task(tag: str):
        async with gate():
            order.append(f"{tag}-in")
            await asyncio.sleep(0.01)
            order.append(f"{tag}-out")

    await asyncio.gather(task("a"), task("b"))
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )


async def test_gate_releases_on_exception():
    """A failing call must not leak a slot; four failures would deadlock."""
    gate = ConcurrencyGate(limit=1)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            async with gate():
                raise RuntimeError("boom")
    assert gate.in_flight == 0

    async with gate():
        pass


async def test_gate_slot_is_released_during_backoff():
    """A coroutine sleeping through backoff must not hold an in-flight slot."""
    gate = ConcurrencyGate(limit=1)
    held_during_sleep = {"yes": False}
    calls = {"n": 0}

    async def observe(_delay: float) -> None:
        held_during_sleep["yes"] = gate.in_flight > 0

    async def flaky():
        calls["n"] += 1
        async with gate():
            if calls["n"] < 2:
                raise httpx.ConnectError("refused")
            return "ok"

    await retry_async(flaky, policy=RetryPolicy(max_attempts=3), sleep=observe)
    assert not held_during_sleep["yes"]


def test_gate_rejects_useless_limit():
    with pytest.raises(ValueError):
        ConcurrencyGate(limit=0)


async def test_gate_stats_shape():
    gate = ConcurrencyGate(limit=2)
    async with gate():
        pass
    assert set(gate.stats()) == {
        "limit", "in_flight", "peak_in_flight", "total_acquired", "total_waited",
    }


def test_process_gate_is_a_singleton():
    reset_llm_gate()
    first = get_llm_gate(limit=3)
    assert get_llm_gate() is first
    assert first.limit == 3
    reset_llm_gate()
    assert get_llm_gate(limit=7) is not first
