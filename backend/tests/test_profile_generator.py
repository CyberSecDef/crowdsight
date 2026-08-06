"""Phase 4 Step 1 — entity to persona, with a mocked model.

The eligibility logic carries the most weight. The ontology is generated per
document, so filtering on a hard-coded `Person` type selects nobody — silently
— on every real ontology this system produces.
"""

from __future__ import annotations

import json

import pytest
import respx

from app.services.ontology_generator import Ontology
from app.services.profile_generator import (
    ALL_OCCUPATIONS,
    OCCUPATION_SECTORS,
    PERSON_FALLBACK_TYPE,
    EntityContext,
    EntityRoles,
    PersonaProfile,
    ProfileError,
    classify_entity_types,
)
from app.utils.llm_client import LLMClient
from app.utils.retry import RetryPolicy
from tests.conftest import chat_completion

CHAT = "http://ollama:11434/v1/chat/completions"

ONTOLOGY = Ontology.model_validate({
    "domain": "Municipal housing policy",
    "entity_types": [
        {"name": "Councillor", "description": "An elected member"},
        {"name": "PlanningOfficer", "description": "A council officer"},
        {"name": "ResidentsAssociation", "description": "A residents body"},
        {"name": "PolicyDraft", "description": "A draft document"},
    ],
    "relationship_types": [],
})

ROLES = EntityRoles(
    individuals=["Councillor", "PlanningOfficer"],
    institutions=["ResidentsAssociation"],
    neither=["PolicyDraft"],
)


# --------------------------------------------------------------------------
# The occupation spectrum
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "occupation",
    ["mechanic", "carpenter", "plumber", "electrician", "welder", "care worker",
     "bus driver", "shop assistant", "cleaner", "refuse collector", "retired",
     "student", "unemployed", "full-time carer"],
)
def test_ordinary_work_is_represented(occupation):
    """A crowd of consultants and directors is not a crowd."""
    assert occupation in ALL_OCCUPATIONS


def test_professionals_are_a_minority():
    professional = OCCUPATION_SECTORS["office and professional"]
    assert len(professional) / len(ALL_OCCUPATIONS) < 0.2


def test_sampling_spreads_across_sectors(profile_generator):
    """Uniform sampling over-represents the largest sector."""
    sampled = profile_generator().sample_occupations(9)
    hit = {
        sector for sector, group in OCCUPATION_SECTORS.items()
        if any(occupation in group for occupation in sampled)
    }
    assert len(sampled) == len(set(sampled)) == 9
    assert len(hit) >= 8


def test_sampling_beyond_the_pool_still_returns_the_count(profile_generator):
    assert len(profile_generator().sample_occupations(200)) == 200


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------


def test_person_is_eligible_even_when_the_ontology_lacks_it():
    """Real ontologies produce Councillor and Mayor, never Person."""
    assert PERSON_FALLBACK_TYPE not in {t.name for t in ONTOLOGY.entity_types}
    assert PERSON_FALLBACK_TYPE in ROLES.eligible()


def test_institutions_are_opt_in():
    assert "ResidentsAssociation" not in ROLES.eligible()
    assert "ResidentsAssociation" in ROLES.eligible(include_institutions=True)


def test_documents_are_never_eligible():
    assert "PolicyDraft" not in ROLES.eligible(include_institutions=True)


def test_selection_filters_and_orders(profile_generator):
    entities = [
        {"uuid": "1", "name": "Jane Doe", "type": "Councillor", "mention_count": 5},
        {"uuid": "2", "name": "Sarah Kim", "type": "PlanningOfficer", "mention_count": 2},
        {"uuid": "3", "name": "RRA", "type": "ResidentsAssociation", "mention_count": 9},
        {"uuid": "4", "name": "Draft 2026", "type": "PolicyDraft", "mention_count": 7},
        {"uuid": "5", "name": "A Resident", "type": "Person", "mention_count": 1},
    ]
    chosen = profile_generator().select(entities, ROLES)
    assert [e["name"] for e in chosen] == ["Jane Doe", "Sarah Kim", "A Resident"]


def test_selection_can_include_institutions(profile_generator):
    entities = [{"uuid": "3", "name": "RRA", "type": "ResidentsAssociation",
                 "mention_count": 9}]
    assert profile_generator().select(entities, ROLES, include_institutions=True)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@respx.mock
async def test_classification_splits_the_ontology(config):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps({
        "individuals": ["Councillor", "PlanningOfficer"],
        "institutions": ["ResidentsAssociation"],
        "neither": ["PolicyDraft"],
    })))
    roles = await classify_entity_types(
        ONTOLOGY, LLMClient(config, retry_policy=RetryPolicy(max_attempts=1))
    )
    assert roles.individuals == ["Councillor", "PlanningOfficer"]
    assert roles.institutions == ["ResidentsAssociation"]


@respx.mock
async def test_classification_discards_invented_types(config):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps({
        "individuals": ["Councillor", "MadeUpType"], "institutions": [], "neither": [],
    })))
    roles = await classify_entity_types(
        ONTOLOGY, LLMClient(config, retry_policy=RetryPolicy(max_attempts=1))
    )
    assert roles.individuals == ["Councillor"]


