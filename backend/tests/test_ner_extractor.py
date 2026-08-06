"""Phase 3 Step 4 — extraction and deduplication, with a mocked model.

The same person mentioned in eight chunks must become one node. A graph with
eight Jane Does produces eight agents who each think they are her, and a report
that counts her opinion eight times.

Deduplication is lexical because measurement showed embeddings cannot do it:
`Eastgate`/`Eastgate corridor` — which must not merge — scores 0.856, above
every genuine alias pair. Those lexical rules are what these tests pin down.
"""

from __future__ import annotations

import json

import pytest
import respx

from app.storage.ner_extractor import NERExtractor, is_alias_of, normalise_name
from app.utils.chunker import Chunk
from app.utils.llm_client import LLMClient
from app.utils.retry import RetryPolicy
from tests.conftest import chat_completion, route_by_chunk

CHAT = "http://ollama:11434/v1/chat/completions"


def extraction(entities=(), relationships=()):
    return chat_completion(json.dumps({
        "entities": list(entities), "relationships": list(relationships)
    }))


def person(name, **attrs):
    return {"name": name, "type": "Person", "attributes": attrs}


def chunks(count: int) -> list[Chunk]:
    return [
        Chunk(index=i, text=f"passage {i}", start=i * 100, end=i * 100 + 50)
        for i in range(count)
    ]


@pytest.fixture
def extractor(config, sample_ontology):
    def _make(**kwargs):
        return NERExtractor(
            sample_ontology, config,
            llm=LLMClient(config, retry_policy=RetryPolicy(max_attempts=1), **kwargs),
        )
    return _make


# --------------------------------------------------------------------------
# Name normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Cllr. Jane Doe", "Jane Doe"),
        ("Councillor Jane Doe", "Jane Doe"),
        ("the Chamber of Commerce", "Chamber of Commerce"),
        ("Mayor Alan Reyes", "Alan Reyes"),
        ("Sarah Kim.", "sarah kim"),
        ("Tom  Whitfield", "Tom Whitfield"),
        ("John Smith Jr", "John Smith"),
    ],
)
def test_names_that_must_normalise_together(left, right):
    assert normalise_name(left) == normalise_name(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Jane Doe", "John Doe"),
        ("Eastgate", "Eastgate corridor"),
        ("Jane Doe", "Jane Doe's amendment"),
        ("Riverbend Gazette", "Riverbend Residents Association"),
    ],
)
def test_names_that_must_stay_apart(left, right):
    assert normalise_name(left) != normalise_name(right)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("tom whitfield", "opposition councillor tom whitfield", True),
        ("residents association", "riverbend residents association", True),
        ("reyes", "alan reyes", False),          # one token would swallow a name
        ("eastgate", "eastgate corridor", False),
        ("mill street", "mill street conservation area", False),  # prefix, not suffix
        ("jane doe", "john doe", False),
    ],
)
def test_multi_token_suffix_alias_rule(left, right, expected):
    assert is_alias_of(left, right) is expected


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


@respx.mock
async def test_entities_extract_from_a_chunk(extractor):
    respx.post(CHAT).mock(return_value=extraction(
        entities=[person("Jane Doe", role="chair"),
                  {"name": "Riverbend Council", "type": "Organisation"}],
        relationships=[{"source": "Jane Doe", "target": "Riverbend Council",
                        "type": "WORKS_FOR"}],
    ))
    result = await extractor().extract(chunks(1))

    assert len(result.entities) == 2
    assert result.entities[0].attributes == {"role": "chair"}
    assert result.entities[0].mentions[0].chunk_index == 0
    assert all(e.uuid for e in result.entities)


@respx.mock
async def test_relationship_endpoints_resolve_to_entities(extractor):
    respx.post(CHAT).mock(return_value=extraction(
        entities=[person("Jane Doe"), {"name": "Riverbend Council", "type": "Organisation"}],
        relationships=[{"source": "Jane Doe", "target": "Riverbend Council",
                        "type": "WORKS_FOR"}],
    ))
    result = await extractor().extract(chunks(1))
    by_uuid = {e.uuid: e for e in result.entities}
    edge = result.relationships[0]
    assert by_uuid[edge.source_uuid].name == "Jane Doe"
    assert by_uuid[edge.target_uuid].name == "Riverbend Council"


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------


