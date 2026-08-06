"""Generate a population as an interruptible, resumable job.

Profile generation is the second-most expensive stage in the system. Three
hundred agents is roughly half an hour of local inference, and losing that to a
container restart is the failure this module exists to prevent.

**Completed profiles are appended to JSONL as they land.** A kill mid-write
corrupts at most the final line, which resume discards; everything before it is
intact. Rewriting a JSON array after every profile would be quadratic in I/O
and, worse, a kill during a rewrite can truncate the file and lose the lot.

**The plan is written before generation starts, and resume reuses it.** Every
synthetic agent's name, occupation, age and stance is sampled randomly. Without
a persisted plan, a resumed run re-samples and produces a *different*
population from the one it was part-way through building — the same run would
not be reproducible across a restart. A resume that asks for a different size
is refused rather than silently reconciled: merging half of one population into
another is not a resume.

**Failures are simply not recorded as done**, so a resume retries them. Most
failures here are transient — Ollama restarting, a model reload timing out —
and a permanently unusable entity costs one agent, not the population.

Concurrency is bounded by a worker pool sized from `LLM_CONCURRENCY`. The
Ollama gate already bounds what is actually in flight; the pool exists so
progress arrives in completion order and three hundred coroutines are not
created at once.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from app.services.population import PopulationPlan, PopulationSketch
from app.services.profile_generator import EntityContext, PersonaProfile, ProfileGenerator

logger = logging.getLogger(__name__)

__all__ = [
    "GenerationResult",
    "PLAN_FILE",
    "PARTIAL_FILE",
    "generate_population",
    "load_plan",
    "plan_fingerprint",
    "read_partial",
    "save_plan",
]

PLAN_FILE = "plan.json"
PARTIAL_FILE = "profiles.partial.jsonl"


class PlanMismatch(ValueError):
    """A resume was attempted against a plan for a different population."""


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def plan_fingerprint(plan: PopulationPlan) -> str:
    """Identifies a plan by what it will actually generate.

    Names and assigned occupations, in order. Two plans for the same size but
    different sampled crowds are different populations, and resuming across
    them would splice two runs together.
    """
    digest = hashlib.sha256()
    for context in list(plan.named) + list(plan.synthetic):
        digest.update(
            f"{context.uuid}|{context.name}|{context.assigned_occupation or ''}|"
            f"{context.synthetic}\n".encode("utf-8")
        )
    return digest.hexdigest()[:16]


def save_plan(plan: PopulationPlan, directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / PLAN_FILE
    path.write_text(
        json.dumps(
            {
                "fingerprint": plan_fingerprint(plan),
                "total": plan.total,
                "dropped_named": plan.dropped_named,
                "sketch": plan.sketch.model_dump(),
                "named": [dataclasses.asdict(c) for c in plan.named],
                "synthetic": [dataclasses.asdict(c) for c in plan.synthetic],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load_plan(directory: str | Path) -> PopulationPlan | None:
    path = Path(directory) / PLAN_FILE
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return PopulationPlan(
        named=[EntityContext(**c) for c in data.get("named", [])],
        synthetic=[EntityContext(**c) for c in data.get("synthetic", [])],
        sketch=PopulationSketch.model_validate(data.get("sketch", {}))
        if data.get("sketch") else PopulationSketch.fallback(),
        dropped_named=data.get("dropped_named", 0),
    )


def read_partial(directory: str | Path) -> dict[int, PersonaProfile]:
    """Profiles already generated, keyed by their position in the plan.

    A line that will not parse is discarded rather than raising: after a kill
    the final line is routinely half-written, and refusing to resume because of
    it would throw away everything before it.
    """
    path = Path(directory) / PARTIAL_FILE
    if not path.is_file():
        return {}

    done: dict[int, PersonaProfile] = {}
    torn = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            profile = PersonaProfile.model_validate(record["profile"])
        except Exception:  # noqa: BLE001 - a torn or stale line, not a failure
            torn += 1
            continue
        done[int(record["index"])] = profile
    if torn:
        logger.warning(
            "Discarded %d unreadable line(s) from %s; the rest were resumed",
            torn, path.name,
        )
    return done


def _append_profile(handle: Any, index: int, profile: PersonaProfile) -> None:
    """One record, flushed immediately.

    Flushing per profile is what makes a kill lose at most the record in
    flight. Buffering would trade a real guarantee for I/O that is negligible
    next to the inference call that produced the record.
    """
    handle.write(json.dumps({"index": index, "profile": profile.model_dump()}) + "\n")
    handle.flush()


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


@dataclass
class GenerationResult:
    profiles: list[PersonaProfile] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    resumed: int = 0
    generated: int = 0
    elapsed: float = 0.0

    @property
    def total(self) -> int:
        return len(self.profiles)

    def summary(self) -> str:
        rate = f", {self.elapsed / self.generated:.1f}s each" if self.generated else ""
        return (
            f"{self.total} profiles ({self.resumed} resumed, {self.generated} "
            f"generated{rate}); {len(self.failures)} failed"
        )


ProgressHook = Callable[[int, int, str], None]


async def generate_population(
    generator: ProfileGenerator,
    plan: PopulationPlan,
    directory: str | Path,
    *,
    concurrency: int | None = None,
    temperature: float = 0.85,
    resume: bool = True,
    progress: ProgressHook | None = None,
) -> GenerationResult:
    """Generate every persona in ``plan``, resumably.

    ``progress`` is called as each profile lands, with ``(done, total, name)``.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    briefs: list[EntityContext] = list(plan.named) + list(plan.synthetic)
    if not briefs:
        raise ValueError("the plan contains no agents")

    existing = load_plan(directory)
    if existing is not None and resume:
        if plan_fingerprint(existing) != plan_fingerprint(plan):
            raise PlanMismatch(
                f"{directory / PLAN_FILE} describes a different population "
                f"({existing.total} agents) from the one requested "
                f"({plan.total}). Resuming across them would splice two runs "
                f"together. Delete the directory to start again."
            )
    save_plan(plan, directory)

    done = read_partial(directory) if resume else {}
    if not resume:
        (directory / PARTIAL_FILE).unlink(missing_ok=True)

    outstanding = [(i, b) for i, b in enumerate(briefs) if i not in done]
    result = GenerationResult(resumed=len(done))
    if done:
        logger.info("Resuming: %d of %d profiles already generated", len(done), len(briefs))

    total = len(briefs)
    completed = len(done)
    if progress:
        progress(completed, total, "resumed")

    if outstanding:
        workers = max(1, concurrency or generator.config.LLM_CONCURRENCY)
        queue: asyncio.Queue = asyncio.Queue()
        for item in outstanding:
            queue.put_nowait(item)

        lock = asyncio.Lock()
        started = time.monotonic()

        with (directory / PARTIAL_FILE).open("a", encoding="utf-8") as handle:

            async def worker() -> None:
                nonlocal completed
                while True:
                    try:
                        index, brief = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        profile = await generator.generate_for_entity(
                            brief, temperature=temperature
                        )
                    except Exception as exc:  # noqa: BLE001 - one agent, not the run
                        logger.warning("Persona failed for %s: %s", brief.name, exc)
                        async with lock:
                            result.failures.append(brief.name)
                        continue
                    async with lock:
                        # Persist before counting it done: a profile that was
                        # reported but not written would be lost on resume.
                        _append_profile(handle, index, profile)
                        done[index] = profile
                        completed += 1
                        result.generated += 1
                        if progress:
                            progress(completed, total, profile.name)

            await asyncio.gather(*(worker() for _ in range(min(workers, len(outstanding)))))

        result.elapsed = time.monotonic() - started

    result.profiles = [done[i] for i in sorted(done)]
    logger.info("Population generation complete: %s", result.summary())
    return result