@respx.mock
async def test_failed_classification_degrades_rather_than_crashing(config):
    """Person stays eligible, so generation falls back to a synthetic crowd."""
    respx.post(CHAT).mock(return_value=chat_completion("never valid"))
    roles = await classify_entity_types(
        ONTOLOGY,
        LLMClient(config, max_json_attempts=1, retry_policy=RetryPolicy(max_attempts=1)),
    )
    assert roles.individuals == []
    assert PERSON_FALLBACK_TYPE in roles.eligible()


def test_roles_round_trip_through_disk(tmp_path):
    path = ROLES.save(tmp_path / "roles.json")
    assert EntityRoles.load(path).individuals == ROLES.individuals


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


@pytest.fixture
def entity() -> EntityContext:
    return EntityContext(
        uuid="u1", name="Councillor Jane Doe", type="Councillor",
        attributes={"role": "chairs the committee"},
        passages=["Councillor Jane Doe, who chairs the committee, said the "
                  "proposal would permit four-storey development."],
        relationships=["WORKS_FOR Planning Committee"],
    )


@respx.mock
async def test_an_entity_yields_a_schema_valid_profile(profile_generator, entity, persona_data):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(persona_data)))
    profile = await profile_generator().generate_for_entity(entity)

    assert isinstance(profile, PersonaProfile)
    assert profile.age == 41
    assert profile.occupation == "carpenter"
    assert profile.background
    assert profile.writing_style


@respx.mock
async def test_required_fields_are_typed_correctly(profile_generator, entity, persona_data):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(persona_data)))
    profile = await profile_generator().generate_for_entity(entity)

    assert isinstance(profile.age, int)
    assert isinstance(profile.traits, list)
    assert isinstance(profile.interests, list)
    assert profile.activity_level in {"low", "moderate", "high"}


@respx.mock
async def test_personality_values_fall_in_range(profile_generator, entity, persona_data):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(persona_data)))
    profile = await profile_generator().generate_for_entity(entity)
    assert all(0.0 <= value <= 1.0 for value in profile.personality.model_dump().values())


@respx.mock
async def test_a_named_profile_records_its_source(profile_generator, entity, persona_data):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(persona_data)))
    profile = await profile_generator().generate_for_entity(entity)

    assert profile.provenance == "named"
    assert profile.source_entity_uuid == "u1"
    assert profile.source_entity_type == "Councillor"


@respx.mock
async def test_a_named_agent_keeps_the_graphs_name(profile_generator, entity, persona_data):
    """A model handed one name can return another; the posts would misattribute."""
    respx.post(CHAT).mock(return_value=chat_completion(
        json.dumps({**persona_data, "name": "Someone Else"})
    ))
    profile = await profile_generator().generate_for_entity(entity)
    assert profile.name == "Councillor Jane Doe"


@respx.mock
async def test_a_named_agents_gender_is_not_invented(profile_generator, entity, persona_data):
    respx.post(CHAT).mock(return_value=chat_completion(
        json.dumps({**persona_data, "gender": "female"})
    ))
    profile = await profile_generator().generate_for_entity(entity)
    assert profile.gender == "", "the document did not say"


@respx.mock
async def test_the_prompt_is_grounded_in_the_entity(profile_generator, entity, persona_data):
    route = respx.post(CHAT).mock(return_value=chat_completion(json.dumps(persona_data)))
    await profile_generator().generate_for_entity(entity)
    prompt = json.loads(route.calls[0].request.content)["messages"][-1]["content"]

    assert "Councillor Jane Doe" in prompt
    assert "four-storey" in prompt, "source passages"
    assert "chairs the committee" in prompt, "recorded attributes"
    assert "ordinary working life" in prompt, "occupation steer"


@respx.mock
async def test_generation_uses_a_high_temperature(profile_generator, entity, persona_data):
    """Near-identical personas are useless; the variation has to come from somewhere."""
    route = respx.post(CHAT).mock(return_value=chat_completion(json.dumps(persona_data)))
    await profile_generator().generate_for_entity(entity)
    assert json.loads(route.calls[0].request.content)["temperature"] == 0.8


@respx.mock
async def test_unusable_output_raises_profile_error(profile_generator, entity):
    respx.post(CHAT).mock(return_value=chat_completion("not json"))
    with pytest.raises(ProfileError):
        await profile_generator(max_json_attempts=1).generate_for_entity(entity)


@respx.mock
async def test_one_bad_entity_costs_only_that_entity(profile_generator, persona_data):
    def handler(request):
        if "Bad Entity" in request.content.decode():
            return chat_completion("garbage")
        return chat_completion(json.dumps(persona_data))

    respx.post(CHAT).mock(side_effect=handler)
    contexts = [
        EntityContext(uuid=str(i), name=name, type="Councillor")
        for i, name in enumerate(["Good One", "Bad Entity", "Good Two"])
    ]
    profiles, failures = await profile_generator(max_json_attempts=1).generate_many(contexts)

    assert len(profiles) == 2
    assert failures == ["Bad Entity"]
