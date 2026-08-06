"""Persist an extracted graph to Neo4j, with provenance back to the source.

The shape:

    (:Entity)-[:MENTIONED_IN {surface}]->(:Chunk)-[:PART_OF]->(:Document)
    (:Entity)-[:<ONTOLOGY_TYPE>]->(:Entity)

Every node carries ``graph_id``, so one Neo4j instance holds many documents and
deleting one is a single scan.

**Provenance is a traversal, not an array.** Phase 8 requires every claim in a
report to cite specific source text; making that a graph query means a citation
is checkable by the same mechanism that produced it, and "which entities
co-occur in a passage" becomes a query rather than a scan.

**Chunk nodes store offsets, not text.** Chunks are exact slices of the
normalised document, so offsets recover the text exactly. The document is
written once to ``data/graphs/<graph_id>/document.txt``; storing it again in
Neo4j would put a 50 MB document into the graph store and its page cache, and
overlap means it would store rather more than 50 MB.

**Rebuilding is idempotent** because identifiers are derived, not random. An
entity's UUID is a UUID5 of ``graph_id | type | normalised name``, so a second
ingestion of the same document MERGEs onto the same nodes instead of doubling
them. The extractor's random UUIDs are transient and remapped here.

Entities also carry their ontology type as a second label, so a Phase 9
visualisation can match ``(:Person)`` directly. That is the one place Cypher
genuinely cannot parameterise, and it goes through ``escape_identifier``.
"""

from __future__ import annotations

import json
import logging
import uuid as uuidlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.config import Config, get_config
from app.services.ontology_generator import Ontology
from app.storage.ner_extractor import Entity, ExtractionResult, Relationship
from app.storage.neo4j_schema import apply_schema
from app.storage.neo4j_storage import Neo4jStorage, escape_identifier
from app.utils.chunker import Chunk
from app.utils.file_parser import ParsedDocument

logger = logging.getLogger(__name__)

__all__ = ["BuildResult", "GraphBuilder", "GraphNotFound", "derive_uuid"]

# Stable namespace, so derived UUIDs are reproducible across processes and
# releases. Changing it orphans every previously built graph.
NAMESPACE = uuidlib.UUID("6f5c1a2e-2f3d-4c5b-9a7e-0d1b2c3d4e5f")

# Attributes become node properties under this prefix, so an extracted
# attribute called "name" or "type" cannot shadow a structural one.
ATTRIBUTE_PREFIX = "attr_"


class GraphNotFound(LookupError):
    """No graph with that identifier exists."""


def derive_uuid(*parts: str) -> str:
    """A deterministic identifier for a logical graph object."""
    return uuidlib.uuid5(NAMESPACE, "|".join(parts)).hex


@dataclass
class BuildResult:
    graph_id: str
    chunks: int = 0
    entities: int = 0
    relationships: int = 0
    mentions: int = 0
    document_path: Path | None = None
    ontology_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"graph {self.graph_id}: {self.entities} entities, "
            f"{self.relationships} relationships, {self.mentions} mentions "
            f"across {self.chunks} chunks"
        )


