"""Phase 8 Step 2 — checking that a report is about the run it claims to be about.

A report that reads well and cites nothing is indistinguishable from the
model's prior assumptions about housing consultations, and would have been just
as fluent had the simulation never run. Everything here exists to tell those
apart, and it does it by the only means available: resolving every reference
against the run's own database.

**A claim whose evidence does not exist is dropped, not annotated.** An
unverified claim sitting in a report body still reads as a finding, which is
the confusion this step exists to prevent. It is removed and listed in the
verification record instead — silently deleting it would be its own dishonesty,
and a reader is owed the knowledge that the model asserted something it could
not support.

**Prose is checked too, because it is the part people read.** A citation object
can be validated by construction; the executive summary cannot, and nothing
stops a model writing "post 47 drove the backlash" there. References in free
text are found and resolved, and the ones that do not exist are reported.

**Not citing and citing wrongly are different failures.** A claim with no
citation is unsupported — the model did not show its working. A claim citing a
post that does not exist is *fabricated* evidence, which is worse, and the two
are counted separately because they say different things about a run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from app.services.run_reader import RunReader

logger = logging.getLogger(__name__)

__all__ = [
    "GroundedReport",
    "Grounding",
    "RunFacts",
    "check_report",
    "extract_references",
]

#: "post 12", "post #12", "posts 3 and 4", "post_id 7"
_POST_RE = re.compile(r"\bposts?[\s_#]*(?:id)?[\s#]*(\d+)", re.IGNORECASE)
#: "agent 4", "user 4", "agent #4", "user_id 4"
_AGENT_RE = re.compile(r"\b(?:agents?|users?)[\s_#]*(?:id)?[\s#]*(\d+)", re.IGNORECASE)
#: "round 3", "in round 3"
_ROUND_RE = re.compile(r"\brounds?[\s_#]*(\d+)", re.IGNORECASE)
#: "@dawn_mercer"
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{2,32})")


@dataclass
class RunFacts:
    """Everything a citation could refer to, read once from the run."""

    post_ids: set[int] = field(default_factory=set)
    agent_ids: set[int] = field(default_factory=set)
    rounds: set[int] = field(default_factory=set)
    usernames: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, sim_dir: str | Path) -> "RunFacts":
        reader = RunReader(sim_dir)
        if not reader.exists:
            return cls()

        identities = reader.identities()
        facts = cls(
            agent_ids={int(i) for i in identities},
            usernames={i.username.lower() for i in identities.values() if i.username},
            rounds={r["round"] for r in reader.timeline()},
        )
        page = reader.posts(limit=500, order="oldest")
        facts.post_ids = {int(p["post_id"]) for p in page["posts"]}
        # A run can hold more posts than one page; the ids are contiguous
        # rowids, so pull the rest rather than judging citations against a
        # window and calling the remainder fabricated.
        offset = len(page["posts"])
        while offset < page["total"]:
            more = reader.posts(limit=500, offset=offset, order="oldest")
            if not more["posts"]:
                break
            facts.post_ids.update(int(p["post_id"]) for p in more["posts"])
            offset += len(more["posts"])
        return facts

    @property
    def empty(self) -> bool:
        return not (self.post_ids or self.agent_ids or self.rounds)


class Reference(BaseModel):
    """One thing a report pointed at, and whether it exists."""

    kind: str
    value: str
    where: str
    resolved: bool


class DroppedClaim(BaseModel):
    """A finding removed because its evidence did not exist."""

    section: str
    claim: str
    reason: str


class Grounding(BaseModel):
    """The verification record. Published with the report, not beside it."""

    checked: int = 0
    resolved: int = 0
    unresolved: list[Reference] = Field(default_factory=list)
    uncited_claims: list[str] = Field(default_factory=list)
    dropped: list[DroppedClaim] = Field(default_factory=list)
    prose_references: int = 0
    prose_unresolved: list[Reference] = Field(default_factory=list)

    @property
    def grounded(self) -> bool:
        """True when nothing in the report pointed at something that is not there."""
        return not self.unresolved and not self.prose_unresolved

    def summary(self) -> str:
        if self.empty_run:
            return "The run holds no data, so nothing could be verified."
        parts = [f"{self.resolved}/{self.checked} citation(s) resolved"]
        if self.dropped:
            parts.append(f"{len(self.dropped)} claim(s) dropped as unsupported")
        if self.uncited_claims:
            parts.append(f"{len(self.uncited_claims)} claim(s) cited nothing")
        if self.prose_unresolved:
            parts.append(
                f"{len(self.prose_unresolved)} reference(s) in prose do not exist")
        return "; ".join(parts)

    empty_run: bool = False


class GroundedReport(BaseModel):
    """A report and its verification, kept together."""

    report: dict[str, Any]
    grounding: Grounding


# --------------------------------------------------------------------------
# Prose
# --------------------------------------------------------------------------


def extract_references(text: str) -> list[tuple[str, str]]:
    """Find things in free text that look like references to the run.

    Deliberately conservative: it matches the forms a report actually uses
    ("post 12", "agent 4", "round 3", "@dawn_mercer") rather than every bare
    number, because reading "four-storey" as a citation would produce noise
    that drowns the real findings.
    """
    if not text:
        return []
    found: list[tuple[str, str]] = []
    for pattern, kind in ((_POST_RE, "post"), (_AGENT_RE, "agent"),
                          (_ROUND_RE, "round")):
        found.extend((kind, match.group(1)) for match in pattern.finditer(text))
    found.extend(("username", match.group(1)) for match in _HANDLE_RE.finditer(text))
    return found


def _resolve(kind: str, value: str, facts: RunFacts) -> bool:
    if kind == "username":
        return value.lower() in facts.usernames
    try:
        number = int(value)
    except ValueError:
        return False
    if kind == "post":
        return number in facts.post_ids
    if kind == "agent":
        return number in facts.agent_ids
    if kind == "round":
        return number in facts.rounds
    return False


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------


def _check_citation(citation: Any, facts: RunFacts, where: str,
                    grounding: Grounding) -> list[str]:
    """Resolve one citation, returning the reasons it failed."""
    failures: list[str] = []
    for kind, values in (("post", getattr(citation, "post_ids", []) or []),
                         ("agent", getattr(citation, "agent_ids", []) or []),
                         ("round", getattr(citation, "rounds", []) or [])):
        for value in values:
            grounding.checked += 1
            if _resolve(kind, str(value), facts):
                grounding.resolved += 1
                continue
            grounding.unresolved.append(Reference(
                kind=kind, value=str(value), where=where, resolved=False))
            failures.append(f"{kind} {value} does not exist in this run")
    return failures


def check_report(report: Any, sim_dir: str | Path, *,
                 facts: RunFacts | None = None) -> tuple[Any, Grounding]:
    """Verify a report against its run, dropping claims that cannot be supported.

    Returns the pruned report and the verification record. The report object is
    modified in place — callers hold the same instance the agent produced.
    """
    facts = facts if facts is not None else RunFacts.load(sim_dir)
    grounding = Grounding(empty_run=facts.empty)

    if facts.empty:
        logger.warning("No run data to verify %s against", Path(sim_dir).name)
        return report, grounding

    _prune(report, "dominant_narratives", "label", facts, grounding)
    _prune(report, "counter_narratives", "label", facts, grounding)
    _prune(report, "influential_agents", "username", facts, grounding)
    _prune(report, "emergent_behaviour", "claim", facts, grounding)

    for field_name in ("executive_summary", "sentiment_reading",
                       "influence_propagation"):
        text = getattr(report, field_name, "") or ""
        for kind, value in extract_references(text):
            grounding.prose_references += 1
            if not _resolve(kind, value, facts):
                grounding.prose_unresolved.append(Reference(
                    kind=kind, value=value, where=field_name, resolved=False))

    logger.info("Grounding for %s: %s", Path(sim_dir).name, grounding.summary())
    return report, grounding


def _prune(report: Any, section: str, label_field: str, facts: RunFacts,
           grounding: Grounding) -> None:
    """Drop the findings in one section whose evidence does not resolve."""
    items = getattr(report, section, None)
    if not items:
        return

    kept = []
    for item in items:
        label = str(getattr(item, label_field, "") or getattr(item, "claim", ""))
        where = f"{section}[{label[:60]}]"
        citation = getattr(item, "citation", None)

        if citation is None or getattr(citation, "empty", True):
            # Unsupported, not fabricated: the model did not show its working.
            # Kept, because nothing about it is false — but recorded, because a
            # reader should know which findings rest on nothing.
            grounding.uncited_claims.append(where)
            kept.append(item)
            continue

        failures = _check_citation(citation, facts, where, grounding)
        if failures:
            grounding.dropped.append(DroppedClaim(
                section=section, claim=label,
                reason="; ".join(failures[:3])))
            continue
        kept.append(item)

    setattr(report, section, kept)


def unresolved_by_section(grounding: Grounding) -> dict[str, int]:
    """Where the fabricated references were, for a report's own verification note."""
    out: dict[str, int] = {}
    for reference in list(grounding.unresolved) + list(grounding.prose_unresolved):
        key = reference.where.split("[")[0]
        out[key] = out.get(key, 0) + 1
    return out
