"""Derive the scenario a simulation runs: the event, the seeds, the schedule.

**OASIS forces the attribution question.** `env.step()` takes
`dict[SocialAgent, ManualAction]` — every post comes from an agent, and there
is no platform-level injection. So a seed post introducing the event must be
attributed to somebody, and attributing an invented statement to a real named
person is the fabrication problem Phase 4 exists to prevent.

Two attributions are therefore allowed, and only two:

* **broadcaster** — a clearly synthetic account, invented for the run, whose
  name is checked against every entity the graph holds. Anything the model
  writes goes here.
* **named_quote** — a line the document *actually contains*, posted by the
  agent for the person who said it. Reproducing what someone genuinely said is
  not fabrication. This is only allowed when the text is found verbatim in the
  source, and the config records the offsets so the quote stays checkable.

A quote the generator claims but cannot locate is **demoted to the broadcaster
rather than dropped or trusted**. The model paraphrases constantly; a
paraphrase attributed to a real person is exactly the thing to avoid, and
silently believing it would be worse than losing the post.

**Scheduled mid-run events are counterfactual and disabled by default.** A
leaked report or a rebuttal that never happened changes what the run measures,
and a reader of the resulting report has no reason to suspect it. They are
generated, marked, and left off until an operator turns them on in review.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import Config, get_config
from app.services.ontology_generator import Ontology
from app.storage.ner_extractor import normalise_name
from app.utils.llm_client import LLMClient, LLMJSONError

logger = logging.getLogger(__name__)

__all__ = [
    "Broadcaster",
    "ScenarioError",
    "ScheduledEvent",
    "SeedPost",
    "SimulationConfig",
    "SimulationConfigGenerator",
    "find_verbatim",
]

Platform = Literal["twitter", "reddit"]
Attribution = Literal["broadcaster", "named_quote"]

MIN_ROUNDS = 1
MAX_HOURS_PER_ROUND = 168.0  # a week; beyond this "rounds" stop meaning anything

_WHITESPACE = re.compile(r"\s+")


class ScenarioError(RuntimeError):
    """A usable scenario could not be derived."""


# --------------------------------------------------------------------------
# Verbatim matching
# --------------------------------------------------------------------------


def find_verbatim(document: str, quote: str) -> tuple[int, int] | None:
    """Locate ``quote`` in ``document``, ignoring whitespace and case.

    Returns offsets into the *original* document so a reviewer can check the
    quote against the source. Whitespace is normalised on both sides because a
    model reproducing a line across a chunk boundary will collapse a newline,
    and that is not a paraphrase.

    Returns ``None`` when the text is not there — which is the answer that
    matters, since it is what stops a paraphrase being attributed to a real
    person.
    """
    quote = quote.strip().strip('"“”\'')
    if len(quote) < 12:
        # Too short to be evidence of anything: "the plan" appears everywhere.
        return None

    positions: list[int] = []
    flattened: list[str] = []
    previous_space = False
    for index, character in enumerate(document):
        if character.isspace():
            if previous_space or not flattened:
                continue
            flattened.append(" ")
            positions.append(index)
            previous_space = True
        else:
            flattened.append(character.lower())
            positions.append(index)
            previous_space = False

    haystack = "".join(flattened)
    needle = _WHITESPACE.sub(" ", quote).strip().lower()
    found = haystack.find(needle)
    if found == -1:
        return None
    start = positions[found]
    end = positions[min(found + len(needle) - 1, len(positions) - 1)] + 1
    return start, end


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


class Broadcaster(BaseModel):
    """The synthetic account that introduces the event."""

    name: str = Field(description="A plausible local outlet or account name")
    handle: str = ""
    description: str = ""

    @field_validator("name")
    @classmethod
    def _require_name(cls, value: str) -> str:
        # Models put the "@" in the display name as often as in the handle.
        # The handle carries it; the name is what a reader sees.
        cleaned = value.strip().lstrip("@").strip()
        if not cleaned:
            raise ValueError("the broadcaster needs a name")
        return cleaned

    @model_validator(mode="after")
    def _normalise_handle(self) -> "Broadcaster":
        """Always normalise, never merely fill.

        The model returns handles already carrying an "@", and a display layer
        that prepends its own produces "@@RB_Echo" — observed on the first real
        run.
        """
        source = self.handle or self.name
        slug = re.sub(r"[^a-z0-9]+", "_", source.lower()).strip("_")
        object.__setattr__(self, "handle", slug[:24] or "wire")
        return self


class SeedPost(BaseModel):
    """A post that introduces the event, at round zero."""

    content: str
    attribution: Attribution = "broadcaster"
    #: Set only for ``named_quote``: whose words these are.
    speaker: str = ""
    #: Offsets into the stored document, proving the quote is real.
    source_start: int | None = None
    source_end: int | None = None
    #: Recorded when a claimed quote could not be found and was reassigned.
    demoted_reason: str = ""

    @field_validator("content")
    @classmethod
    def _require_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a seed post needs content")
        return value.strip()

    @property
    def verified(self) -> bool:
        return self.attribution == "named_quote" and self.source_start is not None


class ScheduledEvent(BaseModel):
    """Something injected mid-run that did not happen in the source."""

    round: int = Field(ge=1)
    description: str
    content: str
    #: Always true for generated events. Present so the flag is explicit in the
    #: written config rather than implied by which list it sits in.
    counterfactual: bool = True
    #: Off until an operator turns it on. A baseline run reflects the document.
    enabled: bool = False

    @field_validator("description", "content")
    @classmethod
    def _require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class SimulationConfig(BaseModel):
    """Everything the engine needs to start a run, and a human to review one."""

    graph_id: str = ""
    platform: Platform = "twitter"
    event: str = Field(description="What the population is reacting to")
    rounds: int = Field(default=10, ge=MIN_ROUNDS)
    start_time: str = ""
    hours_per_round: float = Field(default=6.0, gt=0, le=MAX_HOURS_PER_ROUND)
    broadcaster: Broadcaster
    seed_posts: list[SeedPost] = Field(default_factory=list)
    scheduled_events: list[ScheduledEvent] = Field(default_factory=list)
    notes: str = ""

    @field_validator("event")
    @classmethod
    def _require_event(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("the scenario needs a triggering event")
        return value.strip()

    @field_validator("seed_posts")
    @classmethod
    def _require_seeds(cls, value: list[SeedPost]) -> list[SeedPost]:
        if not value:
            raise ValueError(
                "a simulation needs at least one seed post; agents have nothing "
                "to react to otherwise"
            )
        return value

    @model_validator(mode="after")
    def _events_fall_inside_the_run(self) -> "SimulationConfig":
        """An event scheduled past the last round simply never fires."""
        kept = [e for e in self.scheduled_events if e.round <= self.rounds]
        dropped = len(self.scheduled_events) - len(kept)
        if dropped:
            logger.warning(
                "Dropped %d scheduled event(s) past round %d", dropped, self.rounds
            )
        object.__setattr__(self, "scheduled_events", kept)
        if not self.start_time:
            object.__setattr__(
                self, "start_time", datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
        return self

    # -- derived ------------------------------------------------------------

    def round_time(self, index: int) -> str:
        """Wall-clock time the simulation pretends round ``index`` happens at."""
        try:
            start = datetime.fromisoformat(self.start_time)
        except ValueError:
            start = datetime.now(timezone.utc)
        return (start + timedelta(hours=self.hours_per_round * index)).isoformat(
            timespec="seconds"
        )

    def enabled_events(self) -> list[ScheduledEvent]:
        return [e for e in self.scheduled_events if e.enabled]

    def events_for_round(self, index: int) -> list[ScheduledEvent]:
        return [e for e in self.enabled_events() if e.round == index]

    def summary(self) -> str:
        verified = sum(1 for p in self.seed_posts if p.verified)
        enabled = len(self.enabled_events())
        return (
            f"{self.platform}, {self.rounds} rounds x {self.hours_per_round}h; "
            f"{len(self.seed_posts)} seed post(s) ({verified} verified quote(s)); "
            f"{len(self.scheduled_events)} scheduled event(s), {enabled} enabled"
        )

    # -- persistence --------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "SimulationConfig":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You design scenarios for a social simulation. Given a source document, you \
describe the event a population is reacting to and write the posts that \
introduce it.

Rules:
- event: one paragraph stating what happened, in the document's own terms.
- broadcaster: invent a plausible local news account that will post the \
announcements. It must NOT be any organisation the document names.
- seed_posts: the posts that open the simulation. Use attribution \
"broadcaster" for anything you write yourself. Use "named_quote" ONLY when the \
content is a sentence copied word-for-word from the document, and set speaker \
to the person who said it. Do not paraphrase and call it a quote.
- scheduled_events: things that could plausibly happen next but have NOT \
happened — a follow-up announcement, a rebuttal, a leak. Give each a round \
number within the run. These are hypotheticals, not facts.
- hours_per_round: how much time one round represents.

Write posts as people actually post: short, specific, in the register of the \
platform."""

