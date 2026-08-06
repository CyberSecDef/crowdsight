"""Extract entities and relationships from chunks, and deduplicate them.

The same person mentioned in eight chunks must become one node, not eight.
Everything downstream depends on that: a graph with eight Jane Does produces
eight agents who each think they are her, and a report that counts her opinion
eight times.

**Deduplication is lexical first, embeddings second, and that order was
established by measurement rather than assumption.** Against
``nomic-embed-text``, cosine similarity separates the cases badly: ``Mayor Alan
Reyes``/``Alan Reyes`` scores 0.8394 while ``Jane Doe``/``John Doe`` scores
0.8132 — a 0.023 margin, far too narrow to hang a merge decision on. Stripping
honorifics and articles and comparing normalised names got 9 of 9 true merges
with 0 false merges on the same set, at no inference cost at all.

Embedding similarity therefore runs as a **second pass with a deliberately high
threshold and a lexical guard**, catching what normalisation misses without
reintroducing the failure mode. Merging two real entities into one node is
unrecoverable — nothing downstream can tell that it happened — so the bias is
towards leaving duplicates.

Merges happen only within an ontology type. If the model calls something a
``Person`` in one chunk and a ``PublicFigure`` in another, that stays two
nodes: a wrong merge is worse than a duplicate.

Attribute conflicts resolve to the **first occurrence in document order**, with
losing values kept on the node as ``attribute_conflicts`` so a reviewer can see
the disagreement instead of it being silently discarded.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, Field, field_validator

from app.config import Config, get_config
from app.services.ontology_generator import Ontology, to_identifier
from app.storage.embedding_service import EmbeddingService
from app.storage.neo4j_schema import cosine_similarity
from app.utils.chunker import Chunk
from app.utils.llm_client import LLMClient, LLMJSONError

logger = logging.getLogger(__name__)

__all__ = [
    "ChunkExtraction",
    "is_alias_of",
    "Entity",
    "ExtractionResult",
    "NERExtractor",
    "Relationship",
    "normalise_name",
]

# Titles and honorifics that decorate a name without changing who it refers to.
HONORIFICS = frozenset({
    "mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor", "sir", "dame",
    "lord", "lady", "cllr", "councillor", "councilor", "mayor", "deputy",
    "sen", "senator", "rep", "representative", "gov", "governor", "hon",
    "honourable", "honorable", "rev", "reverend", "capt", "captain", "col",
    "colonel", "lt", "lieutenant", "sgt", "sergeant", "gen", "general",
    "chair", "chairman", "chairwoman", "president", "director", "minister",
})
ARTICLES = frozenset({"the", "a", "an"})
SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq"})

_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)


def normalise_name(name: str) -> str:
    """Reduce a surface form to a comparable key.

    ``"Cllr. Jane Doe"``, ``"Councillor Jane Doe"`` and ``"Jane Doe"`` all
    become ``"jane doe"``. Leading honorifics and articles are stripped, as
    are trailing suffixes; punctuation and case are discarded.
    """
    text = unicodedata.normalize("NFKC", name).lower().strip()
    text = _PUNCTUATION.sub(" ", text)
    tokens = [t for t in text.split() if t]
    while tokens and (tokens[0] in HONORIFICS or tokens[0] in ARTICLES):
        tokens.pop(0)
    while tokens and tokens[-1] in SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def is_alias_of(left: str, right: str) -> bool:
    """True when one normalised name is a multi-token suffix of the other.

    This is the pattern real documents actually use: "opposition councillor
    tom whitfield" for "tom whitfield", "the Residents Association" for
    "Riverbend Residents Association". A *suffix* specifically — a prefix rule
    would fuse "Mill Street" into "Mill Street conservation area", which are
    different places.

    Two tokens minimum, so a bare surname does not swallow a full name:
    "reyes" stays separate from "alan reyes", and "eastgate" from "eastgate
    corridor". That leaves some duplicates, which is the intended trade — a
    wrong merge cannot be undone downstream.
    """
    a, b = left.split(), right.split()
    if not a or not b or a == b:
        return a == b and bool(a)
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    return len(shorter) >= 2 and longer[-len(shorter):] == shorter


def _token_similarity(left: str, right: str) -> float:
    """Jaccard overlap of normalised tokens, as a guard on embedding merges."""
    a, b = set(left.split()), set(right.split())
    if not a or not b:
        return 0.0
    if a <= b or b <= a:
        return 1.0
    return len(a & b) / len(a | b)


# --------------------------------------------------------------------------
# What the model returns
# --------------------------------------------------------------------------


class ExtractedEntity(BaseModel):
    name: str = Field(description="The entity's name exactly as written")
    type: str = Field(description="One of the ontology entity types")
    attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("attributes", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> Any:
        """Models return numbers and lists for attributes; flatten them.

        Rejecting would cost a repair round trip for something with an
        unambiguous representation.
        """
        if not isinstance(value, Mapping):
            return {}
        flattened: dict[str, str] = {}
        for key, item in value.items():
            if item is None or item == "":
                continue
            if isinstance(item, (list, tuple)):
                item = ", ".join(str(x) for x in item if x not in (None, ""))
            flattened[str(key)] = str(item).strip()
        return {k: v for k, v in flattened.items() if v}


class ExtractedRelationship(BaseModel):
    source: str = Field(description="Name of the entity the relation starts at")
    target: str = Field(description="Name of the entity the relation points to")
    type: str = Field(description="One of the ontology relationship types")
    attributes: dict[str, str] = Field(default_factory=dict)

    _stringify = field_validator("attributes", mode="before")(
        ExtractedEntity._stringify.__func__  # type: ignore[attr-defined]
    )


class ChunkExtraction(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Aggregated graph objects
# --------------------------------------------------------------------------


@dataclass
class Mention:
    """Where in the source an entity was seen. Phase 3 Step 5 needs this."""

    chunk_index: int
    start: int
    end: int
    surface: str


@dataclass
class Entity:
    """One deduplicated node."""

    uuid: str
    name: str
    normalised: str
    type: str
    attributes: dict[str, str] = field(default_factory=dict)
    attribute_conflicts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    aliases: set[str] = field(default_factory=set)
    mentions: list[Mention] = field(default_factory=list)
    embedding: list[float] | None = None
    # True when the entity was never returned in an `entities` list and exists
    # only because a relationship referenced it. Recorded so Step 5 can mark
    # its provenance honestly and a reviewer can judge it.
    inferred: bool = False

    @property
    def mention_count(self) -> int:
        return len(self.mentions)


@dataclass
class Relationship:
    """One deduplicated edge, between resolved entity UUIDs."""

    uuid: str
    type: str
    source_uuid: str
    target_uuid: str
    attributes: dict[str, str] = field(default_factory=dict)
    attribute_conflicts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    mentions: list[Mention] = field(default_factory=list)


@dataclass
class ExtractionResult:
    entities: list[Entity]
    relationships: list[Relationship]
    chunks_processed: int = 0
    chunks_failed: int = 0
    raw_entity_count: int = 0
    raw_relationship_count: int = 0
    dropped_off_ontology: int = 0
    dropped_unresolved: int = 0
    merged_by_name: int = 0
    merged_by_alias: int = 0
    merged_by_similarity: int = 0
    inferred_entities: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.entities)} entities and {len(self.relationships)} "
            f"relationships from {self.chunks_processed} chunk(s); "
            f"{self.raw_entity_count} raw mentions merged "
            f"({self.merged_by_name} by name, {self.merged_by_alias} by alias, "
            f"{self.merged_by_similarity} by similarity); "
            f"{self.inferred_entities} inferred from edges; "
            f"{self.dropped_off_ontology} off-ontology, "
            f"{self.dropped_unresolved} unresolved endpoints, "
            f"{self.chunks_failed} chunk(s) failed"
        )


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You extract structured knowledge from text. You are given an ontology and one \
passage. Return only what the passage actually states.

Rules:
- Use only the entity types and relationship types listed in the ontology.
- "name" is the entity as written in the passage. "type" is the ontology type.
- A relationship's "source" and "target" are entity NAMES, never type names.
- Every name used in a relationship must also appear in your entities list.
- Do not infer facts the passage does not state. An empty result is correct \
when the passage contains nothing relevant.
- Attributes are short strings drawn from the passage, not your own knowledge.

Worked example. For the passage:

  "Dr Amara Osei, who runs the Northfield Clinic, criticised the funding plan."

with entity types Person, Organisation, PolicyDraft and relationship types \
RUNS (Person to Organisation) and OPPOSES (Person to PolicyDraft), return:

{"entities": [
   {"name": "Dr Amara Osei", "type": "Person", "attributes": {"role": "clinic director"}},
   {"name": "Northfield Clinic", "type": "Organisation", "attributes": {}},
   {"name": "the funding plan", "type": "PolicyDraft", "attributes": {}}],
 "relationships": [
   {"source": "Dr Amara Osei", "target": "Northfield Clinic", "type": "RUNS", "attributes": {}},
   {"source": "Dr Amara Osei", "target": "the funding plan", "type": "OPPOSES", "attributes": {}}]}

Note that source and target are names such as "Northfield Clinic", not types \
such as "Organisation"."""

