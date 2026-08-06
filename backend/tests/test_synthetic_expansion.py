"""Phase 4 Step 2 — expanding named actors into a crowd.

The safety property is that a synthetic agent never carries the name of someone
the document names. Without it a simulated post becomes a fabricated statement
attributed to a real, identifiable person.
"""

from __future__ import annotations

import json
import random

import pytest
import respx

from app.services.ontology_generator import Ontology
from app.services.population import (
    FIRST_NAMES,
    SURNAMES,
    NameAllocator,
    PopulationSketch,
    assert_no_name_collisions,
    build_population,
    plan_population,
    provenance_counts,
    sketch_population,
)
from app.storage.ner_extractor import normalise_name
from app.utils.llm_client import LLMClient
from app.utils.retry import RetryPolicy
from tests.conftest import chat_completion

CHAT = "http://ollama:11434/v1/chat/completions"

# Deliberately not the allocated name: every test that matters asserts this
# string is *absent* from the output.
MODEL_PERSONA = {
    "name": "MODEL CHOSEN NAME", "age": 99, "occupation": "astronaut",
    "background": "Invented.", "personality": {"openness": 0.5},
    "traits": ["x"], "interests": ["y"], "leanings": "none",
    "activity_level": "moderate", "writing_style": "plain",
}


@pytest.fixture
def plan(named_contexts, sample_sketch, profile_generator):
    def _make(total: int = 12, named_ratio: float = 0.25):
        return plan_population(named_contexts, total=total, sketch=sample_sketch,
                               generator=profile_generator(), named_ratio=named_ratio)
    return _make


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------


def test_a_reserved_name_and_its_title_variants_are_unavailable():
    allocator = NameAllocator(["Councillor Jane Doe"], rng=random.Random(1))
    assert not allocator.is_available("Jane Doe")
    assert not allocator.is_available("Cllr Jane Doe")
    assert allocator.is_available("Dawn Mercer")


def test_allocated_names_are_unique_and_never_reserved():
    allocator = NameAllocator(["Jane Doe", "Alan Reyes"], rng=random.Random(1))
    names = [allocator.take() for _ in range(200)]
    assert len(set(names)) == 200
    assert not ({normalise_name(n) for n in names} & allocator.reserved)


def test_an_exhausted_pool_numbers_rather_than_repeats():
    """Two agents sharing a name breaks every downstream join."""
    everything = [f"{first} {last}" for first in FIRST_NAMES for last in SURNAMES]
    allocator = NameAllocator(everything, rng=random.Random(2))
    forced = [allocator.take() for _ in range(3)]
    assert len(set(forced)) == 3
    assert allocator.collisions_avoided > 0


# --------------------------------------------------------------------------
# Ratio
# --------------------------------------------------------------------------


def test_requesting_n_agents_plans_n(plan):
    assert plan(total=20).total == 20


@pytest.mark.parametrize(
    ("total", "ratio", "expect_named", "expect_synthetic"),
    [
        (20, 0.25, 4, 16),   # cap allows 5, only 4 named entities exist
        (100, 0.25, 4, 96),
        (4, 0.25, 1, 3),     # cap binds
        (4, 1.0, 4, 0),
        (10, 0.0, 1, 9),     # never discards the document's actors entirely
    ],
)
def test_named_to_synthetic_ratio_is_respected(plan, total, ratio, expect_named,
                                               expect_synthetic):
    result = plan(total=total, named_ratio=ratio)
    assert len(result.named) == expect_named
    assert len(result.synthetic) == expect_synthetic


def test_named_entities_beyond_the_cap_are_reported(plan):
    assert plan(total=4, named_ratio=0.25).dropped_named == 3


def test_a_document_naming_nobody_yields_an_all_synthetic_crowd(sample_sketch,
                                                                profile_generator):
    result = plan_population([], total=5, sketch=sample_sketch,
                             generator=profile_generator())
    assert result.total == 5 and not result.named


@pytest.mark.parametrize("kwargs", [{"total": 0}, {"named_ratio": 1.5}, {"named_ratio": -0.1}])
def test_invalid_planning_arguments_rejected(named_contexts, sample_sketch,
                                             profile_generator, kwargs):
    with pytest.raises(ValueError):
        plan_population(named_contexts, sketch=sample_sketch,
                        generator=profile_generator(), **{"total": 5, **kwargs})


# --------------------------------------------------------------------------
# What the plan assigns
# --------------------------------------------------------------------------


def test_synthetic_agents_are_assigned_varied_occupations(plan):
    occupations = [c.assigned_occupation for c in plan(total=20).synthetic]
    assert all(occupations)
    assert len(set(occupations)) >= 14


def test_assigned_attributes_come_from_the_sketch(plan, sample_sketch):
    result = plan(total=20)
    ages = [c.assigned_age for c in result.synthetic]
    stances = {c.assigned_stance.split(":")[0] for c in result.synthetic}

    assert all(sample_sketch.min_age <= age <= sample_sketch.max_age for age in ages)
    assert stances <= {s.label for s in sample_sketch.stances}
    assert {c.group for c in result.synthetic} <= set(sample_sketch.affected_groups)