@respx.mock
async def test_one_person_across_eight_chunks_becomes_one_node(extractor):
    variants = ["Jane Doe", "Cllr Jane Doe", "Councillor Jane Doe", "Jane Doe",
                "Mayor Jane Doe", "the Jane Doe", "Jane Doe.", "JANE DOE"]
    respx.post(CHAT).mock(side_effect=route_by_chunk({
        f"passage {i}": extraction(entities=[person(v)])
        for i, v in enumerate(variants)
    }))
    result = await extractor().extract(chunks(8))

    assert len(result.entities) == 1
    assert result.entities[0].mention_count == 8
    assert result.entities[0].name == "Jane Doe", "canonical name is the first seen"
    assert len(result.entities[0].aliases) >= 5
    assert result.merged_by_name == 7


@respx.mock
async def test_distinct_people_stay_distinct(extractor):
    names = ["Jane Doe", "John Doe", "Alan Reyes"]
    respx.post(CHAT).mock(side_effect=route_by_chunk({
        f"passage {i}": extraction(entities=[person(n)]) for i, n in enumerate(names)
    }))
    assert len((await extractor().extract(chunks(3))).entities) == 3


@respx.mock
async def test_same_name_under_different_types_stays_separate(extractor):
    """A wrong merge cannot be undone downstream."""
    respx.post(CHAT).mock(side_effect=route_by_chunk({
        "passage 0": extraction(entities=[{"name": "Eastgate", "type": "Location"}]),
        "passage 1": extraction(entities=[{"name": "Eastgate", "type": "Organisation"}]),
    }))
    assert len((await extractor().extract(chunks(2))).entities) == 2


@respx.mock
async def test_suffix_aliases_merge(extractor):
    respx.post(CHAT).mock(side_effect=route_by_chunk({
        "passage 0": extraction(entities=[person("Opposition councillor Tom Whitfield")]),
        "passage 1": extraction(entities=[person("Tom Whitfield")]),
    }))
    result = await extractor().extract(chunks(2))
    assert len(result.entities) == 1
    assert result.merged_by_alias == 1


# --------------------------------------------------------------------------
# Attribute conflicts
# --------------------------------------------------------------------------


@respx.mock
async def test_first_occurrence_wins_and_conflicts_are_recorded(extractor):
    respx.post(CHAT).mock(side_effect=route_by_chunk({
        "passage 0": extraction(entities=[person("Jane Doe", role="chair", ward="Eastgate")]),
        "passage 1": extraction(entities=[person("Jane Doe", role="committee chair")]),
        "passage 2": extraction(entities=[person("Jane Doe", role="chairperson")]),
    }))
    entity = (await extractor().extract(chunks(3))).entities[0]

    assert entity.attributes["role"] == "chair"
    assert entity.attributes["ward"] == "Eastgate"
    assert [c["value"] for c in entity.attribute_conflicts["role"]] == [
        "committee chair", "chairperson"
    ]
    assert entity.attribute_conflicts["role"][0]["chunk"] == 1


@respx.mock
async def test_conflict_resolution_is_deterministic(extractor):
    """Chunks fan out concurrently; 'first' must mean earliest, not fastest."""
    async def run():
        with respx.mock:
            respx.post(CHAT).mock(side_effect=route_by_chunk({
                f"passage {i}": extraction(entities=[person("Jane Doe", role=f"r{i}")])
                for i in range(4)
            }))
            return (await extractor().extract(chunks(4))).entities[0]

    first, second = await run(), await run()
    assert first.attributes == second.attributes == {"role": "r0"}
    assert first.attribute_conflicts == second.attribute_conflicts


# --------------------------------------------------------------------------
# Ontology conformance
# --------------------------------------------------------------------------


