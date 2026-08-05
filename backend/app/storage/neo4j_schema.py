"""Graph schema: constraints, indexes, and the vector-search capability.

Applying the schema is idempotent. Every statement uses ``IF NOT EXISTS``, so
it can run on every startup — which it should, because a graph missing its
uniqueness constraint will happily accept the duplicate entities that Phase 3's
deduplication exists to prevent, and nothing will complain until the results
are wrong.

**Vector index.** Neo4j gained native vector indexes in 5.11. When the server
supports one we create it and let the database do nearest-neighbour search;
when it does not, cosine similarity is computed in-process instead. The
fallback is genuinely slower — it reads candidate embeddings back to the
client — but it keeps the system working on an older server rather than
failing at the point of use, several phases later.

Capability is established by *trying*, not by parsing a version string. A
version check encodes an assumption about which builds have the feature;
attempting the creation and reading back ``SHOW INDEXES`` establishes the fact.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.storage.neo4j_storage import Neo4jStorage, StorageError

logger = logging.getLogger(__name__)

__all__ = [
    "ENTITY_LABEL",
    "SchemaReport",
    "apply_schema",
    "cosine_similarity",
    "drop_schema",
    "similarity_search",
    "supports_vector_index",
]

ENTITY_LABEL = "Entity"
VECTOR_INDEX_NAME = "entity_embedding_vec"

# Uniqueness first: it is the one that protects correctness rather than speed.
CONSTRAINTS: tuple[tuple[str, str], ...] = (
    (
        "entity_uuid_unique",
        "CREATE CONSTRAINT entity_uuid_unique IF NOT EXISTS "
        "FOR (e:Entity) REQUIRE e.uuid IS UNIQUE",
    ),
)

INDEXES: tuple[tuple[str, str], ...] = (
    # Filtering by type is the commonest graph query in Phase 3 Step 6.
    ("entity_type", "CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)"),
    # Name lookup drives deduplication during extraction.
    ("entity_name", "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)"),
    # Every query is scoped to one graph; without this they all scan globally.
    (
        "entity_graph_id",
        "CREATE INDEX entity_graph_id IF NOT EXISTS FOR (e:Entity) ON (e.graph_id)",
    ),
    (
        "entity_graph_type",
        "CREATE INDEX entity_graph_type IF NOT EXISTS "
        "FOR (e:Entity) ON (e.graph_id, e.type)",
    ),
)


@dataclass
class SchemaReport:
    """What applying the schema actually did."""

    constraints: list[str] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)
    vector_index: str | None = None
    vector_search_available: bool = False
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mode = "native vector index" if self.vector_search_available else "in-process cosine"
        return (
            f"{len(self.constraints)} constraint(s), {len(self.indexes)} index(es), "
            f"similarity search via {mode}"
        )


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


async def apply_schema(
    storage: Neo4jStorage, *, dimensions: int | None = None
) -> SchemaReport:
    """Create constraints and indexes. Safe to run repeatedly."""
    dimensions = dimensions or storage.config.EMBEDDING_DIM
    report = SchemaReport()

    for name, statement in CONSTRAINTS:
        await storage.write(statement)
        report.constraints.append(name)

    for name, statement in INDEXES:
        await storage.write(statement)
        report.indexes.append(name)

    created = await _create_vector_index(storage, dimensions)
    if created:
        report.vector_index = VECTOR_INDEX_NAME
        report.vector_search_available = True
    else:
        report.notes.append(
            "Server does not support native vector indexes; similarity search "
            "will compute cosine distance in-process, which is slower on large "
            "graphs."
        )

    logger.info("Neo4j schema applied: %s", report.summary())
    return report


async def _create_vector_index(storage: Neo4jStorage, dimensions: int) -> bool:
    statement = (
        f"CREATE VECTOR INDEX {VECTOR_INDEX_NAME} IF NOT EXISTS "
        f"FOR (e:{ENTITY_LABEL}) ON (e.embedding) "
        f"OPTIONS {{ indexConfig: {{ "
        f"`vector.dimensions`: $dimensions, "
        f"`vector.similarity_function`: 'cosine' }} }}"
    )  # cypher-audit: ok - index name and label are module constants, not input
    try:
        await storage.write(statement, dimensions=dimensions)
    except StorageError as exc:
        logger.warning("Native vector index unavailable (%s)", exc)
        return False
    return await supports_vector_index(storage)


async def supports_vector_index(storage: Neo4jStorage) -> bool:
    """True when the vector index exists and is usable.

    Established by reading it back rather than by inspecting a version string:
    the question that matters is whether the index is there, and only the
    server can answer it.
    """
    try:
        rows = await storage.read(
            "SHOW INDEXES YIELD name, type WHERE name = $name RETURN name, type",
            name=VECTOR_INDEX_NAME,
        )
    except StorageError:
        return False
    return any(str(row.get("type", "")).upper() == "VECTOR" for row in rows)


async def drop_schema(storage: Neo4jStorage) -> None:
    """Remove everything this module creates. For tests and re-provisioning."""
    for name in (VECTOR_INDEX_NAME,):
        try:
            await storage.write(f"DROP INDEX {name} IF EXISTS")  # cypher-audit: ok - module constant
        except StorageError:  # pragma: no cover - already absent
            pass
    for name, _ in INDEXES:
        try:
            await storage.write(f"DROP INDEX {name} IF EXISTS")  # cypher-audit: ok - module constant
        except StorageError:  # pragma: no cover
            pass
    for name, _ in CONSTRAINTS:
        try:
            await storage.write(f"DROP CONSTRAINT {name} IF EXISTS")  # cypher-audit: ok - module constant
        except StorageError:  # pragma: no cover
            pass


# --------------------------------------------------------------------------
# Similarity search
# --------------------------------------------------------------------------


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, for the no-vector-index path.

    Returns 0.0 for a zero-magnitude vector rather than dividing by zero: an
    all-zero embedding means the model failed, and propagating a NaN through a
    ranking turns that into a much stranger bug much later.
    """
    if len(left) != len(right):
        raise ValueError(
            f"Cannot compare vectors of different dimensionality: "
            f"{len(left)} vs {len(right)}"
        )
    dot = 0.0
    left_sq = 0.0
    right_sq = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_sq += a * a
        right_sq += b * b
    if left_sq == 0.0 or right_sq == 0.0:
        return 0.0
    return dot / (math.sqrt(left_sq) * math.sqrt(right_sq))


