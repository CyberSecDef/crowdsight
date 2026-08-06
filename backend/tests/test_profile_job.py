"""Phase 4 Step 4 — resumable generation.

Not in the spec's list of test files, but Step 4's guarantees are the kind that
only fail after a crash, when nobody is watching. Three hundred agents is half
an hour of local inference; a resume that silently rebuilds a *different*
population is worse than one that fails.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
import respx

from app.services.population import PopulationPlan, plan_population
from app.services.profile_job import (
    PARTIAL_FILE,
    PLAN_FILE,
    PlanMismatch,
    generate_population,
    load_plan,
    plan_fingerprint,
    read_partial,
    save_plan,
)
from tests.conftest import chat_completion

CHAT = "http://ollama:11434/v1/chat/completions"

PERSONA = {
    "name": "Placeholder", "age": 40, "occupation": "carpenter",
    "background": "Some background.", "personality": {"openness": 0.5},
    "traits": ["x"], "interests": ["y"], "leanings": "none",
    "activity_level": "moderate", "writing_style": "plain",
}


@pytest.fixture
def plan(named_contexts, sample_sketch, profile_generator):
    def _make(total: int = 12):
        return plan_population(named_contexts[:2], total=total, sketch=sample_sketch,
                               generator=profile_generator(), named_ratio=0.25)
    return _make


def persona_response():
    return chat_completion(json.dumps(PERSONA))


# --------------------------------------------------------------------------
# The plan on disk
# --------------------------------------------------------------------------


def test_plan_round_trips_with_its_assignments(plan, tmp_path):
    original = plan()
    save_plan(original, tmp_path)
    assert (tmp_path / PLAN_FILE).is_file()

    loaded = load_plan(tmp_path)
    assert [c.name for c in loaded.synthetic] == [c.name for c in original.synthetic]
    assert ([c.assigned_occupation for c in loaded.synthetic]
            == [c.assigned_occupation for c in original.synthetic])
    assert all(c.synthetic for c in loaded.synthetic)


def test_fingerprint_identifies_the_population(plan, tmp_path):
    original = plan()
    save_plan(original, tmp_path)
    assert plan_fingerprint(load_plan(tmp_path)) == plan_fingerprint(original)
    assert plan_fingerprint(plan(total=13)) != plan_fingerprint(original)


def test_load_plan_on_an_empty_directory(tmp_path):
    assert load_plan(tmp_path) is None


# --------------------------------------------------------------------------
# A complete run
# --------------------------------------------------------------------------


@respx.mock
async def test_every_agent_is_generated(plan, profile_generator, tmp_path):
    respx.post(CHAT).mock(return_value=persona_response())
    result = await generate_population(profile_generator(), plan(), tmp_path)

    assert result.total == 12
    assert result.generated == 12
    assert not result.failures


@respx.mock
async def test_progress_is_reported_per_profile(plan, profile_generator, tmp_path):
    respx.post(CHAT).mock(return_value=persona_response())
    seen: list[tuple[int, int, str]] = []
    await generate_population(profile_generator(), plan(), tmp_path,
                              progress=lambda d, t, n: seen.append((d, t, n)))

    counts = [entry[0] for entry in seen]
    assert counts == sorted(counts), "progress must not go backwards"
    assert counts[-1] == 12
    assert all(entry[1] == 12 for entry in seen)


@respx.mock
async def test_completed_work_is_one_line_per_profile(plan, profile_generator, tmp_path):
    respx.post(CHAT).mock(return_value=persona_response())
    await generate_population(profile_generator(), plan(), tmp_path)

    lines = (tmp_path / PARTIAL_FILE).read_text().strip().splitlines()
    assert len(lines) == 12
    assert all(set(json.loads(line)) == {"index", "profile"} for line in lines)


@respx.mock
async def test_profiles_come_back_in_plan_order(plan, profile_generator, tmp_path):
    respx.post(CHAT).mock(return_value=persona_response())
    result = plan()
    generated = await generate_population(profile_generator(), result, tmp_path)
    assert [p.name for p in generated.profiles][:2] == [c.name for c in result.named]


# --------------------------------------------------------------------------
# Resumption
# --------------------------------------------------------------------------


@pytest.fixture
async def interrupted(plan, profile_generator, tmp_path):
    """A run stopped part-way, with work on disk."""
    calls = {"n": 0}

    def stop_after_five(request):
        calls["n"] += 1
        if calls["n"] > 5:
            raise RuntimeError("simulated interruption")
        return persona_response()

    result = plan()
    with respx.mock:
        respx.post(CHAT).mock(side_effect=stop_after_five)
        await generate_population(profile_generator(), result, tmp_path, concurrency=1)
    return result


async def test_an_interruption_keeps_completed_work(interrupted, tmp_path):
    assert len(read_partial(tmp_path)) == 5


@respx.mock
async def test_resume_regenerates_only_the_outstanding(interrupted, profile_generator,
                                                       tmp_path):
    respx.post(CHAT).mock(return_value=persona_response())
    result = await generate_population(profile_generator(), interrupted, tmp_path)

    assert result.resumed == 5
    assert result.generated == 7
    assert result.total == 12


@respx.mock
async def test_resume_reuses_the_persisted_assignments(interrupted, profile_generator,
                                                       tmp_path):
    """Re-planning would build a different population from the interrupted one."""
    respx.post(CHAT).mock(return_value=persona_response())
    result = await generate_population(profile_generator(), interrupted, tmp_path)

    synthetic = result.profiles[len(interrupted.named):]
    assert [p.name for p in synthetic] == [c.assigned_name for c in interrupted.synthetic]
    assert ([p.occupation for p in synthetic]
            == [c.assigned_occupation for c in interrupted.synthetic])


@respx.mock
async def test_resume_retries_what_failed(interrupted, profile_generator, tmp_path):
    respx.post(CHAT).mock(return_value=persona_response())
    result = await generate_population(profile_generator(), interrupted, tmp_path)
    assert not result.failures


@respx.mock
async def test_resuming_a_complete_run_generates_nothing(plan, profile_generator, tmp_path):
    route = respx.post(CHAT).mock(return_value=persona_response())
    result = plan()
    await generate_population(profile_generator(), result, tmp_path)
    calls_after_first = route.call_count

    second = await generate_population(profile_generator(), result, tmp_path)
    assert second.generated == 0
    assert second.resumed == 12
    assert route.call_count == calls_after_first


@respx.mock
async def test_a_torn_final_line_is_discarded_and_the_rest_kept(plan, profile_generator,
                                                                tmp_path):
    """After a kill the last line is routinely half-written."""
    respx.post(CHAT).mock(return_value=persona_response())
    result = plan(total=6)
    await generate_population(profile_generator(), result, tmp_path)

    path = tmp_path / PARTIAL_FILE
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:4]) + "\n" + lines[4][:30])

    assert len(read_partial(tmp_path)) == 4
    recovered = await generate_population(profile_generator(), result, tmp_path)
    assert recovered.total == 6
    assert recovered.resumed == 4


@respx.mock
async def test_resuming_a_different_population_is_refused(plan, profile_generator,
                                                          tmp_path):
    """Splicing half of one population into another is not a resume."""
    respx.post(CHAT).mock(return_value=persona_response())
    await generate_population(profile_generator(), plan(total=6), tmp_path)

    with pytest.raises(PlanMismatch, match="different population"):
        await generate_population(profile_generator(), plan(total=20), tmp_path)


@respx.mock
async def test_resume_false_starts_over(plan, profile_generator, tmp_path):
    respx.post(CHAT).mock(return_value=persona_response())
    await generate_population(profile_generator(), plan(total=6), tmp_path)

    fresh = await generate_population(profile_generator(), plan(total=8), tmp_path,
                                      resume=False)
    assert fresh.generated == 8
    assert fresh.resumed == 0


# --------------------------------------------------------------------------
# Concurrency and failure
# --------------------------------------------------------------------------


@respx.mock
async def test_work_runs_in_parallel_and_is_bounded(plan, profile_generator, tmp_path):
    live = {"now": 0, "peak": 0}

    async def slow(request):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.05)
        live["now"] -= 1
        return persona_response()

    respx.post(CHAT).mock(side_effect=slow)
    started = time.monotonic()
    result = await generate_population(profile_generator(), plan(total=16), tmp_path,
                                       concurrency=4)
    elapsed = time.monotonic() - started

    assert result.total == 16
    assert live["peak"] <= 4
    assert elapsed < 16 * 0.05 * 0.6, "serial execution would take the full sum"


@respx.mock
async def test_one_failing_agent_costs_only_itself_and_is_retried(plan, profile_generator,
                                                                  tmp_path):
    result = plan(total=8)
    doomed = result.synthetic[0].assigned_name

    def one_bad(request):
        if doomed in request.content.decode():
            return chat_completion("garbage")
        return persona_response()

    respx.post(CHAT).mock(side_effect=one_bad)
    first = await generate_population(profile_generator(max_json_attempts=1), result,
                                      tmp_path)
    assert first.total == 7
    assert first.failures == [result.synthetic[0].name]

    respx.post(CHAT).mock(return_value=persona_response())
    second = await generate_population(profile_generator(), result, tmp_path)
    assert second.total == 8
    assert second.generated == 1
    assert second.resumed == 7


async def test_an_empty_plan_is_rejected(profile_generator, tmp_path):
    with pytest.raises(ValueError):
        await generate_population(profile_generator(), PopulationPlan(), tmp_path)
