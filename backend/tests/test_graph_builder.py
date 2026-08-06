"""Phase 3 Step 5 — persisting a graph, against a live Neo4j.

Marked `integration`: run with `pytest -m integration` after
`docker compose up -d neo4j`.

Two properties carry the step. Provenance must trace a node back to the text
that produced it, and rebuilding the same document must be idempotent — which
is only true because identifiers are derived from content rather than random.
"""

from __future__ import annotations

import pytest

from app.services.graph_builder import GraphBuilder, GraphNotFound, derive_uuid
from app.storage.ner_extractor import Entity, ExtractionResult, Mention, Relationship
from app.utils.chunker import chunk_text
from app.utils.file_parser import ParsedDocument

pytestmark = pytest.mark.integration


@pytest.fixture
def chunks(council_text, integration_config):
    return chunk_text(council_text, config=integration_config)


@pytest.fixture
def document(council_text) -> ParsedDocument:
    return ParsedDocument(
        text=council_text, filename="council.txt", extension="txt",
        byte_size=len(council_text.encode()), char_count=len(council_text),
    )


@pytest.fixture
def extraction(chunks):
    """Two entities and an edge, with a conflict and an inferred node."""
    def _make(entities: int = 2):
        jane = Entity(
            uuid="tmp-jane", name="Councillor Jane Doe", normalised="jane doe",
            type="Person", attributes={"role": "chair"},
            attribute_conflicts={"role": [{"value": "committee chair", "chunk": 1}]},
            aliases={"Councillor Jane Doe", "Cllr Jane Doe"},
            mentions=[
                Mention(0, chunks[0].start, chunks[0].end, "Councillor Jane Doe"),
                Mention(min(1, len(chunks) - 1), chunks[-1].start, chunks[-1].end,
                        "Cllr Jane Doe"),
            ],
            embedding=[0.01] * 768,
        )
        if entities == 1:
            return ExtractionResult(entities=[jane], relationships=[])
        council = Entity(
            uuid="tmp-council", name="Riverbend City Council",
            normalised="riverbend city council", type="Organisation",
            aliases={"Riverbend City Council"},
            mentions=[Mention(0, chunks[0].start, chunks[0].end, "Riverbend City Council")],
            embedding=[0.02] * 768, inferred=True,
        )
        edge = Relationship(
            uuid="tmp-edge", type="WORKS_FOR", source_uuid="tmp-jane",
            target_uuid="tmp-council",
            mentions=[Mention(0, chunks[0].start, chunks[0].end, "WORKS_FOR")],
        )
        return ExtractionResult(entities=[jane, council], relationships=[edge])
    return _make


@pytest.fixture
async def builder(storage, integration_config, tmp_path, graph_ns):
    gb = GraphBuilder(storage, integration_config, data_dir=tmp_path / "graphs")
    yield gb
    await gb.delete(graph_ns)


@pytest.fixture
async def built(builder, graph_ns, document, chunks, sample_ontology, extraction):
    return await builder.build(
        graph_id=graph_ns, document=document, chunks=chunks,
        ontology=sample_ontology, extraction=extraction(),
    )


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


async def test_build_reports_what_it_wrote(built, chunks):
    assert built.entities == 2
    assert built.relationships == 1
    assert built.chunks == len(chunks)
    assert built.mentions == 3


async def test_entities_persist_and_are_retrievable(built, storage, graph_ns):
    rows = await storage.read(
        "MATCH (e:Entity {graph_id:$g}) RETURN e.name AS name ORDER BY name", g=graph_ns
    )
    assert [r["name"] for r in rows] == ["Councillor Jane Doe", "Riverbend City Council"]


async def test_entity_properties_round_trip(built, storage, graph_ns):
    rows = await storage.read(
        "MATCH (e:Entity {graph_id:$g, name:$n}) "
        "RETURN e.attr_role AS role, e.aliases AS aliases, e.inferred AS inferred, "
        "       e.attribute_conflicts AS conflicts, size(e.embedding) AS dim",
        g=graph_ns, n="Councillor Jane Doe",
    )
    row = rows[0]
    assert row["role"] == "chair", "attributes stored under an attr_ prefix"
    assert sorted(row["aliases"]) == ["Cllr Jane Doe", "Councillor Jane Doe"]
    assert "committee chair" in row["conflicts"]
    assert row["dim"] == 768
    assert row["inferred"] is False


async def test_ontology_type_becomes_a_second_label(built, storage, graph_ns):
    """So a visualisation can match (:Person) directly."""
    rows = await storage.read("MATCH (e:Person {graph_id:$g}) RETURN e.name AS n", g=graph_ns)
    assert len(rows) == 1


async def test_relationship_uses_the_ontology_type_as_its_edge_type(built, storage, graph_ns):
    rows = await storage.read(
        "MATCH (:Entity {graph_id:$g})-[r:WORKS_FOR]->(:Entity) "
        "RETURN r.mention_count AS m", g=graph_ns
    )
    assert len(rows) == 1 and rows[0]["m"] == 1


