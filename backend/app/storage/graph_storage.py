"""Read access to a built graph: fetch, filter, paginate, traverse.

Every query is scoped to a ``graph_id``. One Neo4j instance holds many
documents, and a query that forgets the scope silently returns another
document's entities — which is the kind of bug that looks like a modelling
problem for a long time before anyone suspects the query.

Traversal is capped. A neighbourhood query on a well-connected node in a large
graph can reach most of it, and a visualisation asked to render that will
simply hang. Results carry a ``truncated`` flag rather than quietly returning a
prefix, so the caller can say so.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from app.config import Config, get_config
from app.storage.neo4j_storage import Neo4jStorage

logger = logging.getLogger(__name__)

__all__ = [
    "GraphStorage",
    "Page",
    "Subgraph",
    "document_path",
    "graph_dir",
    "ontology_path",
]

DEFAULT_DATA_DIR = Path("data/graphs")

# Depth beyond this is almost always a mistake: at branching factor 5 a depth-6
# neighbourhood is 15,000 nodes, and nobody reads that.
MAX_TRAVERSAL_DEPTH = 5
DEFAULT_NODE_LIMIT = 500
MAX_PAGE_SIZE = 500


def graph_dir(graph_id: str, data_dir: str | Path | None = None) -> Path:
    return (Path(data_dir) if data_dir else DEFAULT_DATA_DIR) / graph_id


def document_path(graph_id: str, data_dir: str | Path | None = None) -> Path:
    return graph_dir(graph_id, data_dir) / "document.txt"


def ontology_path(graph_id: str, data_dir: str | Path | None = None) -> Path:
    return graph_dir(graph_id, data_dir) / "ontology.json"


@dataclass
class Page:
    """One page of results, and enough context to request the next."""

    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


@dataclass
class Subgraph:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    def summary(self) -> str:
        suffix = " (truncated)" if self.truncated else ""
        return f"{len(self.nodes)} nodes, {len(self.edges)} edges{suffix}"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class GraphStorage:
    """Queries over a persisted graph."""

    def __init__(
        self,
        storage: Neo4jStorage,
        config: Config | None = None,
        *,
        data_dir: str | Path | None = None,
    ) -> None:
        self.config = config or get_config()
        self.storage = storage
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR

    # -- documents ----------------------------------------------------------

    async def list_graphs(self) -> list[dict[str, Any]]:
        return await self.storage.read(
            "MATCH (d:Document) "
            "OPTIONAL MATCH (e:Entity {graph_id: d.graph_id}) "
            "RETURN d.graph_id AS graph_id, d.filename AS filename, "
            "       d.domain AS domain, d.built_at AS built_at, "
            "       d.char_count AS char_count, count(e) AS entity_count "
            "ORDER BY d.built_at DESC"
        )

    async def get_graph(self, graph_id: str) -> dict[str, Any] | None:
        rows = await self.storage.read(
            "MATCH (d:Document {graph_id: $graph_id}) "
            "OPTIONAL MATCH (c:Chunk {graph_id: $graph_id}) "
            "OPTIONAL MATCH (e:Entity {graph_id: $graph_id}) "
            "RETURN d.graph_id AS graph_id, d.filename AS filename, "
            "       d.domain AS domain, d.built_at AS built_at, "
            "       d.char_count AS char_count, d.page_count AS page_count, "
            "       d.entity_types AS entity_types, "
            "       d.relationship_types AS relationship_types, "
            "       count(DISTINCT c) AS chunk_count, "
            "       count(DISTINCT e) AS entity_count",
            graph_id=graph_id,
        )
        return rows[0] if rows and rows[0].get("graph_id") else None

    def load_document(self, graph_id: str) -> str | None:
        path = document_path(graph_id, self.data_dir)
        return path.read_text(encoding="utf-8") if path.is_file() else None

    # -- entities -----------------------------------------------------------

    async def get_entity(self, graph_id: str, uuid: str) -> dict[str, Any] | None:
        rows = await self.storage.read(
            "MATCH (e:Entity {graph_id: $graph_id, uuid: $uuid}) "
            "RETURN e AS entity",
            graph_id=graph_id,
            uuid=uuid,
        )
        return _entity_row(rows[0]["entity"]) if rows else None

    async def list_entities(
        self,
        graph_id: str,
        *,
        types: Sequence[str] | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        limit = _clamp(limit, 1, MAX_PAGE_SIZE)
        offset = max(0, offset)
        filters = ["e.graph_id = $graph_id"]
        params: dict[str, Any] = {"graph_id": graph_id}
        if types:
            filters.append("e.type IN $types")
            params["types"] = list(types)
        if search:
            filters.append(
                "(toLower(e.name) CONTAINS $search OR e.normalised CONTAINS $search)"
            )
            params["search"] = search.lower()
        where = " AND ".join(filters)

        total = await self.storage.read(
            f"MATCH (e:Entity) WHERE {where} RETURN count(e) AS total",  # cypher-audit: ok - `where` is built from fixed clauses; all values are parameters
            **params,
        )
        rows = await self.storage.read(
            f"MATCH (e:Entity) WHERE {where} "  # cypher-audit: ok - `where` is built from fixed clauses; all values are parameters
            "RETURN e AS entity "
            "ORDER BY e.mention_count DESC, e.name ASC "
            "SKIP $offset LIMIT $limit",
            **params,
            offset=offset,
            limit=limit,
        )
        return Page(
            items=[_entity_row(row["entity"]) for row in rows],
            total=total[0]["total"] if total else 0,
            limit=limit,
            offset=offset,
        )

    async def entity_types(self, graph_id: str) -> list[dict[str, Any]]:
        return await self.storage.read(
            "MATCH (e:Entity {graph_id: $graph_id}) "
            "RETURN e.type AS type, count(e) AS count "
            "ORDER BY count DESC, type ASC",
            graph_id=graph_id,
        )

    # -- relationships ------------------------------------------------------

    async def list_relationships(
        self, graph_id: str, *, uuid: str | None = None, limit: int = 100, offset: int = 0
    ) -> Page:
        limit = _clamp(limit, 1, MAX_PAGE_SIZE)
        offset = max(0, offset)
        clause = (
            "MATCH (s:Entity {graph_id: $graph_id})-[r]->(t:Entity) "
            "WHERE r.graph_id = $graph_id"
        )
        params: dict[str, Any] = {"graph_id": graph_id}
        if uuid:
            clause += " AND (s.uuid = $uuid OR t.uuid = $uuid)"
            params["uuid"] = uuid

        total = await self.storage.read(
            f"{clause} RETURN count(r) AS total",  # cypher-audit: ok - clause is fixed text; values are parameters
            **params,
        )
        rows = await self.storage.read(
            f"{clause} "  # cypher-audit: ok - clause is fixed text; values are parameters
            "RETURN type(r) AS type, r.uuid AS uuid, "
            "       r.mention_count AS mention_count, "
            "       r.chunk_indices AS chunk_indices, "
            "       s.uuid AS source, s.name AS source_name, "
            "       t.uuid AS target, t.name AS target_name "
            "ORDER BY r.mention_count DESC, type ASC "
            "SKIP $offset LIMIT $limit",
            **params,
            offset=offset,
            limit=limit,
        )
        return Page(
            items=rows,
            total=total[0]["total"] if total else 0,
            limit=limit,
            offset=offset,
        )

    # -- traversal ----------------------------------------------------------

    async def neighbours(
        self,
        graph_id: str,
        uuid: str,
        *,
        depth: int = 1,
        limit: int = DEFAULT_NODE_LIMIT,
    ) -> Subgraph:
        """Entities within ``depth`` hops, and the edges between them.

        Undirected: a neighbourhood is about who is connected, not who
        initiated. Direction survives on each edge.

        Paths are constrained to ``:Entity`` nodes throughout, so traversal
        cannot hop through a ``:Chunk`` — two people mentioned in the same
        passage are not neighbours in the graph sense, and letting the walk
        route through provenance would make almost everything adjacent.
        """
        depth = _clamp(depth, 1, MAX_TRAVERSAL_DEPTH)
        limit = _clamp(limit, 1, MAX_PAGE_SIZE * 4)

        rows = await self.storage.read(
            "MATCH path = (start:Entity {graph_id: $graph_id, uuid: $uuid})"
            f"-[*1..{depth}]-(other:Entity) "  # cypher-audit: ok - depth is an int clamped to 1..MAX_TRAVERSAL_DEPTH
            "WHERE all(n IN nodes(path) WHERE n:Entity AND n.graph_id = $graph_id) "
            "UNWIND nodes(path) AS node "
            "WITH collect(DISTINCT node) AS nodes "
            "RETURN nodes[0..$limit] AS nodes, size(nodes) AS total",
            graph_id=graph_id,
            uuid=uuid,
            limit=limit,
        )
        if not rows or not rows[0]["nodes"]:
            entity = await self.get_entity(graph_id, uuid)
            return Subgraph(nodes=[entity] if entity else [])

        nodes = [_entity_row(node) for node in rows[0]["nodes"]]
        truncated = rows[0]["total"] > len(nodes)
        uuids = [node["uuid"] for node in nodes]
        edges = await self._edges_between(graph_id, uuids)
        return Subgraph(nodes=nodes, edges=edges, truncated=truncated)

    async def subgraph(
        self, graph_id: str, *, limit: int = DEFAULT_NODE_LIMIT
    ) -> Subgraph:
        """The whole graph, capped — what a visualisation actually renders."""
        limit = _clamp(limit, 1, MAX_PAGE_SIZE * 4)
        total = await self.storage.read(
            "MATCH (e:Entity {graph_id: $graph_id}) RETURN count(e) AS total",
            graph_id=graph_id,
        )
        rows = await self.storage.read(
            "MATCH (e:Entity {graph_id: $graph_id}) "
            "RETURN e AS entity "
            "ORDER BY e.mention_count DESC, e.name ASC LIMIT $limit",
            graph_id=graph_id,
            limit=limit,
        )
        nodes = [_entity_row(row["entity"]) for row in rows]
        edges = await self._edges_between(graph_id, [n["uuid"] for n in nodes])
        counted = total[0]["total"] if total else 0
        return Subgraph(nodes=nodes, edges=edges, truncated=counted > len(nodes))

    async def _edges_between(
        self, graph_id: str, uuids: Sequence[str]
    ) -> list[dict[str, Any]]:
        if not uuids:
            return []
        return await self.storage.read(
            "MATCH (s:Entity)-[r]->(t:Entity) "
            "WHERE r.graph_id = $graph_id AND s.uuid IN $uuids AND t.uuid IN $uuids "
            "RETURN type(r) AS type, r.uuid AS uuid, s.uuid AS source, "
            "       t.uuid AS target, r.mention_count AS mention_count",
            graph_id=graph_id,
            uuids=list(uuids),
        )

    # -- provenance ---------------------------------------------------------

    async def entity_chunks(self, graph_id: str, uuid: str) -> list[dict[str, Any]]:
        """Passages mentioning an entity, with text sliced from the document."""
        rows = await self.storage.read(
            "MATCH (e:Entity {graph_id: $graph_id, uuid: $uuid})"
            "-[m:MENTIONED_IN]->(c:Chunk) "
            "RETURN c.index AS chunk_index, c.start AS start, c.end AS end, "
            "       m.surface AS surface "
            "ORDER BY c.index, m.surface",
            graph_id=graph_id,
            uuid=uuid,
        )
        text = self.load_document(graph_id)
        if text is not None:
            for row in rows:
                row["text"] = text[row["start"] : row["end"]]
        return rows


def _entity_row(node: Any) -> dict[str, Any]:
    """Flatten a Neo4j node into a plain dict, unpacking attr_ properties."""
    data = dict(node)
    attributes = {
        key[len("attr_") :]: value
        for key, value in data.items()
        if key.startswith("attr_")
    }
    result = {
        "uuid": data.get("uuid"),
        "name": data.get("name"),
        "normalised": data.get("normalised"),
        "type": data.get("type"),
        "aliases": sorted(data.get("aliases") or []),
        "mention_count": data.get("mention_count", 0),
        "inferred": bool(data.get("inferred")),
        "attributes": attributes,
    }
    conflicts = data.get("attribute_conflicts")
    if conflicts:
        try:
            result["attribute_conflicts"] = json.loads(conflicts)
        except (TypeError, ValueError):  # pragma: no cover - stored by us
            result["attribute_conflicts"] = {}
    # The embedding is 768 floats; nothing that reads an entity wants it inline.
    return result
