"""Propose a domain-appropriate ontology for a document.

A fixed schema forces every document into the same shape: a hospital incident
report and a municipal housing draft both become People and Organisations,
losing the distinctions that make the eventual simulation worth anything. This
module asks the model what types the document actually needs, then hands the
result to the operator for review before extraction commits to it.

Two constraints shape the design.

**Type names become Neo4j labels.** A model asked for entity types will happily
answer "Public Figure" or "Local Government Body", neither of which is a legal
Cypher identifier. Names are normalised to PascalCase and the original is kept
as a human-readable label, so operators and the extraction prompt see readable
text while the graph gets valid identifiers. Re-prompting for a formatting fix
that a two-line transform handles would spend 30–90 seconds of local inference
per attempt, and small models tend to re-offend anyway.

**The sample must represent the document.** Policy drafts open with a title
page and boilerplate; an ontology derived from the first few thousand
characters describes the cover. The sample is stratified across the beginning,
middle and end, so types that only appear in the substance are seen.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import Config, get_config
from app.utils.chunker import Chunk, chunk_text, iter_sentence_spans
from app.utils.llm_client import LLMClient

logger = logging.getLogger(__name__)

__all__ = [
    "EntityType",
    "Ontology",
    "OntologyError",
    "OntologyGenerator",
    "RelationshipType",
    "build_sample",
    "to_identifier",
]

# How much document text the model sees. Large enough to span several sections,
# small enough to leave a 14b model room to think inside its context window.
DEFAULT_SAMPLE_BUDGET = 12_000

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class OntologyError(RuntimeError):
    """The model could not produce a usable ontology."""


def to_identifier(name: str, *, upper_first: bool = True) -> str:
    """Turn a human phrase into a bare Cypher identifier.

    ``"Public Figure"`` becomes ``PublicFigure``; ``"works-for"`` becomes
    ``WORKS_FOR`` when ``upper_first`` is false. Returns ``""`` when nothing
    usable survives, which callers treat as a rejection rather than papering
    over with a placeholder.
    """
    words = [w for w in _NON_ALNUM.split(name.strip()) if w]
    if not words:
        return ""
    parts: list[str] = []
    for word in words:
        # Split existing camelCase so "publicFigure" and "Public Figure" agree.
        parts.extend(p for p in _CAMEL_BOUNDARY.split(word) if p)
    if upper_first:
        identifier = "".join(p[:1].upper() + p[1:] for p in parts)
    else:
        identifier = "_".join(p.upper() for p in parts)
    if not identifier or not (identifier[0].isalpha() or identifier[0] == "_"):
        return ""
    return identifier[:63]


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


class EntityType(BaseModel):
    """One kind of node the graph may contain."""

    name: str = Field(description="PascalCase identifier, e.g. Person")
    label: str = Field(default="", description="Human-readable name")
    description: str = Field(description="What this type represents")
    attributes: list[str] = Field(
        default_factory=list, description="Expected properties, e.g. role, age"
    )

    @model_validator(mode="after")
    def _normalise(self) -> "EntityType":
        original = self.label or self.name
        identifier = to_identifier(self.name)
        if not identifier:
            raise ValueError(
                f"entity type name {self.name!r} contains no usable identifier "
                f"characters"
            )
        object.__setattr__(self, "name", identifier)
        object.__setattr__(self, "label", original.strip() or identifier)
        object.__setattr__(
            self, "attributes", _clean_attributes(self.attributes)
        )
        return self


class RelationshipType(BaseModel):
    """One kind of edge the graph may contain."""

    name: str = Field(description="UPPER_SNAKE_CASE identifier, e.g. WORKS_FOR")
    label: str = Field(default="", description="Human-readable name")
    description: str = Field(description="What this relationship means")
    source_types: list[str] = Field(
        default_factory=list, description="Entity types this may start from"
    )
    target_types: list[str] = Field(
        default_factory=list, description="Entity types this may point to"
    )
    attributes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalise(self) -> "RelationshipType":
        original = self.label or self.name
        identifier = to_identifier(self.name, upper_first=False)
        if not identifier:
            raise ValueError(
                f"relationship type name {self.name!r} contains no usable "
                f"identifier characters"
            )
        object.__setattr__(self, "name", identifier)
        object.__setattr__(self, "label", original.strip() or identifier)
        for field in ("source_types", "target_types"):
            values = [to_identifier(v) for v in getattr(self, field)]
            object.__setattr__(self, field, [v for v in values if v])
        object.__setattr__(self, "attributes", _clean_attributes(self.attributes))
        return self


def _clean_attributes(values: Iterable[str]) -> list[str]:
    """Attribute names are node properties, so snake_case and deduplicated."""
    seen: dict[str, None] = {}
    for value in values:
        cleaned = "_".join(w.lower() for w in _NON_ALNUM.split(str(value)) if w)
        if cleaned and cleaned[0].isalpha():
            seen.setdefault(cleaned[:63], None)
    return list(seen)


class Ontology(BaseModel):
    """The proposed schema for one document's graph."""

    domain: str = Field(default="", description="One-line description of the domain")
    entity_types: list[EntityType]
    relationship_types: list[RelationshipType] = Field(default_factory=list)

    @field_validator("entity_types")
    @classmethod
    def _require_entity_types(cls, value: list[EntityType]) -> list[EntityType]:
        if not value:
            raise ValueError("an ontology needs at least one entity type")
        return value

    @model_validator(mode="after")
    def _dedupe_and_link(self) -> "Ontology":
        """Drop duplicate types and relationships pointing at unknown types.

        A relationship whose endpoints are not in the ontology cannot be
        extracted, and leaving it in produces edges the graph schema has no
        place for — a failure that surfaces during ingestion rather than here.
        """
        entities: dict[str, EntityType] = {}
        for entity in self.entity_types:
            entities.setdefault(entity.name, entity)
        object.__setattr__(self, "entity_types", list(entities.values()))

        known = set(entities)
        relationships: dict[str, RelationshipType] = {}
        for relationship in self.relationship_types:
            endpoints = relationship.source_types + relationship.target_types
            unknown = [e for e in endpoints if e not in known]
            if unknown:
                logger.debug(
                    "Dropping relationship %s: unknown endpoint type(s) %s",
                    relationship.name, unknown,
                )
                continue
            relationships.setdefault(relationship.name, relationship)
        object.__setattr__(self, "relationship_types", list(relationships.values()))
        return self

    # -- persistence --------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(self.model_dump(), indent=2, sort_keys=True)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Ontology":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def summary(self) -> str:
        return (
            f"{len(self.entity_types)} entity type(s): "
            f"{', '.join(e.name for e in self.entity_types)}; "
            f"{len(self.relationship_types)} relationship type(s)"
        )


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------