@respx.mock
async def test_off_ontology_and_nameless_entities_are_dropped(extractor):
    respx.post(CHAT).mock(return_value=extraction(entities=[
        person("Jane Doe"),
        {"name": "Some Vibe", "type": "Vibe"},
        {"name": "", "type": "Person"},
    ]))
    result = await extractor().extract(chunks(1))
    assert [e.name for e in result.entities] == ["Jane Doe"]
    assert result.dropped_off_ontology >= 2


@respx.mock
async def test_off_ontology_and_self_loop_edges_are_dropped(extractor):
    respx.post(CHAT).mock(return_value=extraction(
        entities=[person("Jane Doe")],
        relationships=[
            {"source": "Jane Doe", "target": "Jane Doe", "type": "ENJOYS"},
            {"source": "Jane Doe", "target": "Jane Doe", "type": "WORKS_FOR"},
        ],
    ))
    result = await extractor().extract(chunks(1))
    assert result.relationships == []
    assert result.dropped_unresolved >= 1


@respx.mock
async def test_unlisted_endpoint_is_materialised_when_grounded(extractor):
    """Models name endpoints they never return; strict resolution loses the edge."""
    respx.post(CHAT).mock(return_value=extraction(
        entities=[person("Jane Doe")],
        relationships=[{"source": "Jane Doe", "target": "passage", "type": "WORKS_FOR"}],
    ))
    result = await extractor().extract([
        Chunk(index=0, text="Jane Doe works for passage", start=0, end=26)
    ])
    assert result.inferred_entities == 1
    assert len(result.relationships) == 1
    assert any(e.inferred for e in result.entities)


@respx.mock
async def test_ungrounded_endpoint_is_not_invented(extractor):
    """The guard between recovering a real name and inventing 'Nobody'."""
    respx.post(CHAT).mock(return_value=extraction(
        entities=[person("Jane Doe")],
        relationships=[{"source": "Jane Doe", "target": "Nobody", "type": "WORKS_FOR"}],
    ))
    result = await extractor().extract(chunks(1))
    assert result.inferred_entities == 0
    assert result.relationships == []


@respx.mock
async def test_duplicate_relationships_merge_into_one_edge(extractor):
    respx.post(CHAT).mock(side_effect=route_by_chunk({
        "passage 0": extraction(
            entities=[person("Jane Doe"), {"name": "Riverbend Council", "type": "Organisation"}],
            relationships=[{"source": "Jane Doe", "target": "Riverbend Council",
                            "type": "WORKS_FOR"}]),
        "passage 1": extraction(
            entities=[person("Cllr Jane Doe"),
                      {"name": "the Riverbend Council", "type": "Organisation"}],
            relationships=[{"source": "Cllr Jane Doe", "target": "the Riverbend Council",
                            "type": "works for"}]),
    }))
    result = await extractor().extract(chunks(2))
    assert len(result.entities) == 2
    assert len(result.relationships) == 1
    assert result.relationships[0].type == "WORKS_FOR"
    assert len(result.relationships[0].mentions) == 2


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


@respx.mock
async def test_one_bad_chunk_costs_that_chunk_only(extractor):
    respx.post(CHAT).mock(side_effect=route_by_chunk({
        "passage 0": extraction(entities=[person("Jane Doe")]),
        "passage 1": lambda: chat_completion("garbage"),
        "passage 2": extraction(entities=[person("Alan Reyes")]),
    }))
    result = await extractor().extract(chunks(3))
    assert result.chunks_failed == 1
    assert result.chunks_processed == 2
    assert len(result.entities) == 2


async def test_no_chunks_yields_an_empty_result(extractor):
    result = await extractor().extract([])
    assert result.entities == [] and result.relationships == []


@respx.mock
async def test_an_empty_extraction_is_not_an_error(extractor):
    respx.post(CHAT).mock(return_value=extraction())
    result = await extractor().extract(chunks(2))
    assert result.chunks_processed == 2
    assert result.entities == []
