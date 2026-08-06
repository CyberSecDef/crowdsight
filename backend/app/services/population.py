"""Expand a handful of named actors into a crowd.

A document names the people documents name: the councillor, the mayor, the
officer. Ten named actors do not make a population, and a simulation of ten
office-holders answers a different question from the one being asked.

**Provenance is the safety property of this module.** Every profile is marked
`named` or `synthetic`, and a synthetic agent may never carry the name of
someone the document names. Without that, a simulated quote becomes a
fabricated statement attributed to a real, identifiable person — which is the
line between a defensible simulation and a libel.

The collision check is normalised, not literal: `Cllr. Jane Doe` and `jane doe`
are the same person for this purpose, so the same normalisation that
deduplicates entities in Phase 3 guards names here.

What this module cannot promise: that an invented name matches nobody
anywhere. `Dawn Mercer` is a real person somewhere. What it does promise is
that no synthetic agent shares a name with anyone *this document names*, and
that every consumer can tell which agents are invented.

**The crowd is grounded, but sampled locally.** One call sketches who the event
actually affects and how opinion divides; occupations, ages and stances are
then sampled here. Step 1 measured what happens when the model is left to
invent whole personas unaided — five carpenters out of nine — so the spread
stays under our control while the substance stays tied to the document.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, Field, field_validator

from app.config import Config, get_config
from app.services.ontology_generator import Ontology
from app.services.profile_generator import (
    EntityContext,
    PersonaProfile,
    ProfileGenerator,
)
from app.storage.ner_extractor import normalise_name
from app.utils.llm_client import LLMClient, LLMJSONError

logger = logging.getLogger(__name__)

__all__ = [
    "NameAllocator",
    "PopulationPlan",
    "PopulationSketch",
    "Stance",
    "build_population",
    "plan_population",
    "sketch_population",
]

# Common given names across a range of origins, chosen to read as ordinary
# rather than notable. Collision with the document's own people is checked
# separately and is the guarantee that actually matters.
FIRST_NAMES: tuple[str, ...] = (
    "Aisha", "Alan", "Amara", "Andrei", "Aneta", "Bilal", "Bridget", "Callum",
    "Carys", "Chidi", "Clara", "Dawn", "Declan", "Dilnoza", "Eamon", "Edith",
    "Elif", "Emeka", "Esme", "Fatima", "Fergus", "Fiona", "Gareth", "Gita",
    "Hannah", "Harun", "Heather", "Ibrahim", "Imogen", "Ines", "Ivan", "Jacek",
    "Jamila", "Joanna", "Kaito", "Karen", "Kwame", "Lena", "Lorna", "Lucia",
    "Mairead", "Marek", "Mei", "Nadia", "Neil", "Nkechi", "Olga", "Omar",
    "Padraig", "Petra", "Priya", "Rafiq", "Ravi", "Rhian", "Rosa", "Ruth",
    "Sanjay", "Seren", "Sofia", "Stefan", "Tamsin", "Tariq", "Teresa", "Tomas",
    "Ursula", "Vikram", "Wanda", "Yusuf", "Zainab", "Zoltan",
)

SURNAMES: tuple[str, ...] = (
    "Achebe", "Ahmed", "Bakker", "Beringer", "Blaszczyk", "Calloway", "Castell",
    "Chowdhury", "Coyle", "Dalgleish", "Delaney", "Dimitrova", "Eze", "Fenwick",
    "Ferreira", "Gallagher", "Ghosh", "Halloran", "Hartnell", "Iqbal", "Janssen",
    "Kalinowski", "Keane", "Kowalczyk", "Larkin", "Lindqvist", "Mercer",
    "Mbeki", "Nakamura", "Novak", "Oyelaran", "Pargeter", "Petrov", "Quill",
    "Rahman", "Ravenscroft", "Rosales", "Sandoval", "Sharpe", "Sinclair",
    "Sowande", "Tremaine", "Vance", "Vasilyev", "Whitlock", "Wojcik", "Yilmaz",
    "Zhang",
)


class NameAllocator:
    """Hands out names that collide with nobody the document names.

    Reserved names are normalised the same way entity names are, so
    ``Cllr. Jane Doe`` reserves ``Jane Doe``. Exhausting the pool falls back to
    a numbered suffix rather than reusing a name: two agents with one name is a
    correctness problem in every downstream join.
    """

    def __init__(
        self,
        reserved: Iterable[str] = (),
        *,
        rng: random.Random | None = None,
        first_names: Sequence[str] = FIRST_NAMES,
        surnames: Sequence[str] = SURNAMES,
    ) -> None:
        self.rng = rng or random.Random()
        self.first_names = list(first_names)
        self.surnames = list(surnames)
        self._reserved = {normalise_name(name) for name in reserved}
        self._reserved.discard("")
        self._issued: set[str] = set()
        self.collisions_avoided = 0

    @property
    def reserved(self) -> set[str]:
        return set(self._reserved)

    def is_available(self, name: str) -> bool:
        key = normalise_name(name)
        return bool(key) and key not in self._reserved and key not in self._issued

    def take(self) -> str:
        """A fresh name, guaranteed not to be a name the document uses."""
        for _ in range(500):
            candidate = (
                f"{self.rng.choice(self.first_names)} {self.rng.choice(self.surnames)}"
            )
            if self.is_available(candidate):
                self._issued.add(normalise_name(candidate))
                return candidate
            self.collisions_avoided += 1

        # Pool exhausted or heavily reserved: number rather than repeat.
        suffix = 2
        while True:
            candidate = (
                f"{self.rng.choice(self.first_names)} "
                f"{self.rng.choice(self.surnames)} {suffix}"
            )
            if self.is_available(candidate):
                self._issued.add(normalise_name(candidate))
                return candidate
            suffix += 1


# --------------------------------------------------------------------------
# The sketch
# --------------------------------------------------------------------------


class Stance(BaseModel):
    label: str
    description: str = ""
    weight: float = Field(default=1.0, ge=0.0)

    @field_validator("label")
    @classmethod
    def _require_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a stance needs a label")
        return value.strip()


class PopulationSketch(BaseModel):
    """Who this event affects, and how opinion divides among them."""

    setting: str = ""
    affected_groups: list[str] = Field(default_factory=list)
    stances: list[Stance] = Field(default_factory=list)
    min_age: int = 18
    max_age: int = 85
    notes: str = ""

    @field_validator("stances")
    @classmethod
    def _require_stances(cls, value: list[Stance]) -> list[Stance]:
        if not value:
            raise ValueError("a population needs at least one stance")
        return value

    @field_validator("min_age", "max_age", mode="before")
    @classmethod
    def _coerce_age(cls, value: Any) -> Any:
        from app.services.profile_generator import normalise_age

        try:
            return normalise_age(value)
        except (ValueError, TypeError):
            return 18 if value is None else value

    def weighted_stances(self) -> list[Stance]:
        return [s for s in self.stances if s.weight > 0] or list(self.stances)

    @classmethod
    def fallback(cls) -> "PopulationSketch":
        """Used when the model cannot produce a sketch.

        A generic crowd is a poor simulation, but it is a working one; failing
        the whole population because one call went wrong is worse.
        """
        return cls(
            setting="the local area affected by the event",
            affected_groups=["residents", "local workers", "business owners"],
            stances=[
                Stance(label="opposed", description="Against the change", weight=0.3),
                Stance(label="supportive", description="In favour", weight=0.3),
                Stance(label="conditional",
                       description="Supportive if specific concerns are met", weight=0.25),
                Stance(label="indifferent",
                       description="Largely unaffected and uninterested", weight=0.15),
            ],
        )


SKETCH_SYSTEM = """\
You describe the population affected by an event, for a social simulation.