USER_PROMPT = """\
Platform: {platform}
Rounds available: {rounds}
Domain: {domain}

People and organisations the document names (do not invent quotes for them):
{named}

SOURCE DOCUMENT
{document}

Design the scenario."""


class SimulationConfigGenerator:
    """Derives a runnable scenario from a document and its graph."""

    def __init__(
        self, config: Config | None = None, *, llm: LLMClient | None = None
    ) -> None:
        self.config = config or get_config()
        self.llm = llm or LLMClient(self.config)

    async def generate(
        self,
        document: str,
        *,
        ontology: Ontology | None = None,
        named_entities: Sequence[str] = (),
        graph_id: str = "",
        platform: Platform = "twitter",
        rounds: int | None = None,
        temperature: float = 0.5,
        excerpt_limit: int = 8000,
    ) -> SimulationConfig:
        """Propose a scenario, then verify everything it claims about the source."""
        if not document.strip():
            raise ScenarioError("Cannot derive a scenario from an empty document.")

        rounds = min(rounds or self.config.MAX_ROUNDS, self.config.MAX_ROUNDS)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT.format(
                platform=platform,
                rounds=rounds,
                domain=(ontology.domain if ontology else "") or "unspecified",
                named=", ".join(named_entities) or "(none identified)",
                document=document[:excerpt_limit],
            )},
        ]
        try:
            config = await self.llm.complete_json(
                messages, SimulationConfig, temperature=temperature
            )
        except LLMJSONError as exc:
            raise ScenarioError(f"Scenario generation failed: {exc}") from exc

        config.graph_id = graph_id
        config.platform = platform
        config.rounds = min(config.rounds, rounds)
        self._verify(config, document, named_entities)
        logger.info("Scenario derived: %s", config.summary())
        return config

    def _verify(
        self, config: SimulationConfig, document: str, named_entities: Sequence[str]
    ) -> None:
        """Check every claim the model made about the source.

        Two things are checked because both are ways a real person can end up
        misrepresented: the broadcaster must not be an organisation the
        document names, and a claimed quote must actually be in the text.
        """
        reserved = {normalise_name(name) for name in named_entities} - {""}
        if normalise_name(config.broadcaster.name) in reserved:
            original = config.broadcaster.name
            config.broadcaster.name = f"The {original} Wire"
            config.broadcaster.handle = ""
            Broadcaster.model_validate(config.broadcaster.model_dump())
            logger.warning(
                "Broadcaster %r collided with a named entity; renamed to %r",
                original, config.broadcaster.name,
            )

        known = {normalise_name(name) for name in named_entities} - {""}
        for post in config.seed_posts:
            if post.attribution != "named_quote":
                post.speaker = ""
                post.source_start = post.source_end = None
                continue

            speaker = normalise_name(post.speaker)
            if not speaker or speaker not in known:
                self._demote(post, "speaker is not an entity the document names")
                continue

            span = find_verbatim(document, post.content)
            if span is None:
                self._demote(post, "the quoted text is not in the source document")
                continue
            post.source_start, post.source_end = span

    @staticmethod
    def _demote(post: SeedPost, reason: str) -> None:
        """Reassign an unverifiable quote to the broadcaster.

        Not dropped: the content is still a reasonable way to introduce the
        event. Not trusted: a paraphrase attributed to a real person is the
        thing this whole design is avoiding.
        """
        logger.warning(
            "Seed post attributed to %r demoted to the broadcaster: %s",
            post.speaker, reason,
        )
        post.demoted_reason = reason
        post.attribution = "broadcaster"
        post.speaker = ""
        post.source_start = post.source_end = None

    async def aclose(self) -> None:
        await self.llm.aclose()
