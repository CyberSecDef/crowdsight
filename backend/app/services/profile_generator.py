"""Turn graph entities into agent personas.

**Eligibility cannot be a hard-coded ``Person`` filter.** The ontology is
generated per document, and real runs produce ``Councillor``, ``Mayor``,
``PlanningOfficer``, ``ResidentsAssociation`` — never the literal type
``Person``. Filtering on that name selects nobody, silently. So the ontology's
types are classified once, cheaply, into individuals, institutions and neither,
and ``Person`` is always available as a fallback type regardless of what the
ontology proposed.

**A population is not a cast of office-holders.** A document names the people
with titles — the councillor, the mayor, the planning officer — because those
are the people documents name. A crowd reacting to a housing policy is mostly
not those people: it is mechanics, carpenters, care workers, shop staff, bus
drivers, students, retirees and the unemployed. Personas therefore draw
occupations across the whole spectrum in :data:`OCCUPATION_SECTORS`, and the
prompt says so explicitly, because a model left to its own devices populates a
town with consultants and directors.

Named entities stay truthful to the source: their occupation comes from the
document, not from the pool. The pool is for the synthetic majority that Phase
4 Step 2 generates, and for grounding the prompt's sense of what ordinary looks
like.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import Config, get_config
from app.services.ontology_generator import Ontology
from app.utils.llm_client import LLMClient, LLMJSONError

logger = logging.getLogger(__name__)

__all__ = [
    "OCCUPATION_SECTORS",
    "PERSON_FALLBACK_TYPE",
    "EntityRoles",
    "PersonaProfile",
    "ProfileError",
    "ProfileGenerator",
    "classify_entity_types",
    "normalise_age",
    "normalise_unit_interval",
]

#: Always eligible, whatever the ontology proposed. Ordinary people rarely earn
#: a type of their own in a document's ontology, but they are most of a crowd.
PERSON_FALLBACK_TYPE = "Person"

#: Deliberately weighted towards ordinary work. A model asked for "a resident"
#: reaches for consultants, analysts and directors; a real street does not look
#: like that.
OCCUPATION_SECTORS: dict[str, tuple[str, ...]] = {
    "skilled trades": (
        "mechanic", "carpenter", "plumber", "electrician", "welder", "roofer",
        "bricklayer", "painter and decorator", "HVAC engineer", "locksmith",
        "landscaper", "scaffolder", "glazier", "upholsterer",
    ),
    "transport and logistics": (
        "bus driver", "lorry driver", "delivery courier", "taxi driver",
        "forklift operator", "warehouse picker", "train conductor",
        "dispatch coordinator",
    ),
    "retail and hospitality": (
        "shop assistant", "supermarket supervisor", "barista", "chef",
        "kitchen porter", "bartender", "hotel receptionist", "waiter",
        "market trader", "hairdresser", "barber", "beautician",
    ),
    "care and health": (
        "care worker", "nurse", "healthcare assistant", "paramedic",
        "pharmacy technician", "dental nurse", "childminder", "midwife",
        "physiotherapist", "support worker",
    ),
    "education and public service": (
        "teaching assistant", "primary teacher", "librarian", "social worker",
        "refuse collector", "street cleaner", "postal worker", "firefighter",
        "police officer", "council administrator", "school caretaker",
    ),
    "office and professional": (
        "accountant", "solicitor", "surveyor", "architect", "software developer",
        "estate agent", "insurance broker", "HR officer", "journalist",
        "graphic designer", "civil engineer",
    ),
    "manual and industrial": (
        "factory operative", "cleaner", "security guard", "groundworker",
        "farm worker", "fishmonger", "butcher", "baker", "printer",
        "recycling plant operative",
    ),
    "self-employed and small business": (
        "shopkeeper", "café owner", "driving instructor", "freelance photographer",
        "gardener", "dog walker", "small landlord", "market stallholder",
    ),
    "not in paid work": (
        "retired", "student", "apprentice", "full-time carer", "unemployed",
        "stay-at-home parent", "on long-term sick leave", "volunteer",
    ),
}

ALL_OCCUPATIONS: tuple[str, ...] = tuple(
    occupation for group in OCCUPATION_SECTORS.values() for occupation in group
)

ActivityLevel = Literal["low", "moderate", "high"]

_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
_LEVEL_WORDS = {
    "very low": 0.1, "low": 0.25, "below average": 0.35, "moderate": 0.5,
    "medium": 0.5, "average": 0.5, "above average": 0.65, "high": 0.75,
    "very high": 0.9,
}

MIN_AGE = 16
MAX_AGE = 99


class ProfileError(RuntimeError):
    """A persona could not be produced for an entity."""


# --------------------------------------------------------------------------
# Coercion helpers
# --------------------------------------------------------------------------


def normalise_age(value: Any) -> int:
    """Coerce whatever the model returned into a plausible age.

    Models answer ``34``, ``"34"``, ``"thirty-four"``, ``"34 years old"`` and
    ``"mid-thirties"`` interchangeably. Rejecting the lot would cost a repair
    round trip — 30-90 s of local inference — for something with an
    unambiguous reading.
    """
    if isinstance(value, bool):
        raise ValueError("age must be a number, not a boolean")
    if isinstance(value, (int, float)):
        age = int(value)
    else:
        text = str(value).strip().lower()
        digits = re.search(r"\d{1,3}", text)
        if digits:
            age = int(digits.group())
        else:
            age = _age_from_words(text)
    if not MIN_AGE <= age <= MAX_AGE:
        raise ValueError(f"age {age} is outside {MIN_AGE}-{MAX_AGE}")
    return age


def _age_from_words(text: str) -> int:
    cleaned = text.replace("-", " ").replace("_", " ")
    # "mid-thirties" / "early forties" -> the decade, nudged.
    decade = re.search(r"(twenties|thirties|forties|fifties|sixties|seventies|eighties)", cleaned)
    if decade:
        base = {"twenties": 20, "thirties": 30, "forties": 40, "fifties": 50,
                "sixties": 60, "seventies": 70, "eighties": 80}[decade.group(1)]
        if "early" in cleaned:
            return base + 2
        if "late" in cleaned:
            return base + 8
        return base + 5
    total = 0
    found = False
    for word in cleaned.split():
        if word in _WORD_NUMBERS:
            total += _WORD_NUMBERS[word]
            found = True
    if not found:
        raise ValueError(f"cannot read an age from {text!r}")
    return total


def normalise_unit_interval(value: Any) -> float:
    """Coerce a personality score into 0..1.

    Accepts floats, percentages, 1-10 scales and words like "high". Clamps
    rather than rejects: a model that answers 1.2 meant "very high", and
    failing the whole profile over it helps nobody.
    """
    if isinstance(value, bool):
        raise ValueError("expected a number, not a boolean")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().lower().rstrip("%")
        if text in _LEVEL_WORDS:
            return _LEVEL_WORDS[text]
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            raise ValueError(f"cannot read a score from {value!r}")
        number = float(match.group())
        if "%" in str(value):
            number /= 100.0
    # Above 1.0 the intended scale is ambiguous, so disambiguate on shape:
    #   1 < n < 2   an overshoot of 0..1 — a model asked for 0..1 answering
    #               1.4 meant "very high", not "0.14"
    #   2 <= n <= 10 a 1-10 scale
    #   n > 10      a percentage
    if 1.0 < number < 2.0:
        number = 1.0
    elif 2.0 <= number <= 10.0:
        number /= 10.0
    elif number > 10.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


class BigFive(BaseModel):
    """Numeric personality, so it can be validated, clustered and plotted."""

    openness: float = 0.5
    conscientiousness: float = 0.5
    extraversion: float = 0.5
    agreeableness: float = 0.5
    neuroticism: float = 0.5

    @field_validator("*", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return normalise_unit_interval(value) if value is not None else 0.5


class PersonaProfile(BaseModel):
    """One agent's persona."""

    name: str
    age: int
    occupation: str
    sector: str = ""
    background: str = Field(description="A few sentences of biography")
    personality: BigFive = Field(default_factory=BigFive)
    traits: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    leanings: str = Field(default="", description="Political or topical disposition")
    activity_level: ActivityLevel = "moderate"
    writing_style: str = ""

    # Set by the generator, not the model.
    provenance: Literal["named", "synthetic"] = "synthetic"
    source_entity_uuid: str | None = None
    source_entity_type: str | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _accept_field_drift(cls, data: Any) -> Any:
        """Map the field names models actually use onto ours."""
        if not isinstance(data, Mapping):
            return data
        aliases = {
            "bio": "background", "biography": "background", "summary": "background",
            "job": "occupation", "profession": "occupation", "role": "occupation",
            "job_title": "occupation",
            "personality_traits": "traits", "characteristics": "traits",
            "big_five": "personality", "big5": "personality",
            "political_leaning": "leanings", "political_leanings": "leanings",
            "leaning": "leanings", "stance": "leanings",
            "activity": "activity_level", "posting_frequency": "activity_level",
            "style": "writing_style", "writing_style_hint": "writing_style",
            "tone": "writing_style", "hobbies": "interests",
        }
        merged = dict(data)
        for alias, canonical in aliases.items():
            if alias in merged and canonical not in merged:
                merged[canonical] = merged.pop(alias)
        return merged

    @field_validator("age", mode="before")
    @classmethod
    def _coerce_age(cls, value: Any) -> Any:
        return normalise_age(value)

    @field_validator("activity_level", mode="before")
    @classmethod
    def _coerce_activity(cls, value: Any) -> Any:
        if value is None:
            return "moderate"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            score = normalise_unit_interval(value)
        else:
            text = str(value).strip().lower()
            if text in {"low", "moderate", "high"}:
                return text
            if text in {"medium", "average"}:
                return "moderate"
            if text in {"very high", "very active", "prolific"}:
                return "high"
            if text in {"very low", "rare", "lurker", "inactive"}:
                return "low"
            try:
                score = normalise_unit_interval(text)
            except ValueError:
                return "moderate"
        return "low" if score < 0.34 else "high" if score > 0.66 else "moderate"

    @field_validator("traits", "interests", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in re.split(r"[,;]|\band\b", value) if part.strip()]
        if isinstance(value, Mapping):
            return [f"{k}: {v}" for k, v in value.items()]
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("name", "occupation", mode="before")
    @classmethod
    def _require_text(cls, value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("leanings", "writing_style", "background", "sector", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value)
        return str(value).strip()


# --------------------------------------------------------------------------
# Which ontology types are people
# --------------------------------------------------------------------------


class EntityRoles(BaseModel):
    """Which of an ontology's types can become agents, and how."""

    individuals: list[str] = Field(default_factory=list)
    institutions: list[str] = Field(default_factory=list)
    neither: list[str] = Field(default_factory=list)

    def eligible(self, *, include_institutions: bool = False) -> list[str]:
        types = list(self.individuals)
        if include_institutions:
            types.extend(self.institutions)
        if PERSON_FALLBACK_TYPE not in types:
            types.append(PERSON_FALLBACK_TYPE)
        return types

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "EntityRoles":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


CLASSIFY_SYSTEM = """\
You classify ontology entity types. For each type decide whether it denotes:

- "individual": a single human being, whatever their title or trade — a \
councillor, a mechanic, a resident, a nurse.
- "institution": an organisation, body or group that can hold a public \
position — a council, a residents association, a company, a newspaper.
- "neither": anything that is not a person or a body — a document, a place, \
an event, a proposal, a time period.

Return every type given, in exactly one category."""

CLASSIFY_USER = """\
Classify these entity types from a knowledge graph about: {domain}

{types}

Return JSON with keys "individuals", "institutions" and "neither"."""


async def classify_entity_types(
    ontology: Ontology, llm: LLMClient, *, temperature: float = 0.0
) -> EntityRoles:
    """Decide which of an ontology's types denote people.

    One call per ontology, not per entity. Falls back to putting everything in
    ``neither`` if the model fails — with ``Person`` still eligible, so
    generation degrades to a synthetic-only population rather than crashing.
    """
    listing = "\n".join(
        f"- {t.name}: {t.description}" for t in ontology.entity_types
    )
    messages = [
        {"role": "system", "content": CLASSIFY_SYSTEM},
        {"role": "user", "content": CLASSIFY_USER.format(
            domain=ontology.domain or "an unspecified domain", types=listing)},
    ]
    try:
        roles = await llm.complete_json(messages, EntityRoles, temperature=temperature)
    except LLMJSONError as exc:
        logger.warning("Entity type classification failed (%s); falling back", exc)
        return EntityRoles(neither=[t.name for t in ontology.entity_types])

    known = {t.name for t in ontology.entity_types}
    return EntityRoles(
        individuals=[t for t in roles.individuals if t in known],
        institutions=[t for t in roles.institutions if t in known],
        neither=[t for t in roles.neither if t in known],
    )


# --------------------------------------------------------------------------
# Persona synthesis
# --------------------------------------------------------------------------

PERSONA_SYSTEM = """\
You write personas for a social simulation. Each persona is one plausible \
individual who might react publicly to an event.

Rules:
- Ground a named person in what the source says about them. Do not invent a \
career for someone the document already describes.
- Personas must span ordinary life. Most people are not directors, consultants \
or analysts. Mechanics, carpenters, care workers, shop staff, drivers, \
cleaners, students, carers and retirees are the majority of any population.
- Ages, temperaments and opinions should vary. A population where everyone is \
45 and mildly concerned is useless.
- personality scores are floats between 0 and 1.
- activity_level is exactly one of "low", "moderate" or "high".
- Write background and writing_style as plain prose, not lists."""

PERSONA_USER = """\
Create one persona.

{context}

Occupation guidance: draw from ordinary working life unless the source says \
otherwise. Examples across the spectrum: {examples}.

Return JSON with: name, age, occupation, sector, background, personality \
(openness, conscientiousness, extraversion, agreeableness, neuroticism), \
traits, interests, leanings, activity_level, writing_style."""


@dataclass
class EntityContext:
    """What the graph knows about an entity, as prompt material."""

    uuid: str
    name: str
    type: str
    attributes: dict[str, str] = field(default_factory=dict)
    passages: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    #: When set, the persona must have this occupation. Suggesting a spread of
    #: examples is not enough: given eight suggestions the model reaches for
    #: the same one or two, and a measured run produced five carpenters and
    #: landscapers out of nine. Assigning removes the choice.
    assigned_occupation: str | None = None
    #: Set for synthetic crowd members. The allocated name is authoritative: it
    #: has been checked against every name the document uses, and the generator
    #: overwrites whatever the model returns with it.
    assigned_name: str | None = None
    assigned_age: int | None = None
    assigned_stance: str | None = None
    group: str | None = None
    synthetic: bool = False

    def render(self) -> str:
        if self.synthetic:
            return self._render_synthetic()
        lines = [f"This person is named in the source document as: {self.name}",
                 f"The document classifies them as: {self.type}"]
        if self.assigned_occupation:
            lines.append(
                f"Their occupation is exactly: {self.assigned_occupation}. "
                f"Use it verbatim; do not substitute a different job."
            )
        if self.attributes:
            described = ", ".join(f"{k}: {v}" for k, v in sorted(self.attributes.items()))
            lines.append(f"Recorded attributes: {described}")
        if self.relationships:
            lines.append("Connections: " + "; ".join(self.relationships[:6]))
        if self.passages:
            excerpt = " ".join(" ".join(p.split()) for p in self.passages[:2])
            lines.append(f"Source passages mentioning them: {excerpt[:1200]}")
        return "\n".join(lines)

    def _render_synthetic(self) -> str:
        """A crowd member: invented, ordinary, and not in the document.

        The prompt says so explicitly. A model told to write "a resident"
        without that instruction reaches for someone the document mentions,
        which is the one thing a synthetic persona must not be.
        """
        lines = [
            "Invent one ordinary member of the public affected by this event.",
            "They are NOT named in the source document and are not a public "
            "figure, official or spokesperson.",
            f"Their name is exactly: {self.assigned_name or self.name}. Use it "
            f"verbatim.",
        ]
        if self.assigned_occupation:
            lines.append(
                f"Their occupation is exactly: {self.assigned_occupation}. "
                f"Use it verbatim; do not substitute a different job."
            )
        if self.assigned_age:
            lines.append(f"They are {self.assigned_age} years old.")
        if self.group:
            lines.append(f"They belong to this part of the population: {self.group}.")
        if self.assigned_stance:
            lines.append(f"Their view of the event: {self.assigned_stance}.")
        if self.passages:
            excerpt = " ".join(" ".join(p.split()) for p in self.passages[:2])
            lines.append(f"Context for the event: {excerpt[:1000]}")
        return "\n".join(lines)


class ProfileGenerator:
    """Synthesises personas from graph entities."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        llm: LLMClient | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config or get_config()
        self.llm = llm or LLMClient(self.config)
        self.rng = rng or random.Random()

    # -- eligibility --------------------------------------------------------

    async def classify(self, ontology: Ontology) -> EntityRoles:
        return await classify_entity_types(ontology, self.llm)

    def select(
        self,
        entities: Sequence[Mapping[str, Any]],
        roles: EntityRoles,
        *,
        include_institutions: bool = False,
    ) -> list[Mapping[str, Any]]:
        """Entities eligible to become agents, most-mentioned first."""
        eligible = set(roles.eligible(include_institutions=include_institutions))
        chosen = [e for e in entities if e.get("type") in eligible]
        chosen.sort(key=lambda e: (-int(e.get("mention_count") or 0), str(e.get("name"))))
        return chosen

    # -- generation ---------------------------------------------------------

    def sample_occupations(self, count: int) -> list[str]:
        """Occupations spread evenly across sectors.

        Round-robin rather than uniform random: sampling uniformly from the
        flat pool over-represents whichever sector happens to be largest, and
        the point is a population that looks like a street.
        """
        sectors = list(OCCUPATION_SECTORS)
        self.rng.shuffle(sectors)
        pools = {s: list(OCCUPATION_SECTORS[s]) for s in sectors}
        for pool in pools.values():
            self.rng.shuffle(pool)

        chosen: list[str] = []
        while len(chosen) < count:
            drained = True
            for sector in sectors:
                if not pools[sector]:
                    continue
                drained = False
                chosen.append(pools[sector].pop())
                if len(chosen) == count:
                    break
            if drained:  # more requested than the pool holds; recycle
                pools = {s: list(OCCUPATION_SECTORS[s]) for s in sectors}
        return chosen

    def occupation_examples(self, count: int = 8) -> str:
        """A spread across sectors, so the prompt is not anchored on one."""
        sectors = list(OCCUPATION_SECTORS)
        self.rng.shuffle(sectors)
        picks = [self.rng.choice(OCCUPATION_SECTORS[s]) for s in sectors[:count]]
        return ", ".join(picks)

    async def generate_for_entity(
        self, context: EntityContext, *, temperature: float = 0.8
    ) -> PersonaProfile:
        """A persona grounded in what the document says about this entity.

        Temperature is high here, unlike the structuring stages: a population
        of near-identical personas is useless, and the variation has to come
        from somewhere.
        """
        messages = [
            {"role": "system", "content": PERSONA_SYSTEM},
            {"role": "user", "content": PERSONA_USER.format(
                context=context.render(), examples=self.occupation_examples())},
        ]
        try:
            profile = await self.llm.complete_json(
                messages, PersonaProfile, temperature=temperature
            )
        except LLMJSONError as exc:
            raise ProfileError(
                f"Could not generate a persona for {context.name!r}: {exc}"
            ) from exc

        if context.synthetic:
            profile.provenance = "synthetic"
            # The allocated name is the safety property, not a suggestion: it
            # was checked against every name the document uses. Whatever the
            # model returned is discarded.
            if context.assigned_name:
                profile.name = context.assigned_name
            if context.assigned_age:
                profile.age = context.assigned_age
            profile.source_entity_uuid = None
            profile.source_entity_type = None
        else:
            profile.provenance = "named"
            profile.source_entity_uuid = context.uuid
            profile.source_entity_type = context.type
        if context.assigned_occupation:
            profile.occupation = context.assigned_occupation
        # The model invents its own sector labels ("Construction", "Community"),
        # which are useless for clustering. Ours wins whenever the occupation
        # is one we know.
        derived = _sector_for(profile.occupation)
        if derived != "other" or not profile.sector:
            profile.sector = derived
        return profile

    async def generate_many(
        self, contexts: Sequence[EntityContext], *, temperature: float = 0.8
    ) -> tuple[list[PersonaProfile], list[str]]:
        """Generate in parallel. Returns profiles and the names that failed.

        One entity that will not yield a usable persona should cost that
        entity, not the population.
        """
        results = await asyncio.gather(
            *(self.generate_for_entity(c, temperature=temperature) for c in contexts),
            return_exceptions=True,
        )
        profiles: list[PersonaProfile] = []
        failures: list[str] = []
        for context, outcome in zip(contexts, results):
            if isinstance(outcome, PersonaProfile):
                profiles.append(outcome)
            else:
                logger.warning("Persona failed for %s: %s", context.name, outcome)
                failures.append(context.name)
        return profiles, failures

    async def aclose(self) -> None:
        await self.llm.aclose()


def _sector_for(occupation: str) -> str:
    needle = occupation.strip().lower()
    for sector, occupations in OCCUPATION_SECTORS.items():
        if any(needle == o or o in needle for o in occupations):
            return sector
    return "other"