class GraphBuilder:
    """Writes an extraction result into Neo4j and the graph data directory."""

    def __init__(
        self,
        storage: Neo4jStorage,
        config: Config | None = None,
        *,
        data_dir: str | Path | None = None,
    ) -> None:
        self.config = config or get_config()
        self.storage = storage
        self.data_dir = Path(data_dir) if data_dir else Path("data/graphs")

    # -- paths --------------------------------------------------------------

    def graph_dir(self, graph_id: str) -> Path:
        return self.data_dir / graph_id

    def document_path(self, graph_id: str) -> Path:
        return self.graph_dir(graph_id) / "document.txt"

    def ontology_path(self, graph_id: str) -> Path:
        return self.graph_dir(graph_id) / "ontology.json"

    # -- build --------------------------------------------------------------

    async def build(
        self,
        *,
        graph_id: str,
        document: ParsedDocument,
        chunks: Sequence[Chunk],
        ontology: Ontology,
        extraction: ExtractionResult,
        replace: bool = False,
    ) -> BuildResult:
        """Persist a document's graph. Safe to run twice on the same input."""
        if not graph_id:
            raise ValueError("graph_id is required")

        result = BuildResult(graph_id=graph_id)
        await apply_schema(self.storage, dimensions=self.config.EMBEDDING_DIM)

        if replace:
            await self.delete(graph_id)

        result.document_path = self._write_document(graph_id, document)
        result.ontology_path = ontology.save(self.ontology_path(graph_id))

        await self._write_document_node(graph_id, document, ontology)
        result.chunks = await self._write_chunks(graph_id, chunks)

        remap = {
            entity.uuid: derive_uuid(graph_id, entity.type, entity.normalised)
            for entity in extraction.entities
        }
        result.entities = await self._write_entities(graph_id, extraction.entities, remap)
        result.mentions = await self._write_mentions(graph_id, extraction.entities, remap)
        result.relationships = await self._write_relationships(
            graph_id, extraction.relationships, remap
        )

        logger.info("Built %s", result.summary())
        return result

    def _write_document(self, graph_id: str, document: ParsedDocument) -> Path:
        path = self.document_path(graph_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document.text, encoding="utf-8")
        return path

    async def _write_document_node(
        self, graph_id: str, document: ParsedDocument, ontology: Ontology
    ) -> None:
        await self.storage.write(
            "MERGE (d:Document {graph_id: $graph_id}) SET d += $props",
            graph_id=graph_id,
            props={
                "filename": document.filename,
                "extension": document.extension,
                "char_count": document.char_count,
                "byte_size": document.byte_size,
                "page_count": document.page_count,
                "encoding": document.encoding,
                "domain": ontology.domain,
                "entity_types": sorted(e.name for e in ontology.entity_types),
                "relationship_types": sorted(r.name for r in ontology.relationship_types),
                "built_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def _write_chunks(self, graph_id: str, chunks: Sequence[Chunk]) -> int:
        rows = [
            {
                "uuid": derive_uuid(graph_id, "chunk", str(chunk.index)),
                "graph_id": graph_id,
                "index": chunk.index,
                "start": chunk.start,
                "end": chunk.end,
                "length": len(chunk),
            }
            for chunk in chunks
        ]
        await self.storage.run_batch(
            "UNWIND $rows AS row "
            "MERGE (c:Chunk {uuid: row.uuid}) "
            "SET c.graph_id = row.graph_id, c.index = row.index, "
            "    c.start = row.start, c.end = row.end, c.length = row.length "
            "WITH c, row "
            "MATCH (d:Document {graph_id: row.graph_id}) "
            "MERGE (c)-[:PART_OF]->(d)",
            rows,
        )
        return len(rows)

    async def _write_entities(
        self, graph_id: str, entities: Sequence[Entity], remap: dict[str, str]
    ) -> int:
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entity in entities:
            by_type[entity.type].append({
                "uuid": remap[entity.uuid],
                "graph_id": graph_id,
                "name": entity.name,
                "normalised": entity.normalised,
                "type": entity.type,
                "aliases": sorted(entity.aliases),
                "mention_count": entity.mention_count,
                "inferred": entity.inferred,
                "embedding": entity.embedding,
                "attribute_conflicts": (
                    json.dumps(entity.attribute_conflicts, sort_keys=True)
                    if entity.attribute_conflicts else None
                ),
                "attrs": _prefixed(entity.attributes),
            })

        written = 0
        for entity_type, rows in by_type.items():
            label = escape_identifier(entity_type)
            # The one thing Cypher cannot parameterise. The label comes from a
            # normalised ontology name and goes through escape_identifier,
            # which validates rather than escapes.
            statement = (
                "UNWIND $rows AS row "
                "MERGE (e:Entity {uuid: row.uuid}) "
                f"SET e:{label} "
                "SET e.graph_id = row.graph_id, e.name = row.name, "
                "    e.normalised = row.normalised, e.type = row.type, "
                "    e.aliases = row.aliases, e.mention_count = row.mention_count, "
                "    e.inferred = row.inferred, "
                "    e.attribute_conflicts = row.attribute_conflicts "
                "SET e += row.attrs "
                "FOREACH (_ IN CASE WHEN row.embedding IS NULL THEN [] ELSE [1] END | "
                "    SET e.embedding = row.embedding)"
            )  # cypher-audit: ok - label validated by escape_identifier; values are parameters
            await self.storage.run_batch(statement, rows)
            written += len(rows)
        return written

    async def _write_mentions(
        self, graph_id: str, entities: Sequence[Entity], remap: dict[str, str]
    ) -> int:
        rows = [
            {
                "entity_uuid": remap[entity.uuid],
                "chunk_uuid": derive_uuid(graph_id, "chunk", str(mention.chunk_index)),
                "surface": mention.surface,
                "chunk_index": mention.chunk_index,
            }
            for entity in entities
            for mention in entity.mentions
        ]
        if not rows:
            return 0
        await self.storage.run_batch(
            "UNWIND $rows AS row "
            "MATCH (e:Entity {uuid: row.entity_uuid}) "
            "MATCH (c:Chunk {uuid: row.chunk_uuid}) "
            "MERGE (e)-[m:MENTIONED_IN {surface: row.surface}]->(c) "
            "SET m.chunk_index = row.chunk_index",
            rows,
        )
        return len(rows)

    async def _write_relationships(
        self, graph_id: str, relationships: Sequence[Relationship], remap: dict[str, str]
    ) -> int:
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for relationship in relationships:
            source = remap.get(relationship.source_uuid)
            target = remap.get(relationship.target_uuid)
            if source is None or target is None:
                continue
            by_type[relationship.type].append({
                "uuid": derive_uuid(graph_id, relationship.type, source, target),
                "graph_id": graph_id,
                "source": source,
                "target": target,
                "mention_count": len(relationship.mentions),
                "chunk_indices": sorted({m.chunk_index for m in relationship.mentions}),
                "attrs": _prefixed(relationship.attributes),
            })

        written = 0
        for relationship_type, rows in by_type.items():
            edge = escape_identifier(relationship_type)
            statement = (
                "UNWIND $rows AS row "
                "MATCH (s:Entity {uuid: row.source}) "
                "MATCH (t:Entity {uuid: row.target}) "
                f"MERGE (s)-[r:{edge} {{uuid: row.uuid}}]->(t) "
                "SET r.graph_id = row.graph_id, "
                "    r.mention_count = row.mention_count, "
                "    r.chunk_indices = row.chunk_indices "
                "SET r += row.attrs"
            )  # cypher-audit: ok - type validated by escape_identifier; values are parameters
            await self.storage.run_batch(statement, rows)
            written += len(rows)
        return written

    # -- read back ----------------------------------------------------------

    async def provenance(self, graph_id: str, entity_uuid: str) -> list[dict[str, Any]]:
        """Every passage that mentions an entity, with its text.

        This is the property the spec asks for: any node traceable back to the
        text that produced it. Offsets come from the graph, text from the
        document on disk.
        """
        rows = await self.storage.read(
            "MATCH (e:Entity {uuid: $uuid})-[m:MENTIONED_IN]->(c:Chunk) "
            "RETURN c.index AS chunk_index, c.start AS start, c.end AS end, "
            "       m.surface AS surface "
            "ORDER BY c.index, m.surface",
            uuid=entity_uuid,
        )
        if not rows:
            return []
        text = self.load_document(graph_id)
        for row in rows:
            row["text"] = text[row["start"] : row["end"]]
        return rows

    def load_document(self, graph_id: str) -> str:
        path = self.document_path(graph_id)
        if not path.is_file():
            raise GraphNotFound(
                f"No document stored for graph {graph_id!r} at {path}. "
                f"Provenance needs it: chunks hold offsets, not text."
            )
        return path.read_text(encoding="utf-8")

    async def stats(self, graph_id: str) -> dict[str, Any]:
        rows = await self.storage.read(
            "MATCH (d:Document {graph_id: $graph_id}) "
            "OPTIONAL MATCH (c:Chunk {graph_id: $graph_id}) "
            "OPTIONAL MATCH (e:Entity {graph_id: $graph_id}) "
            "RETURN d.filename AS filename, d.domain AS domain, "
            "       count(DISTINCT c) AS chunks, count(DISTINCT e) AS entities",
            graph_id=graph_id,
        )
        if not rows or rows[0]["filename"] is None:
            raise GraphNotFound(f"No graph {graph_id!r}")
        stats = rows[0]
        edges = await self.storage.read(
            "MATCH (:Entity {graph_id: $graph_id})-[r]->(:Entity {graph_id: $graph_id}) "
            "WHERE r.graph_id = $graph_id RETURN count(r) AS relationships",
            graph_id=graph_id,
        )
        stats["relationships"] = edges[0]["relationships"] if edges else 0
        return stats

    async def delete(self, graph_id: str) -> int:
        """Remove a graph's nodes and its stored document."""
        rows = await self.storage.write(
            "MATCH (n) WHERE n.graph_id = $graph_id "
            "WITH n, count(n) AS ignored DETACH DELETE n "
            "RETURN count(ignored) AS deleted",
            graph_id=graph_id,
        )
        directory = self.graph_dir(graph_id)
        if directory.is_dir():
            for path in sorted(directory.iterdir()):
                path.unlink()
            directory.rmdir()
        return rows[0]["deleted"] if rows else 0


def _prefixed(attributes: Iterable[tuple[str, str]] | dict[str, str]) -> dict[str, str]:
    items = attributes.items() if isinstance(attributes, dict) else attributes
    return {f"{ATTRIBUTE_PREFIX}{key}": value for key, value in items}
