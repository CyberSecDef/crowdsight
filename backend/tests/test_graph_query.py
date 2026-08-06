"""Phase 3 Step 6 — querying and searching a built graph, against live services.

Not in the spec's list of test files, but Step 6 built `graph_storage.py` and
`search_service.py` and their deliberate behaviours — traversal caps, graph
scoping, the lexical-before-vector ranking — would otherwise regress unnoticed.

Marked `integration`: needs Neo4j, and the search tests need Ollama for
embeddings.
"""

from __future__ import annotations

import pytest

from app.services.graph_builder import GraphBuilder, derive_uuid
from app.storage.embedding_service import EmbeddingService
from app.storage.graph_storage import GraphStorage
from app.storage.ner_extractor import Entity, ExtractionResult, Mention, Relationship
from app.storage.search_service import SearchService
from app.utils.chunker import chunk_text
from app.utils.file_parser import ParsedDocument

pytestmark = pytest.mark.integration

PEOPLE = ["Councillor Jane Doe", "Mayor Alan Reyes", "Tom Whitfield", "Sarah Kim"]
ORGS = ["Riverbend Residents Association", "Chamber of Commerce", "Planning Committee"]
PLACES = ["Eastgate corridor", "Northfield ward"]


def _entity(name: str, entity_type: str, chunk, vector) -> Entity:
    normalised = name.lower()
    return Entity(
        uuid="tmp-" + normalised.replace(" ", "-"), name=name, normalised=normalised,
        type=entity_type, aliases={name},
        mentions=[Mention(0, chunk.start, chunk.end, name)], embedding=vector,
    )


@pytest.fixture
async def populated(storage, integration_config, tmp_path, graph_ns, council_text):
    """A small graph with people, organisations, places and four edges."""
    chunks = chunk_text(council_text, config=integration_config)
    embeddings = EmbeddingService(integration_config, cache=None)
    names = PEOPLE + ORGS + PLACES
    vectors = await embeddings.embed_texts(names)

    entities = [
        _entity(name, kind, chunks[0], vector)
        for name, kind, vector in zip(
            names,
            ["Person"] * 4 + ["Organisation"] * 3 + ["Location"] * 2,
            vectors,
        )
    ]
    index = {e.name: e for e in entities}
    mention = [Mention(0, chunks[0].start, chunks[0].end, "edge")]
    relationships = [
        Relationship(uuid="r1", type="WORKS_FOR",
                     source_uuid=index["Councillor Jane Doe"].uuid,
                     target_uuid=index["Planning Committee"].uuid, mentions=mention),
        Relationship(uuid="r2", type="WORKS_FOR", source_uuid=index["Sarah Kim"].uuid,
                     target_uuid=index["Planning Committee"].uuid, mentions=mention),
        Relationship(uuid="r3", type="OPPOSES",
                     source_uuid=index["Riverbend Residents Association"].uuid,
                     target_uuid=index["Planning Committee"].uuid, mentions=mention),
        Relationship(uuid="r4", type="WORKS_FOR", source_uuid=index["Mayor Alan Reyes"].uuid,
                     target_uuid=index["Chamber of Commerce"].uuid, mentions=mention),
    ]

    builder = GraphBuilder(storage, integration_config, data_dir=tmp_path / "graphs",
                           embeddings=embeddings)
    document = ParsedDocument(text=council_text, filename="council.txt", extension="txt",
                              byte_size=len(council_text.encode()),
                              char_count=len(council_text))
    from app.services.ontology_generator import Ontology

    ontology = Ontology.model_validate({
        "entity_types": [{"name": t, "description": t} for t in
                         ("Person", "Organisation", "Location")],
        "relationship_types": [
            {"name": "WORKS_FOR", "description": "Employment",
             "source_types": ["Person"], "target_types": ["Organisation"]},
            {"name": "OPPOSES", "description": "Opposition",
             "source_types": ["Organisation"], "target_types": ["Organisation"]},
        ],
    })
    await builder.build(graph_id=graph_ns, document=document, chunks=chunks,
                        ontology=ontology, extraction=ExtractionResult(
                            entities=entities, relationships=relationships),
                        replace=True)

    graphs = GraphStorage(storage, integration_config, data_dir=tmp_path / "graphs")
    search = SearchService(storage, integration_config, embeddings=embeddings,
                           graphs=graphs)
    try:
        yield graphs, search, chunks
    finally:
        await builder.delete(graph_ns)
        await embeddings.aclose()