def test_no_planned_synthetic_name_collides(plan, named_contexts):
    reserved = {normalise_name(c.name) for c in named_contexts}
    planned = {normalise_name(c.name) for c in plan(total=20).synthetic}
    assert not (planned & reserved)


# --------------------------------------------------------------------------
# The sketch
# --------------------------------------------------------------------------


@respx.mock
async def test_sketch_parses(config, sample_sketch):
    respx.post(CHAT).mock(return_value=chat_completion(
        json.dumps(sample_sketch.model_dump())
    ))
    ontology = Ontology.model_validate(
        {"entity_types": [{"name": "Person", "description": "x"}]}
    )
    result = await sketch_population(
        ontology, "A council approved a housing policy.",
        LLMClient(config, retry_policy=RetryPolicy(max_attempts=1)),
    )
    assert len(result.stances) == 4


@respx.mock
async def test_a_failed_sketch_falls_back_to_a_generic_crowd(config):
    """A generic crowd is a poor simulation; no crowd is no simulation."""
    respx.post(CHAT).mock(return_value=chat_completion("not json"))
    ontology = Ontology.model_validate(
        {"entity_types": [{"name": "Person", "description": "x"}]}
    )
    result = await sketch_population(
        ontology, "text",
        LLMClient(config, max_json_attempts=1, retry_policy=RetryPolicy(max_attempts=1)),
    )
    assert result.stances and result.affected_groups


def test_a_sketch_without_stances_is_rejected():
    with pytest.raises(Exception):
        PopulationSketch(stances=[])


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


@respx.mock
async def test_requesting_n_agents_yields_n_profiles(plan, profile_generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(MODEL_PERSONA)))
    result = plan(total=12)
    profiles, failures = await build_population(profile_generator(), result)

    assert len(profiles) == 12
    assert not failures


@respx.mock
async def test_every_profile_carries_correct_provenance(plan, profile_generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(MODEL_PERSONA)))
    result = plan(total=12)
    profiles, _ = await build_population(profile_generator(), result)

    assert provenance_counts(profiles) == {
        "named": len(result.named), "synthetic": len(result.synthetic)
    }
    assert all(p.provenance in {"named", "synthetic"} for p in profiles)


@respx.mock
async def test_named_profiles_link_back_and_synthetic_ones_do_not(plan, profile_generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(MODEL_PERSONA)))
    profiles, _ = await build_population(profile_generator(), plan(total=12))

    named = [p for p in profiles if p.provenance == "named"]
    synthetic = [p for p in profiles if p.provenance == "synthetic"]
    assert all(p.source_entity_uuid for p in named)
    assert all(p.source_entity_uuid is None for p in synthetic)


@respx.mock
async def test_the_models_chosen_name_is_discarded(plan, profile_generator):
    """The allocated name is the safety property, not a suggestion."""
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(MODEL_PERSONA)))
    result = plan(total=12)
    profiles, _ = await build_population(profile_generator(), result)

    synthetic = [p for p in profiles if p.provenance == "synthetic"]
    assert all(p.name != "MODEL CHOSEN NAME" for p in synthetic)
    assert {p.name for p in synthetic} == {c.assigned_name for c in result.synthetic}
    assert all(p.age != 99 for p in synthetic), "assigned age wins"
    assert all(p.occupation != "astronaut" for p in synthetic), "assigned occupation wins"


@respx.mock
async def test_no_synthetic_agent_takes_a_named_persons_name(plan, profile_generator,
                                                             named_contexts):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(MODEL_PERSONA)))
    profiles, _ = await build_population(profile_generator(), plan(total=12))
    assert assert_no_name_collisions(profiles, [c.name for c in named_contexts]) == []


def test_the_collision_detector_detects(make_persona):
    """A guard that cannot fail is not a guard."""
    offender = make_persona(name="Jane Doe", provenance="synthetic")
    assert assert_no_name_collisions([offender], ["Councillor Jane Doe"]) == ["Jane Doe"]


def test_a_named_profile_is_not_its_own_collision(make_persona):
    named = make_persona(name="Jane Doe", provenance="named")
    assert assert_no_name_collisions([named], ["Councillor Jane Doe"]) == []


@respx.mock
async def test_the_synthetic_prompt_says_the_person_is_invented(plan, profile_generator):
    route = respx.post(CHAT).mock(return_value=chat_completion(json.dumps(MODEL_PERSONA)))
    brief = plan(total=12).synthetic[0]
    await profile_generator().generate_for_entity(brief)
    prompt = json.loads(route.calls[0].request.content)["messages"][-1]["content"]

    assert "NOT named in the source" in prompt
    assert "not a public figure" in prompt
    assert brief.assigned_name in prompt


@respx.mock
async def test_one_failed_persona_costs_only_that_agent(plan, profile_generator):
    def handler(request):
        if "Sarah Kim" in request.content.decode():
            return chat_completion("garbage")
        return chat_completion(json.dumps(MODEL_PERSONA))

    respx.post(CHAT).mock(side_effect=handler)
    profiles, failures = await build_population(
        profile_generator(max_json_attempts=1), plan(total=10, named_ratio=0.4)
    )
    assert failures == ["Sarah Kim"]
    assert len(profiles) == 9