Return:
- setting: one sentence on where and among whom this plays out.
- affected_groups: the kinds of ordinary people this touches. Think renters, \
homeowners, commuters, parents, shift workers, small traders, the retired — \
not job titles from the document.
- stances: the positions people actually take, with a weight for how common \
each is. Include indifference; most people do not care much about most things.
- min_age and max_age: the plausible adult age range.

Be specific to this document, not generic."""

SKETCH_USER = """\
Event domain: {domain}

Entity types in the source: {types}

Source excerpt:
{excerpt}

Describe the affected population."""


async def sketch_population(
    ontology: Ontology,
    excerpt: str,
    llm: LLMClient,
    *,
    temperature: float = 0.4,
) -> PopulationSketch:
    """Derive who the event affects, from the document."""
    messages = [
        {"role": "system", "content": SKETCH_SYSTEM},
        {"role": "user", "content": SKETCH_USER.format(
            domain=ontology.domain or "unspecified",
            types=", ".join(t.name for t in ontology.entity_types) or "none",
            excerpt=excerpt[:6000],
        )},
    ]
    try:
        return await llm.complete_json(messages, PopulationSketch, temperature=temperature)
    except LLMJSONError as exc:
        logger.warning("Population sketch failed (%s); using a generic crowd", exc)
        return PopulationSketch.fallback()


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


@dataclass
class PopulationPlan:
    """What will be generated, before anything is generated."""

    named: list[EntityContext] = field(default_factory=list)
    synthetic: list[EntityContext] = field(default_factory=list)
    sketch: PopulationSketch = field(default_factory=PopulationSketch.fallback)
    dropped_named: int = 0

    @property
    def total(self) -> int:
        return len(self.named) + len(self.synthetic)

    def summary(self) -> str:
        return (
            f"{self.total} agents: {len(self.named)} named, "
            f"{len(self.synthetic)} synthetic"
            + (f" ({self.dropped_named} named entities not used)"
               if self.dropped_named else "")
        )


def plan_population(
    named_contexts: Sequence[EntityContext],
    *,
    total: int,
    sketch: PopulationSketch,
    generator: ProfileGenerator,
    named_ratio: float = 0.25,
    rng: random.Random | None = None,
) -> PopulationPlan:
    """Decide who to generate, and with what assigned attributes.

    ``named_ratio`` caps the share of the population drawn from the document.
    A crowd that is one-third councillors is not the crowd; but if the document
    names fewer people than the cap allows, all of them are used.
    """
    if total < 1:
        raise ValueError("total must be at least 1")
    if not 0.0 <= named_ratio <= 1.0:
        raise ValueError("named_ratio must be between 0 and 1")

    rng = rng or generator.rng
    allowed = int(total * named_ratio)
    if named_contexts and allowed == 0:
        allowed = 1  # never silently discard the document's actors entirely
    named = list(named_contexts[:allowed])
    dropped = max(0, len(named_contexts) - len(named))

    synthetic_count = total - len(named)
    occupations = generator.sample_occupations(synthetic_count)
    allocator = NameAllocator(
        [c.name for c in named_contexts]
        + [c.assigned_name or c.name for c in named_contexts],
        rng=rng,
    )
    stances = sketch.weighted_stances()
    weights = [s.weight for s in stances]
    groups = sketch.affected_groups or ["local residents"]
    low, high = sorted((sketch.min_age, sketch.max_age))

    synthetic: list[EntityContext] = []
    for index in range(synthetic_count):
        stance = rng.choices(stances, weights=weights, k=1)[0]
        allocated = allocator.take()
        synthetic.append(EntityContext(
            uuid=f"synthetic-{index:05d}",
            name=allocated,
            # Both fields, deliberately: `name` is what the plan reads, and
            # `assigned_name` is what the generator enforces over whatever the
            # model returns. Setting only one leaves the model's chosen name in
            # place, which is the whole thing this module exists to prevent.
            assigned_name=allocated,
            type="Person",
            assigned_occupation=occupations[index],
            assigned_age=rng.randint(low, high),
            assigned_stance=f"{stance.label}: {stance.description}".strip(": "),
            group=rng.choice(groups),
            synthetic=True,
        ))

    return PopulationPlan(named=named, synthetic=synthetic, sketch=sketch,
                          dropped_named=dropped)


async def build_population(
    generator: ProfileGenerator,
    plan: PopulationPlan,
    *,
    temperature: float = 0.85,
) -> tuple[list[PersonaProfile], list[str]]:
    """Generate every persona in the plan, named and synthetic alike."""
    contexts = list(plan.named) + list(plan.synthetic)
    profiles, failures = await generator.generate_many(contexts, temperature=temperature)
    logger.info(
        "Population built: %d of %d requested (%d failed)",
        len(profiles), len(contexts), len(failures),
    )
    return profiles, failures


def provenance_counts(profiles: Iterable[PersonaProfile]) -> dict[str, int]:
    counts = {"named": 0, "synthetic": 0}
    for profile in profiles:
        counts[profile.provenance] = counts.get(profile.provenance, 0) + 1
    return counts


def assert_no_name_collisions(
    profiles: Sequence[PersonaProfile], reserved: Iterable[str]
) -> list[str]:
    """Names where a synthetic agent has taken a real person's name.

    Returns the offending names rather than raising, so a caller can decide
    whether to regenerate or refuse. An empty list is the required state.
    """
    reserved_keys = {normalise_name(name) for name in reserved} - {""}
    return [
        profile.name
        for profile in profiles
        if profile.provenance == "synthetic"
        and normalise_name(profile.name) in reserved_keys
    ]
