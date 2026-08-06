"""Search a built graph, over entities and over source passages.

**Search is hybrid, and that follows from measurement rather than taste.**
Phase 3 Step 4 measured `nomic-embed-text` on short entity names and found the
distributions unusable: `Eastgate` and `Eastgate corridor` — different things —
score 0.856, higher than genuine aliases at 0.74. A pure vector search over
entity names inherits exactly that weakness, so typing a name you can see in
the graph might not return it. That is the commonest search there is.

So two arms run and their results merge:

* **Lexical** — exact, prefix and substring matching over name, normalised form
  and aliases. Deterministic, and the only arm that reliably answers "find the
  entity called X".
* **Vector** — cosine similarity over stored embeddings, for the questions
  lexical matching cannot answer at all: "who objected to the timetable".

Lexical hits rank first because when they exist they are almost always what was
wanted. Every hit records which arm found it, so a ranking can be explained
rather than just presented.

Passage search exists because Phase 8 must ground report claims in source text.
A claim about a theme has no entity to start from, so traversing from entities
would not reach it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from app.config import Config, get_config
from app.storage.embedding_service import EmbeddingService
from app.storage.graph_storage import GraphStorage
from app.storage.neo4j_schema import (
    CHUNK_VECTOR_INDEX_NAME,
    VECTOR_INDEX_NAME,
    cosine_similarity,
    supports_vector_index,
)
from app.storage.neo4j_storage import Neo4jStorage

logger = logging.getLogger(__name__)

__all__ = ["ChunkHit", "SearchHit", "SearchService"]

MatchedBy = Literal["exact", "prefix", "substring", "alias", "vector"]

# Lexical confidence, by how the string matched. These are not similarities and
# are not comparable with cosine scores; they exist to order the lexical arm
# among itself, and lexical always precedes vector.
LEXICAL_SCORES: dict[str, float] = {
    "exact": 1.0,
    "prefix": 0.95,
    "alias": 0.9,
    "substring": 0.85,
}


@dataclass
class SearchHit:
    uuid: str
    name: str
    type: str
    score: float
    matched_by: MatchedBy
    mention_count: int = 0
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "type": self.type,
            "score": round(self.score, 4),
            "matched_by": self.matched_by,
            "mention_count": self.mention_count,
            "aliases": self.aliases,
        }


@dataclass
class ChunkHit:
    chunk_index: int
    start: int
    end: int
    score: float
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_index": self.chunk_index,
            "start": self.start,
            "end": self.end,
            "score": round(self.score, 4),
            "text": self.text,
        }


class SearchService:
    """Hybrid search over entities, and semantic search over passages."""

    def __init__(
        self,
        storage: Neo4jStorage,
        config: Config | None = None,
        *,
        embeddings: EmbeddingService | None = None,
        graphs: GraphStorage | None = None,
    ) -> None:
        self.config = config or get_config()
        self.storage = storage
        self.embeddings = embeddings
        self.graphs = graphs or GraphStorage(storage, self.config)

    # -- entities -----------------------------------------------------------

    async def search_entities(
        self,
        graph_id: str,
        query: str,
        *,
        limit: int = 10,
        types: Sequence[str] | None = None,
        semantic: bool = True,
    ) -> list[SearchHit]:
        query = (query or "").strip()
        if not query:
            return []

        hits: list[SearchHit] = []
        seen: set[str] = set()

        for hit in await self._lexical_entities(graph_id, query, types, limit):
            if hit.uuid not in seen:
                seen.add(hit.uuid)
                hits.append(hit)

        if semantic and self.embeddings is not None and len(hits) < limit:
            for hit in await self._vector_entities(graph_id, query, types, limit * 3):
                if hit.uuid in seen:
                    continue
                seen.add(hit.uuid)
                hits.append(hit)
                if len(hits) >= limit:
                    break

        return hits[:limit]

    async def _lexical_entities(
        self, graph_id: str, query: str, types: Sequence[str] | None, limit: int
    ) -> list[SearchHit]:
        needle = query.lower()
        params: dict[str, Any] = {"graph_id": graph_id, "needle": needle, "limit": limit}
        type_filter = ""
        if types:
            type_filter = " AND e.type IN $types"
            params["types"] = list(types)

        rows = await self.storage.read(
            "MATCH (e:Entity {graph_id: $graph_id}) "
            "WHERE (toLower(e.name) CONTAINS $needle "
            "       OR e.normalised CONTAINS $needle "
            "       OR any(a IN e.aliases WHERE toLower(a) CONTAINS $needle))"
            f"{type_filter} "  # cypher-audit: ok - fixed clause chosen by a flag; the value is a parameter
            "RETURN e.uuid AS uuid, e.name AS name, e.type AS type, "
            "       e.normalised AS normalised, e.aliases AS aliases, "
            "       e.mention_count AS mention_count "
            "ORDER BY e.mention_count DESC, e.name ASC LIMIT $limit",
            **params,
        )

        hits = [
            SearchHit(
                uuid=row["uuid"],
                name=row["name"],
                type=row["type"],
                score=LEXICAL_SCORES[_match_kind(needle, row)],
                matched_by=_match_kind(needle, row),  # type: ignore[arg-type]
                mention_count=row.get("mention_count") or 0,
                aliases=sorted(row.get("aliases") or []),
            )
            for row in rows
        ]
        hits.sort(key=lambda h: (-h.score, -h.mention_count, h.name))
        return hits

    async def _vector_entities(
        self, graph_id: str, query: str, types: Sequence[str] | None, fetch: int
    ) -> list[SearchHit]:
        assert self.embeddings is not None
        vector = await self.embeddings.embed(query)

        if await supports_vector_index(self.storage, VECTOR_INDEX_NAME):
            rows = await self.storage.read(
                "CALL db.index.vector.queryNodes($index, $fetch, $vector) "
                "YIELD node, score "
                "WITH node, score WHERE node.graph_id = $graph_id "
                "RETURN node.uuid AS uuid, node.name AS name, node.type AS type, "
                "       node.aliases AS aliases, "
                "       node.mention_count AS mention_count, score",
                index=VECTOR_INDEX_NAME,
                # The index cannot be scoped to a graph, so over-fetch and
                # filter: asking for exactly `fetch` returns too few.
                fetch=max(fetch * 5, 50),
                vector=vector,
                graph_id=graph_id,
            )
        else:
            rows = await self._in_process_entity_scores(graph_id, vector, fetch)

        hits = [
            SearchHit(
                uuid=row["uuid"],
                name=row["name"],
                type=row["type"],
                score=float(row["score"]),
                matched_by="vector",
                mention_count=row.get("mention_count") or 0,
                aliases=sorted(row.get("aliases") or []),
            )
            for row in rows
            if not types or row["type"] in set(types)
        ]
        hits.sort(key=lambda h: -h.score)
        return hits[:fetch]

    async def _in_process_entity_scores(
        self, graph_id: str, vector: Sequence[float], fetch: int
    ) -> list[dict[str, Any]]:
        rows = await self.storage.read(
            "MATCH (e:Entity {graph_id: $graph_id}) WHERE e.embedding IS NOT NULL "
            "RETURN e.uuid AS uuid, e.name AS name, e.type AS type, "
            "       e.aliases AS aliases, e.mention_count AS mention_count, "
            "       e.embedding AS embedding",
            graph_id=graph_id,
        )
        for row in rows:
            row["score"] = cosine_similarity(vector, row.pop("embedding"))
        rows.sort(key=lambda r: -r["score"])
        return rows[:fetch]

    # -- passages -----------------------------------------------------------

    async def search_chunks(
        self, graph_id: str, query: str, *, limit: int = 5
    ) -> list[ChunkHit]:
        """Passages most similar to a query, with their text.

        This is what Phase 8 cites. Text is sliced from the stored document
        using the chunk's offsets rather than read from the graph.
        """
        query = (query or "").strip()
        if not query or self.embeddings is None:
            return []
        vector = await self.embeddings.embed(query)

        if await supports_vector_index(self.storage, CHUNK_VECTOR_INDEX_NAME):
            rows = await self.storage.read(
                "CALL db.index.vector.queryNodes($index, $fetch, $vector) "
                "YIELD node, score "
                "WITH node, score WHERE node.graph_id = $graph_id "
                "RETURN node.index AS chunk_index, node.start AS start, "
                "       node.end AS end, score "
                "ORDER BY score DESC LIMIT $limit",
                index=CHUNK_VECTOR_INDEX_NAME,
                fetch=max(limit * 10, 50),
                vector=vector,
                graph_id=graph_id,
                limit=limit,
            )
        else:
            candidates = await self.storage.read(
                "MATCH (c:Chunk {graph_id: $graph_id}) WHERE c.embedding IS NOT NULL "
                "RETURN c.index AS chunk_index, c.start AS start, c.end AS end, "
                "       c.embedding AS embedding",
                graph_id=graph_id,
            )
            for row in candidates:
                row["score"] = cosine_similarity(vector, row.pop("embedding"))
            candidates.sort(key=lambda r: -r["score"])
            rows = candidates[:limit]

        text = self.graphs.load_document(graph_id)
        return [
            ChunkHit(
                chunk_index=row["chunk_index"],
                start=row["start"],
                end=row["end"],
                score=float(row["score"]),
                text=text[row["start"] : row["end"]] if text else "",
            )
            for row in rows
        ]


def _match_kind(needle: str, row: dict[str, Any]) -> str:
    """How this row matched, from most to least specific."""
    name = (row.get("name") or "").lower()
    normalised = row.get("normalised") or ""
    if name == needle or normalised == needle:
        return "exact"
    if name.startswith(needle) or normalised.startswith(needle):
        return "prefix"
    if needle in name or needle in normalised:
        return "substring"
    return "alias"