# --------------------------------------------------------------------------
# Fetching and pagination
# --------------------------------------------------------------------------


async def test_graph_metadata(populated, graph_ns):
    graphs, _, _ = populated
    meta = await graphs.get_graph(graph_ns)
    assert meta["entity_count"] == 9
    assert meta["filename"] == "council.txt"


async def test_unknown_graph_returns_none(populated):
    graphs, _, _ = populated
    assert await graphs.get_graph("no-such-graph") is None


async def test_entity_fetch_is_graph_scoped(populated, graph_ns):
    """A query that forgets the scope silently returns another document's data."""
    graphs, _, _ = populated
    uuid = derive_uuid(graph_ns, "Person", "councillor jane doe")
    assert (await graphs.get_entity(graph_ns, uuid))["name"] == "Councillor Jane Doe"
    assert await graphs.get_entity("another-graph", uuid) is None


async def test_entity_omits_its_embedding(populated, graph_ns):
    graphs, _, _ = populated
    uuid = derive_uuid(graph_ns, "Person", "councillor jane doe")
    assert "embedding" not in await graphs.get_entity(graph_ns, uuid)


async def test_pagination_does_not_overlap(populated, graph_ns):
    graphs, _, _ = populated
    first = await graphs.list_entities(graph_ns, limit=4)
    last = await graphs.list_entities(graph_ns, limit=4, offset=8)

    assert first.total == 9 and len(first.items) == 4 and first.has_more
    assert len(last.items) == 1 and not last.has_more
    assert not ({i["uuid"] for i in first.items} & {i["uuid"] for i in last.items})


async def test_page_size_is_clamped(populated, graph_ns):
    graphs, _, _ = populated
    assert (await graphs.list_entities(graph_ns, limit=99999)).limit <= 500


@pytest.mark.parametrize(
    ("types", "expected"), [(["Person"], 4), (["Person", "Location"], 6), (["Location"], 2)]
)
async def test_filter_by_type(populated, graph_ns, types, expected):
    graphs, _, _ = populated
    page = await graphs.list_entities(graph_ns, types=types)
    assert page.total == expected
    assert all(item["type"] in types for item in page.items)


async def test_entity_type_counts(populated, graph_ns):
    graphs, _, _ = populated
    counts = {row["type"]: row["count"] for row in await graphs.entity_types(graph_ns)}
    assert counts == {"Person": 4, "Organisation": 3, "Location": 2}


async def test_relationships_listed_and_filtered(populated, graph_ns):
    graphs, _, _ = populated
    everything = await graphs.list_relationships(graph_ns)
    assert everything.total == 4
    assert all(r["source_name"] and r["target_name"] for r in everything.items)

    jane = derive_uuid(graph_ns, "Person", "councillor jane doe")
    assert (await graphs.list_relationships(graph_ns, uuid=jane)).total == 1


# --------------------------------------------------------------------------
# Traversal
# --------------------------------------------------------------------------


async def test_neighbourhood_depth_and_edges(populated, graph_ns):
    graphs, _, _ = populated
    hub = derive_uuid(graph_ns, "Organisation", "planning committee")
    result = await graphs.neighbours(graph_ns, hub, depth=1)
    assert len(result.nodes) == 4
    assert len(result.edges) == 3


async def test_depth_two_reaches_further(populated, graph_ns):
    graphs, _, _ = populated
    jane = derive_uuid(graph_ns, "Person", "councillor jane doe")
    near = await graphs.neighbours(graph_ns, jane, depth=1)
    far = await graphs.neighbours(graph_ns, jane, depth=2)
    assert len(far.nodes) > len(near.nodes)


async def test_traversal_does_not_route_through_chunks(populated, graph_ns):
    """Otherwise two people in the same passage become neighbours."""
    graphs, _, _ = populated
    jane = derive_uuid(graph_ns, "Person", "councillor jane doe")
    result = await graphs.neighbours(graph_ns, jane, depth=3)
    assert all(n["type"] in {"Person", "Organisation", "Location"} for n in result.nodes)