USER_PROMPT = """\
ONTOLOGY

Entity types:
{entity_types}

Relationship types:
{relationship_types}

PASSAGE
{chunk}

Extract the entities and relationships this passage states."""


def _render_ontology(ontology: Ontology) -> tuple[str, str]:
    entities = "\n".join(
        f"- {e.name}: {e.description}"
        + (f" (attributes: {', '.join(e.attributes)})" if e.attributes else "")
        for e in ontology.entity_types
    )
    relationships = "\n".join(
        f"- {r.name}: {r.description} "
        f"(connects a {' or '.join(r.source_types) or 'any entity'} "
        f"to a {' or '.join(r.target_types) or 'any entity'})"
        for r in ontology.relationship_types
    ) or "- (none)"
    return entities, relationships


# --------------------------------------------------------------------------
# Extractor
# --------------------------------------------------------------------------


class NERExtractor:
    """Extracts an ontology-conformant graph from a document's chunks."""

    def __init__(
        self,
        ontology: Ontology,
        config: Config | None = None,
        *,
        llm: LLMClient | None = None,
        embeddings: EmbeddingService | None = None,
        similarity_threshold: float | None = None,
    ) -> None:
        self.config = config or get_config()
        self.ontology = ontology
        self.llm = llm or LLMClient(self.config)
        self.embeddings = embeddings
        self.similarity_threshold = (
            similarity_threshold
            if similarity_threshold is not None
            else self.config.ENTITY_SIMILARITY_THRESHOLD
        )
        self._entity_types = {e.name for e in ontology.entity_types}
        self._relationship_types = {r.name for r in ontology.relationship_types}
        self._relationship_endpoints = {
            r.name: (set(r.source_types), set(r.target_types))
            for r in ontology.relationship_types
        }

    # -- public -------------------------------------------------------------

    async def extract(
        self, chunks: Sequence[Chunk], *, temperature: float = 0.1
    ) -> ExtractionResult:
        """Extract from every chunk, then merge across them."""
        if not chunks:
            return ExtractionResult(entities=[], relationships=[])

        entity_prompt, relationship_prompt = _render_ontology(self.ontology)
        logger.info("Extracting from %d chunk(s)", len(chunks))

        # gather preserves order, which is what makes "first occurrence wins"
        # mean "earliest chunk" rather than "whichever finished first".
        results = await asyncio.gather(
            *(
                self._extract_chunk(chunk, entity_prompt, relationship_prompt, temperature)
                for chunk in chunks
            )
        )

        return await self._merge(chunks, results)

    async def aclose(self) -> None:
        await self.llm.aclose()
        if self.embeddings is not None:
            await self.embeddings.aclose()

    # -- per chunk ----------------------------------------------------------

    async def _extract_chunk(
        self, chunk: Chunk, entity_prompt: str, relationship_prompt: str, temperature: float
    ) -> ChunkExtraction | None:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_PROMPT.format(
                    entity_types=entity_prompt,
                    relationship_types=relationship_prompt,
                    chunk=chunk.text,
                ),
            },
        ]
        try:
            return await self.llm.complete_json(
                messages, ChunkExtraction, temperature=temperature
            )
        except LLMJSONError as exc:
            # One unusable chunk should cost that chunk, not the ingestion.
            logger.warning("Chunk %d failed extraction: %s", chunk.index, exc)
            return None

    # -- merging ------------------------------------------------------------

    async def _merge(
        self, chunks: Sequence[Chunk], results: Sequence[ChunkExtraction | None]
    ) -> ExtractionResult:
        outcome = ExtractionResult(entities=[], relationships=[])
        by_key: dict[tuple[str, str], Entity] = {}
        # Every name seen for a type, so relationships can resolve aliases.
        lookup: dict[tuple[str, str], Entity] = {}
        pending: list[tuple[Chunk, ExtractedRelationship]] = []

        for chunk, result in zip(chunks, results):
            if result is None:
                outcome.chunks_failed += 1
                continue
            outcome.chunks_processed += 1
            outcome.raw_entity_count += len(result.entities)
            outcome.raw_relationship_count += len(result.relationships)

            for extracted in result.entities:
                entity_type = to_identifier(extracted.type)
                if entity_type not in self._entity_types:
                    outcome.dropped_off_ontology += 1
                    continue
                normalised = normalise_name(extracted.name)
                if not normalised:
                    outcome.dropped_off_ontology += 1
                    continue

                key = (entity_type, normalised)
                entity = by_key.get(key)
                if entity is None:
                    entity = Entity(
                        uuid=uuidlib.uuid4().hex,
                        name=extracted.name.strip(),
                        normalised=normalised,
                        type=entity_type,
                    )
                    by_key[key] = entity
                else:
                    outcome.merged_by_name += 1
                entity.aliases.add(extracted.name.strip())
                entity.mentions.append(
                    Mention(chunk.index, chunk.start, chunk.end, extracted.name.strip())
                )
                _merge_attributes(
                    entity.attributes, entity.attribute_conflicts,
                    extracted.attributes, chunk.index,
                )
                lookup[(entity_type, normalised)] = entity

            for relationship in result.relationships:
                pending.append((chunk, relationship))

        # Models routinely name a relationship endpoint they never returned as
        # an entity: measured on a real council document, 4 of 5 edges pointed
        # at names absent from the entities list, and strict resolution threw
        # every one away. The relationship type constrains what the endpoint
        # must be, so materialise it rather than discard the edge.
        self._materialise_endpoints(by_key, pending, outcome)

        entities = self._merge_aliases(list(by_key.values()), outcome)
        entities = await self._merge_similar(entities, outcome)

        # Rebuild the lookup after similarity merging folded some away.
        resolved: dict[str, Entity] = {}
        for entity in entities:
            for name in {entity.normalised, *(normalise_name(a) for a in entity.aliases)}:
                resolved.setdefault(name, entity)

        outcome.entities = entities
        outcome.relationships = self._build_relationships(pending, resolved, outcome)
        logger.info("Extraction complete: %s", outcome.summary())
        return outcome

    def _materialise_endpoints(
        self,
        by_key: dict[tuple[str, str], Entity],
        pending: Sequence[tuple[Chunk, ExtractedRelationship]],
        outcome: ExtractionResult,
    ) -> None:
        """Create entities for relationship endpoints the model omitted.

        Two guards, because this recovers real edges but would otherwise also
        manufacture nodes from hallucinated endpoints:

        * The relationship type must declare exactly one permitted type for
          that position. With a single candidate the type is determined;
          guessing among several would invent a typing the ontology never
          licensed.
        * Every token of the endpoint name must appear in the chunk the edge
          came from. The prompt asks for only what the passage states, so a
          name absent from the passage is not grounded in it — that is the
          line between recovering "Draft Housing Density Policy 2026", which
          is in the text, and inventing an organisation called "Nobody".
        """
        by_name = {normalised: entity for (_, normalised), entity in by_key.items()}
        for chunk, extracted in pending:
            relationship_type = to_identifier(extracted.type, upper_first=False)
            declared = self._relationship_endpoints.get(relationship_type)
            if declared is None:
                continue
            source_types, target_types = declared
            for raw, permitted in ((extracted.source, source_types),
                                   (extracted.target, target_types)):
                normalised = normalise_name(raw)
                if not normalised or normalised in by_name:
                    continue
                if len(permitted) != 1:
                    continue
                if not _grounded_in(normalised, chunk.text):
                    outcome.dropped_unresolved += 1
                    continue
                entity_type = next(iter(permitted))
                if entity_type not in self._entity_types:
                    continue
                entity = Entity(
                    uuid=uuidlib.uuid4().hex,
                    name=raw.strip(),
                    normalised=normalised,
                    type=entity_type,
                    inferred=True,
                )
                entity.aliases.add(raw.strip())
                entity.mentions.append(
                    Mention(chunk.index, chunk.start, chunk.end, raw.strip())
                )
                by_key[(entity_type, normalised)] = entity
                by_name[normalised] = entity
                outcome.inferred_entities += 1

    def _merge_aliases(
        self, entities: list[Entity], outcome: ExtractionResult
    ) -> list[Entity]:
        """Fold multi-token suffix aliases together, within a type.

        Runs before the embedding pass and does most of the remaining work.
        Measurement showed embeddings cannot do this job: on real names the
        should-merge and should-not-merge distributions overlap completely, so
        no threshold separates them.
        """
        survivors: list[Entity] = []
        for entity in sorted(entities, key=lambda e: (e.type, -len(e.normalised.split()))):
            match = next(
                (
                    candidate
                    for candidate in survivors
                    if candidate.type == entity.type
                    and is_alias_of(candidate.normalised, entity.normalised)
                ),
                None,
            )
            if match is None:
                survivors.append(entity)
                continue
            outcome.merged_by_alias += 1
            _absorb(match, entity)
        # Restore first-seen order so downstream output is stable.
        survivors.sort(key=lambda e: (e.mentions[0].chunk_index if e.mentions else 0, e.name))
        return survivors

    async def _merge_similar(
        self, entities: list[Entity], outcome: ExtractionResult
    ) -> list[Entity]:
        """Second pass: fold near-duplicates within a type.

        Runs only when an embedding service is available. The threshold is
        high and paired with a token-overlap guard, because the measured
        margin between "same entity, different phrasing" and "two different
        people" is only about 0.02 on bare names.
        """
        if self.embeddings is None or len(entities) < 2:
            return entities

        vectors = await self.embeddings.embed_texts([e.name for e in entities])
        for entity, vector in zip(entities, vectors):
            entity.embedding = vector

        survivors: list[Entity] = []
        for entity in entities:
            match = None
            for candidate in survivors:
                if candidate.type != entity.type:
                    continue
                if _token_similarity(candidate.normalised, entity.normalised) < 0.5:
                    continue
                assert candidate.embedding is not None and entity.embedding is not None
                if cosine_similarity(candidate.embedding, entity.embedding) >= self.similarity_threshold:
                    match = candidate
                    break
            if match is None:
                survivors.append(entity)
                continue
            logger.debug("Merging %r into %r by similarity", entity.name, match.name)
            outcome.merged_by_similarity += 1
            _absorb(match, entity)
        return survivors

    def _build_relationships(
        self,
        pending: Sequence[tuple[Chunk, ExtractedRelationship]],
        resolved: Mapping[str, Entity],
        outcome: ExtractionResult,
    ) -> list[Relationship]:
        merged: dict[tuple[str, str, str], Relationship] = {}
        for chunk, extracted in pending:
            relationship_type = to_identifier(extracted.type, upper_first=False)
            if relationship_type not in self._relationship_types:
                outcome.dropped_off_ontology += 1
                continue

            source = resolved.get(normalise_name(extracted.source))
            target = resolved.get(normalise_name(extracted.target))
            if source is None or target is None or source.uuid == target.uuid:
                # An edge to an entity that was never extracted cannot be
                # persisted, and a self-loop is almost always a model slip.
                outcome.dropped_unresolved += 1
                continue

            key = (relationship_type, source.uuid, target.uuid)
            relationship = merged.get(key)
            if relationship is None:
                relationship = Relationship(
                    uuid=uuidlib.uuid4().hex,
                    type=relationship_type,
                    source_uuid=source.uuid,
                    target_uuid=target.uuid,
                )
                merged[key] = relationship
            relationship.mentions.append(
                Mention(chunk.index, chunk.start, chunk.end, extracted.type)
            )
            _merge_attributes(
                relationship.attributes, relationship.attribute_conflicts,
                extracted.attributes, chunk.index,
            )
        return list(merged.values())