async def test_chunks_attach_to_the_document(built, storage, graph_ns, chunks):
    rows = await storage.read(
        "MATCH (c:Chunk {graph_id:$g})-[:PART_OF]->(d:Document {graph_id:$g}) "
        "RETURN count(c) AS chunks, d.filename AS filename", g=graph_ns
    )
    assert rows[0]["chunks"] == len(chunks)
    assert rows[0]["filename"] == "council.txt"


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


async def test_provenance_links_back_to_source_chunks(built, builder, graph_ns, council_text):
    """The property the whole step exists for."""
    uuid = derive_uuid(graph_ns, "Person", "jane doe")
    provenance = await builder.provenance(graph_ns, uuid)

    assert len(provenance) == 2
    assert sorted(p["surface"] for p in provenance) == [
        "Cllr Jane Doe", "Councillor Jane Doe"
    ]
    assert all(p["text"] == council_text[p["start"] : p["end"]] for p in provenance)
    assert any("Jane Doe" in p["text"] for p in provenance)


async def test_provenance_of_an_unknown_entity_is_empty(built, builder, graph_ns):
    assert await builder.provenance(graph_ns, "no-such-uuid") == []


async def test_document_and_ontology_are_written_to_disk(built, builder, graph_ns, council_text):
    assert builder.document_path(graph_ns).is_file()
    assert builder.load_document(graph_ns) == council_text
    assert builder.ontology_path(graph_ns).is_file()


def test_missing_document_raises(builder):
    with pytest.raises(GraphNotFound):
        builder.load_document("never-built")


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


async def test_rebuilding_creates_no_new_nodes(
    built, builder, storage, graph_ns, document, chunks, sample_ontology, extraction
):
    async def census():
        return await storage.read(
            "MATCH (n) WHERE n.graph_id=$g RETURN labels(n)[0] AS label, count(n) AS count "
            "ORDER BY label", g=graph_ns
        )

    before = await census()
    await builder.build(graph_id=graph_ns, document=document, chunks=chunks,
                        ontology=sample_ontology, extraction=extraction())
    assert await census() == before


async def test_rebuilding_creates_no_duplicate_edges_or_mentions(
    built, builder, storage, graph_ns, document, chunks, sample_ontology, extraction
):
    await builder.build(graph_id=graph_ns, document=document, chunks=chunks,
                        ontology=sample_ontology, extraction=extraction())
    edges = await storage.read(
        "MATCH (:Entity {graph_id:$g})-[r:WORKS_FOR]->() RETURN count(r) AS c", g=graph_ns
    )
    mentions = await storage.read(
        "MATCH (:Entity {graph_id:$g})-[m:MENTIONED_IN]->() RETURN count(m) AS c", g=graph_ns
    )
    assert edges[0]["c"] == 1
    assert mentions[0]["c"] == 3


def test_uuid_derivation_is_deterministic_and_graph_scoped(graph_ns):
    """Random identifiers would double every node on rebuild."""
    first = derive_uuid(graph_ns, "Person", "jane doe")
    assert first == derive_uuid(graph_ns, "Person", "jane doe")
    assert first != derive_uuid("other-graph", "Person", "jane doe")
    assert first != derive_uuid(graph_ns, "Organisation", "jane doe")


async def test_replace_drops_entities_no_longer_extracted(
    built, builder, storage, graph_ns, document, chunks, sample_ontology, extraction
):
    await builder.build(graph_id=graph_ns, document=document, chunks=chunks,
                        ontology=sample_ontology, extraction=extraction(entities=1),
                        replace=True)
    rows = await storage.read(
        "MATCH (e:Entity {graph_id:$g}) RETURN count(e) AS c", g=graph_ns
    )
    assert rows[0]["c"] == 1


# --------------------------------------------------------------------------
# Isolation and deletion
# --------------------------------------------------------------------------


async def test_stats_and_deletion(
    built, builder, storage, graph_ns, document, chunks, sample_ontology, extraction
):
    other = graph_ns + "-other"
    await builder.build(graph_id=other, document=document, chunks=chunks,
                        ontology=sample_ontology, extraction=extraction())

    stats = await builder.stats(graph_ns)
    assert stats["entities"] == 2 and stats["relationships"] == 1

    await builder.delete(other)
    gone = await storage.read("MATCH (n) WHERE n.graph_id=$g RETURN count(n) AS c", g=other)
    assert gone[0]["c"] == 0
    assert not builder.document_path(other).exists()

    intact = await storage.read(
        "MATCH (n) WHERE n.graph_id=$g RETURN count(n) AS c", g=graph_ns
    )
    assert intact[0]["c"] > 0, "deleting one graph must not touch another"


async def test_stats_on_an_unknown_graph_raises(builder):
    with pytest.raises(GraphNotFound):
        await builder.stats("does-not-exist")