def build_sample(
    text: str,
    *,
    budget: int = DEFAULT_SAMPLE_BUDGET,
    config: Config | None = None,
) -> str:
    """Take a representative slice of the document, spread across its length.

    Returns the whole text when it fits. Otherwise takes chunks from the
    opening, middle and close in equal measure, joined by an explicit elision
    marker so the model is not misled into reading two distant passages as
    consecutive.
    """
    text = text.strip()
    if len(text) <= budget:
        return text

    chunks = chunk_text(text, config=config)
    if not chunks:
        return text[:budget]

    per_section = max(1, budget // 3)
    starts = [0, len(chunks) // 3, (2 * len(chunks)) // 3]

    seen: set[int] = set()
    sections: list[str] = []
    for start in starts:
        fresh = [c for c in chunks[start:] if c.index not in seen]
        section = _take(fresh, per_section)
        if not section:
            continue
        seen.update(c.index for c in section)
        sections.append(_trim_to(" ".join(c.text for c in section), per_section))
    return "\n\n[...]\n\n".join(s for s in sections if s)


def _take(chunks: Sequence[Chunk], budget: int) -> list[Chunk]:
    """Whole chunks fitting the budget, or the first one if none fits."""
    taken: list[Chunk] = []
    total = 0
    for chunk in chunks:
        if total + len(chunk) > budget and taken:
            break
        taken.append(chunk)
        total += len(chunk)
        if total >= budget:
            break
    return taken


def _trim_to(text: str, budget: int) -> str:
    """Cut ``text`` down to ``budget`` characters at a sentence boundary.

    A chunk can be larger than a section's share of the budget — at the
    default settings a chunk is 1500 characters and a section gets 4000, but a
    caller passing a smaller budget would otherwise get a sample several times
    the size it asked for. Trimming at a sentence boundary keeps the tail
    readable; a hard cut is the fallback for text with no sentence breaks.
    """
    if len(text) <= budget:
        return text
    kept = 0
    for start, end in iter_sentence_spans(text):
        if end > budget:
            break
        kept = end
    return text[:kept].strip() if kept else text[:budget].rstrip()


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a knowledge-engineering assistant. You design ontologies: the set of \
entity types and relationship types needed to represent a specific document as \
a knowledge graph.

Rules:
- Propose types the document actually needs. Do not pad with generic types \
that never appear in it.
- Between 4 and 12 entity types, and between 3 and 15 relationship types.
- Entity type names are PascalCase, e.g. Person, Organisation, HousingPolicy.
- Relationship type names are UPPER_SNAKE_CASE, e.g. WORKS_FOR, OPPOSES.
- Every relationship must list source_types and target_types drawn only from \
the entity types you propose.
- Attributes are the properties worth recording for that type, in snake_case.
- Descriptions are one sentence, written for a human reviewer."""

USER_PROMPT = """\
Design a knowledge-graph ontology for the following document.

The text below is a representative sample. Where it reads "[...]" a passage has \
been omitted; the sample is drawn from the beginning, middle and end of the \
document.

--- DOCUMENT SAMPLE ---
{sample}
--- END SAMPLE ---

Propose the entity types and relationship types this document needs."""


class OntologyGenerator:
    """Asks the local model to propose an ontology for a document."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        llm: LLMClient | None = None,
        sample_budget: int = DEFAULT_SAMPLE_BUDGET,
    ) -> None:
        self.config = config or get_config()
        self.llm = llm or LLMClient(self.config)
        self.sample_budget = sample_budget

    async def generate(self, text: str, *, temperature: float = 0.2) -> Ontology:
        """Propose an ontology for ``text``.

        Temperature is low by default: this is a structuring task, and
        creativity here shows up as invented types the document never mentions.
        """
        sample = build_sample(text, budget=self.sample_budget, config=self.config)
        if not sample.strip():
            raise OntologyError("Cannot derive an ontology from empty text.")

        logger.info(
            "Requesting ontology from %s over a %d-character sample",
            self.config.LLM_MODEL_NAME, len(sample),
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT.format(sample=sample)},
        ]
        try:
            ontology = await self.llm.complete_json(
                messages, Ontology, temperature=temperature
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed error
            raise OntologyError(f"Ontology generation failed: {exc}") from exc

        logger.info("Ontology proposed: %s", ontology.summary())
        return ontology

    async def aclose(self) -> None:
        await self.llm.aclose()