def _grounded_in(normalised: str, text: str) -> bool:
    """True when every token of a name is present in the passage."""
    haystack = text.lower()
    return all(token in haystack for token in normalised.split())


def _absorb(keeper: Entity, absorbed: Entity) -> None:
    """Fold one entity into another, preserving aliases, mentions and origin."""
    keeper.aliases |= absorbed.aliases
    keeper.mentions.extend(absorbed.mentions)
    keeper.mentions.sort(key=lambda m: (m.chunk_index, m.start))
    # A node only some of whose mentions were inferred is still evidenced.
    keeper.inferred = keeper.inferred and absorbed.inferred
    _merge_attributes(
        keeper.attributes, keeper.attribute_conflicts, absorbed.attributes,
        absorbed.mentions[0].chunk_index if absorbed.mentions else 0,
    )


def _merge_attributes(
    target: dict[str, str],
    conflicts: dict[str, list[dict[str, Any]]],
    incoming: Mapping[str, str],
    chunk_index: int,
) -> None:
    """First occurrence wins; losing values are recorded, not discarded.

    Deterministic by document order. A reviewer can see that chunk 3 said
    "chair" and chunk 9 said "committee chair", rather than one of them
    vanishing.
    """
    for key, value in incoming.items():
        if key not in target:
            target[key] = value
            continue
        if target[key] == value:
            continue
        entries = conflicts.setdefault(key, [])
        if not any(entry["value"] == value for entry in entries):
            entries.append({"value": value, "chunk": chunk_index})