async def test_unconnected_entity_returns_itself(populated, graph_ns):
    graphs, _, _ = populated
    lonely = derive_uuid(graph_ns, "Location", "northfield ward")
    assert len((await graphs.neighbours(graph_ns, lonely, depth=2)).nodes) == 1


async def test_unknown_entity_returns_an_empty_subgraph(populated, graph_ns):
    graphs, _, _ = populated
    assert (await graphs.neighbours(graph_ns, "no-such-uuid")).nodes == []


async def test_excessive_depth_is_clamped_not_rejected(populated, graph_ns):
    graphs, _, _ = populated
    hub = derive_uuid(graph_ns, "Organisation", "planning committee")
    assert len((await graphs.neighbours(graph_ns, hub, depth=99)).nodes) >= 4


async def test_node_limit_truncates_and_says_so(populated, graph_ns):
    """A visualisation asked to render an unbounded neighbourhood hangs."""
    graphs, _, _ = populated
    hub = derive_uuid(graph_ns, "Organisation", "planning committee")
    result = await graphs.neighbours(graph_ns, hub, depth=2, limit=2)
    assert len(result.nodes) == 2 and result.truncated


async def test_whole_subgraph_and_its_cap(populated, graph_ns):
    graphs, _, _ = populated
    whole = await graphs.subgraph(graph_ns)
    assert len(whole.nodes) == 9 and len(whole.edges) == 4 and not whole.truncated

    capped = await graphs.subgraph(graph_ns, limit=3)
    assert len(capped.nodes) == 3 and capped.truncated


async def test_entity_chunks_return_source_text(populated, graph_ns, council_text):
    graphs, _, _ = populated
    jane = derive_uuid(graph_ns, "Person", "councillor jane doe")
    rows = await graphs.entity_chunks(graph_ns, jane)
    assert rows and rows[0]["text"] == council_text[rows[0]["start"] : rows[0]["end"]]


# --------------------------------------------------------------------------
# Hybrid search
# --------------------------------------------------------------------------


async def test_a_surname_query_returns_the_right_person_first(populated, graph_ns):
    """Pure vector search is unreliable here; the lexical arm carries it."""
    _, search, _ = populated
    hits = await search.search_entities(graph_ns, "Whitfield")
    assert hits[0].name == "Tom Whitfield"


async def test_exact_match_is_ranked_and_labelled(populated, graph_ns):
    _, search, _ = populated
    hits = await search.search_entities(graph_ns, "chamber of commerce")
    assert hits[0].name == "Chamber of Commerce"
    assert hits[0].matched_by == "exact"


async def test_natural_language_query_uses_the_vector_arm(populated, graph_ns):
    _, search, _ = populated
    hits = await search.search_entities(graph_ns, "who objected to the housing timetable")
    assert hits
    assert any(hit.matched_by == "vector" for hit in hits)


async def test_search_respects_a_type_filter(populated, graph_ns):
    _, search, _ = populated
    hits = await search.search_entities(graph_ns, "a", types=["Location"], limit=10)
    assert all(hit.type == "Location" for hit in hits)


async def test_search_is_graph_scoped(populated, graph_ns):
    _, search, _ = populated
    foreign = derive_uuid("another-graph", "Person", "councillor jane doe")
    hits = await search.search_entities(graph_ns, "Jane")
    assert all(hit.uuid != foreign for hit in hits)


async def test_empty_query_returns_nothing(populated, graph_ns):
    _, search, _ = populated
    assert await search.search_entities(graph_ns, "   ") == []


async def test_passage_search_returns_real_source_text(populated, graph_ns, council_text):
    """This is what Phase 8 cites."""
    _, search, _ = populated
    hits = await search.search_chunks(graph_ns, "objections to the consultation period", limit=2)
    assert hits
    assert all(hit.text == council_text[hit.start : hit.end] for hit in hits)
    assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))


async def test_passage_search_without_embeddings_returns_nothing(
    populated, storage, integration_config, graph_ns
):
    graphs, _, _ = populated
    bare = SearchService(storage, integration_config, embeddings=None, graphs=graphs)
    assert await bare.search_chunks(graph_ns, "anything") == []
