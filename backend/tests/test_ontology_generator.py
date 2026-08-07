"""Phase 3 Step 3 — proposing a domain ontology, with a mocked model.

The load-bearing behaviour is normalisation. A model asked for entity types
answers "Public Figure", which `escape_identifier` rejects, so the graph could
not store it. Re-prompting would spend 30-90 s of local inference on something
a two-line transform handles.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from pydantic import ValidationError

from app.services.ontology_generator import (
    Ontology,
    OntologyError,
    OntologyGenerator,
    build_sample,
    to_identifier,
)
from app.storage.neo4j_storage import escape_identifier
from app.utils.llm_client import LLMClient
from app.utils.retry import RetryPolicy
from tests.conftest import chat_completion

CHAT = "http://ollama:11434/v1/chat/completions"

VALID = {
    "domain": "Municipal housing policy",
    "entity_types": [
        {"name": "Person", "description": "An individual", "attributes": ["role"]},
        {"name": "Public Figure", "description": "An elected official",
         "attributes": ["office", "Party Name"]},
        {"name": "Organisation", "description": "A body", "attributes": []},
    ],
    "relationship_types": [
        {"name": "works for", "description": "Employment",
         "source_types": ["Person"], "target_types": ["Organisation"]},
        {"name": "OPPOSES", "description": "Opposition",
         "source_types": ["Public Figure"], "target_types": ["Organisation"]},
    ],
}


def generator(config, **kwargs):
    return OntologyGenerator(
        config,
        llm=LLMClient(config, retry_policy=RetryPolicy(max_attempts=1), **kwargs),
    )


# --------------------------------------------------------------------------
# Identifier normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Public Figure", "PublicFigure"),
        ("local government body", "LocalGovernmentBody"),
        ("Person", "Person"),
        ("publicFigure", "PublicFigure"),
        ("Housing-Policy", "HousingPolicy"),
        ("  spaced  out  ", "SpacedOut"),
        ("123", ""),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_entity_identifiers(raw, expected):
    assert to_identifier(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [("works for", "WORKS_FOR"), ("WORKS_FOR", "WORKS_FOR")]
)
def test_relationship_identifiers(raw, expected):
    assert to_identifier(raw, upper_first=False) == expected


def test_identifier_is_length_capped():
    assert len(to_identifier("a" * 200)) <= 63


# --------------------------------------------------------------------------
# The shared contract with the frontend
# --------------------------------------------------------------------------
#
# Phase 9 Step 2's ontology editor shows what a typed name will become before
# it is submitted, which means this rule is implemented twice — here, and in
# frontend/src/api/ontology.js. Two implementations of one rule drift, and the
# symptom would be a graph carrying type names the operator did not choose.
# Both suites assert against the same fixture, so a change to either side that
# is not a change to the other fails immediately.

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "identifier_cases.json").read_text())


@pytest.mark.parametrize(("raw", "expected"), [tuple(c) for c in _CASES["entity"]])
def test_ENTITY_IDENTIFIERS_MATCH_THE_SHARED_CONTRACT(raw, expected):
    assert to_identifier(raw) == expected


@pytest.mark.parametrize(("raw", "expected"),
                         [tuple(c) for c in _CASES["relationship"]])
def test_RELATIONSHIP_IDENTIFIERS_MATCH_THE_SHARED_CONTRACT(raw, expected):
    assert to_identifier(raw, upper_first=False) == expected


def test_the_shared_contract_covers_both_kinds():
    """A fixture that quietly lost its cases would pass vacuously."""
    assert len(_CASES["entity"]) >= 15
    assert len(_CASES["relationship"]) >= 8


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


@pytest.fixture
def ontology() -> Ontology:
    return Ontology.model_validate(VALID)


def test_names_normalise_and_labels_survive(ontology):
    assert [e.name for e in ontology.entity_types] == [
        "Person", "PublicFigure", "Organisation"
    ]
    assert "Public Figure" in [e.label for e in ontology.entity_types]


def test_relationship_names_and_endpoints_normalise(ontology):
    assert [r.name for r in ontology.relationship_types] == ["WORKS_FOR", "OPPOSES"]
    assert ontology.relationship_types[1].source_types == ["PublicFigure"]


def test_attributes_are_snake_cased(ontology):
    assert ontology.entity_types[1].attributes == ["office", "party_name"]


def test_every_name_is_a_legal_cypher_identifier(ontology):
    """The graph could not store a name that fails this."""
    for entity in ontology.entity_types:
        escape_identifier(entity.name)
    for relationship in ontology.relationship_types:
        escape_identifier(relationship.name)


def test_relationship_with_unknown_endpoint_is_dropped():
    """It could not be extracted, and would fail during ingestion instead."""
    payload = dict(VALID)
    payload["relationship_types"] = VALID["relationship_types"] + [
        {"name": "GOVERNS", "description": "x",
         "source_types": ["Person"], "target_types": ["Nonexistent"]}
    ]
    result = Ontology.model_validate(payload)
    assert [r.name for r in result.relationship_types] == ["WORKS_FOR", "OPPOSES"]


def test_duplicate_types_collapse():
    payload = {"entity_types": [
        {"name": "Person", "description": "a"},
        {"name": "person", "description": "b"},
        {"name": "Person", "description": "c"},
    ]}
    assert len(Ontology.model_validate(payload).entity_types) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"entity_types": []},
        {"entity_types": [{"name": "123", "description": "x"}]},
        {"entity_types": [{"name": "Person"}]},
    ],
)
def test_invalid_ontologies_rejected(payload):
    with pytest.raises(ValidationError):
        Ontology.model_validate(payload)


def test_ontology_round_trips_through_disk(ontology, tmp_path):
    path = ontology.save(tmp_path / "g1" / "ontology.json")
    assert path.exists()
    assert Ontology.load(path).model_dump() == ontology.model_dump()


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------


def test_short_document_is_used_whole(config):
    text = "A short document about Jane Doe."
    assert build_sample(text, config=config) == text


@pytest.fixture
def long_document() -> str:
    para = ("The council approved the housing policy on Tuesday. "
            "Councillor Jane Doe spoke in favour of the measure. ")
    return ("OPENING SECTION. " + para * 60
            + "MIDDLE SECTION. " + para * 60
            + "CLOSING SECTION. " + para * 60)


def test_sample_spans_the_whole_document(long_document, config):
    """An ontology from the first page describes the cover."""
    sample = build_sample(long_document, budget=3000, config=config)
    assert "OPENING SECTION" in sample
    assert "MIDDLE SECTION" in sample
    assert "CLOSING SECTION" in sample


def test_sample_respects_its_budget(long_document, config):
    sample = build_sample(long_document, budget=3000, config=config)
    assert len(sample) <= 3600


def test_sample_marks_elisions(long_document, config):
    """So the model does not read distant passages as consecutive."""
    assert "[...]" in build_sample(long_document, budget=3000, config=config)


def test_sample_is_not_merely_the_first_n_chars(long_document, config):
    sample = build_sample(long_document, budget=3000, config=config)
    assert sample != long_document[:3000]


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


@respx.mock
async def test_valid_json_produces_an_ontology(config):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(VALID)))
    result = await generator(config).generate("A document about housing policy.")
    assert isinstance(result, Ontology)
    assert [e.name for e in result.entity_types] == [
        "Person", "PublicFigure", "Organisation"
    ]


@respx.mock
async def test_prompt_carries_the_sample_and_a_low_temperature(config):
    route = respx.post(CHAT).mock(return_value=chat_completion(json.dumps(VALID)))
    await generator(config).generate("housing policy and councillors")
    payload = json.loads(route.calls[0].request.content)
    assert "housing policy and councillors" in payload["messages"][-1]["content"]
    assert payload["temperature"] == 0.2, "creativity here invents types"


@respx.mock
async def test_repair_loop_recovers_from_malformed_then_invalid(config):
    route = respx.post(CHAT).mock(side_effect=[
        chat_completion("Here is your ontology:"),
        chat_completion(json.dumps({"entity_types": []})),
        chat_completion(json.dumps(VALID)),
    ])
    result = await generator(config, max_json_attempts=3).generate("doc")
    assert isinstance(result, Ontology)
    assert route.call_count == 3


@respx.mock
async def test_exhausted_repairs_raise_ontology_error(config):
    respx.post(CHAT).mock(return_value=chat_completion("never valid"))
    with pytest.raises(OntologyError):
        await generator(config, max_json_attempts=2).generate("doc")


async def test_empty_document_is_rejected(config):
    with pytest.raises(OntologyError):
        await generator(config).generate("   ")