async def similarity_search(
    storage: Neo4jStorage,
    embedding: Sequence[float],
    *,
    graph_id: str | None = None,
    limit: int = 10,
    use_index: bool | None = None,
) -> list[dict[str, Any]]:
    """Nearest entities by embedding, native index if available.

    Returns rows of ``{uuid, name, type, score}`` ordered by descending score,
    identically in both modes so callers never branch on which one ran.
    """
    if use_index is None:
        use_index = await supports_vector_index(storage)

    if use_index and graph_id is None:
        rows = await storage.read(
            "CALL db.index.vector.queryNodes($index, $limit, $embedding) "
            "YIELD node, score "
            "RETURN node.uuid AS uuid, node.name AS name, node.type AS type, score",
            index=VECTOR_INDEX_NAME,
            limit=limit,
            embedding=list(embedding),
        )
        return rows

    if use_index:
        # Over-fetch, then filter: the index cannot be scoped to a graph, so
        # asking for exactly `limit` would return too few after filtering.
        rows = await storage.read(
            "CALL db.index.vector.queryNodes($index, $fetch, $embedding) "
            "YIELD node, score "
            "WITH node, score WHERE node.graph_id = $graph_id "
            "RETURN node.uuid AS uuid, node.name AS name, node.type AS type, score "
            "LIMIT $limit",
            index=VECTOR_INDEX_NAME,
            fetch=max(limit * 10, 100),
            embedding=list(embedding),
            graph_id=graph_id,
            limit=limit,
        )
        return rows

    return await _in_process_similarity(storage, embedding, graph_id, limit)


async def _in_process_similarity(
    storage: Neo4jStorage,
    embedding: Sequence[float],
    graph_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Fallback: read candidate embeddings back and rank them here."""
    if graph_id is None:
        rows = await storage.read(
            "MATCH (e:Entity) WHERE e.embedding IS NOT NULL "
            "RETURN e.uuid AS uuid, e.name AS name, e.type AS type, "
            "e.embedding AS embedding"
        )
    else:
        rows = await storage.read(
            "MATCH (e:Entity) WHERE e.embedding IS NOT NULL AND e.graph_id = $graph_id "
            "RETURN e.uuid AS uuid, e.name AS name, e.type AS type, "
            "e.embedding AS embedding",
            graph_id=graph_id,
        )

    scored = []
    for row in rows:
        vector = row.pop("embedding", None)
        if not vector:
            continue
        row["score"] = cosine_similarity(embedding, vector)
        scored.append(row)
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:limit]
