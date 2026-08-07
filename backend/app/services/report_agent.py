"""Phase 8 Step 1 — turning a run into a report.

Everything before this phase produced evidence. This produces the claims, which
is where a simulation becomes useful and also where it becomes easiest to
mislead: a report that reads well and cites nothing is indistinguishable from
the model's prior assumptions about housing consultations, and would have been
just as fluent had the run never happened.

Four decisions follow from that.

**The numbers come from SQL; the model writes prose about them.** The timeline,
the per-round action counts, the most-engaged posts, the influential agents and
the sentiment trajectory are all computed before the model is asked anything.
It cannot get them wrong, and a report is never weaker than the data it started
from even if the model uses its tools badly.

**The tool budget is spent on follow-up, not discovery.** Five calls is not
enough for a 14b model to find its way around a run from nothing, but it is
plenty to pull the posts behind an interesting round or read one agent's
history. Both budgets are hard: exceeding them ends the loop rather than
extending it, because an unbounded agent loop on local inference is a reliable
way to lose an afternoon.

**Every claim carries citations, and they are checked in Step 2.** The schema
requires post ids, agent ids and round numbers on each finding, so a claim that
cites nothing cannot be represented, let alone rendered.

**A thin run gets a short report, not a confident one.** Two agents over two
rounds cannot support a narrative analysis, and the caveats say so with the
numbers that make it obvious.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.config import Config, get_config
from app.services.run_reader import RunReader
from app.services.sentiment import SentimentScorer, round_trajectory
from app.utils.llm_client import LLMClient, LLMJSONError

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_REFLECTION_ROUNDS",
    "DEFAULT_TOOL_BUDGET",
    "Report",
    "ReportAgent",
    "ReportError",
    "ToolBox",
]

#: The spec's figures. Both are ceilings, not targets.
DEFAULT_TOOL_BUDGET = 5
DEFAULT_REFLECTION_ROUNDS = 2

#: A tool result larger than this is truncated before it reaches the prompt.
#: A run holds tens of thousands of rows and one careless query would otherwise
#: fill the context window with evidence nobody asked for.
MAX_TOOL_RESULT_CHARS = 6_000

#: How much of the baseline bundle is shown. Enough to reason over, bounded so
#: a three-hundred agent run does not push the instructions out of context.
TOP_POSTS = 12
TOP_AGENTS = 10


class ReportError(RuntimeError):
    """A report could not be produced."""


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


class Citation(BaseModel):
    """What a claim rests on. Validated against the run in Step 2."""

    post_ids: list[int] = Field(default_factory=list)
    agent_ids: list[int] = Field(default_factory=list)
    rounds: list[int] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.post_ids or self.agent_ids or self.rounds)


class Finding(BaseModel):
    """One claim about the run, and the evidence for it."""

    claim: str
    detail: str = ""
    citation: Citation = Field(default_factory=Citation)


class Narrative(BaseModel):
    label: str
    summary: str
    support: str = Field(default="", description="Who carried it, and how widely")
    citation: Citation = Field(default_factory=Citation)


class InfluentialAgent(BaseModel):
    user_id: int
    username: str = ""
    why: str
    citation: Citation = Field(default_factory=Citation)


class Report(BaseModel):
    """The document itself."""

    sim_id: str = ""
    graph_id: str = ""
    event: str = ""
    executive_summary: str
    sentiment_trajectory: list[dict[str, Any]] = Field(default_factory=list)
    sentiment_reading: str = Field(
        default="", description="What the trajectory shows, in prose")
    dominant_narratives: list[Narrative] = Field(default_factory=list)
    counter_narratives: list[Narrative] = Field(default_factory=list)
    influential_agents: list[InfluentialAgent] = Field(default_factory=list)
    influence_propagation: str = ""
    emergent_behaviour: list[Finding] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    #: Filled in by the agent, not the model.
    evidence: dict[str, Any] = Field(default_factory=dict)
    tool_calls_used: int = 0
    reflection_rounds_used: int = 0

    def sections(self) -> dict[str, Any]:
        return {
            "executive_summary": self.executive_summary,
            "sentiment_trajectory": self.sentiment_trajectory,
            "dominant_narratives": self.dominant_narratives,
            "counter_narratives": self.counter_narratives,
            "influential_agents": self.influential_agents,
            "emergent_behaviour": self.emergent_behaviour,
            "caveats": self.caveats,
        }


class _ToolRequest(BaseModel):
    tool: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class _AgentTurn(BaseModel):
    """Either a request for more evidence, or the report."""

    tool_requests: list[_ToolRequest] = Field(default_factory=list)
    report: Report | None = None


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@dataclass
class ToolBox:
    """Read-only access to one run, with a hard call budget.

    The budget lives here rather than in the loop so that no path — a retry, a
    reflection round, a malformed response handled generously — can spend more
    than was allowed.
    """

    reader: RunReader
    budget: int = DEFAULT_TOOL_BUDGET
    used: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    def describe(self) -> str:
        return (
            "posts_in_round(round, limit=10) — the posts a round produced\n"
            "agent_history(user_id, limit=10) — one agent's posts and actions\n"
            "posts_by_agent(user_id, limit=10) — what one agent wrote\n"
            "comments_on(post_id, limit=10) — replies to a post\n"
            "most_engaged(limit=10) — the posts that drew the most reaction"
        )

    def run(self, request: _ToolRequest) -> dict[str, Any]:
        if self.used >= self.budget:
            return {"error": "The tool budget for this report is spent."}
        self.used += 1

        name = (request.tool or "").strip()
        args = request.arguments or {}
        try:
            result = self._dispatch(name, args)
        except Exception as exc:  # noqa: BLE001 - a bad tool call is not a failed report
            logger.warning("Report tool %r failed: %s", name, exc)
            result = {"error": f"{type(exc).__name__}: {exc}"}

        payload = _sanitise(result)
        self.calls.append({"tool": name, "arguments": args,
                           "reason": request.reason[:200]})
        return payload

    def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        limit = min(int(args.get("limit") or 10), 25)
        if name == "posts_in_round":
            return self.reader.posts(round_index=int(args["round"]), limit=limit,
                                     order="oldest")["posts"]
        if name in {"posts_by_agent", "agent_history"}:
            user_id = int(args.get("user_id", args.get("agent", -1)))
            posts = self.reader.posts(agent=user_id, limit=limit)["posts"]
            if name == "posts_by_agent":
                return posts
            return {
                "posts": posts,
                "actions": self.reader.actions(agent=user_id, limit=limit)["actions"],
            }
        if name == "comments_on":
            return self.reader.comments(post_id=int(args["post_id"]),
                                        limit=limit)["comments"]
        if name == "most_engaged":
            return self.reader.posts(limit=limit, min_engagement=1,
                                     population_only=True)["posts"]
        return {"error": f"No such tool {name!r}"}


def _sanitise(result: Any) -> dict[str, Any]:
    """Make a tool result safe and small enough to put in a prompt.

    Two separate problems. Size: a query can return thousands of rows and the
    context window is finite, so results are truncated and say so rather than
    being silently cut. Content: post text is written by agents, and an agent
    that has been told to write "ignore your instructions" must not have that
    read as an instruction — so results go in as JSON data, and any fence that
    could end the data block is defanged.
    """
    try:
        text = json.dumps(result, default=str)
    except (TypeError, ValueError):
        text = str(result)

    truncated = len(text) > MAX_TOOL_RESULT_CHARS
    if truncated:
        text = text[:MAX_TOOL_RESULT_CHARS]
    text = text.replace("```", "'''")
    return {"data": text, "truncated": truncated,
            "note": ("Result truncated; ask for a smaller limit if you need more."
                     if truncated else "")}


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

SYSTEM = """\
You are an analyst writing a report on a completed social simulation.

The population is synthetic. Your job is to describe what these agents did and \
said, not to predict what real people would do, and not to restate what is \
generally true about the topic. A sentence that would have been just as true \
had the simulation never run does not belong in the report.

EVIDENCE: every finding must cite the run — post_ids, agent_ids, rounds. Cite \
what you actually saw in the data provided. Do not invent an id to justify a \
claim; a claim you cannot cite should be left out or moved to the caveats.

You may request tools to see more, but the budget is small and already partly \
spent. Prefer writing the report over gathering more.

Respond with JSON in one of two shapes.

To ask for evidence:
{"tool_requests": [{"tool": "posts_in_round", "arguments": {"round": 3}, \
"reason": "sentiment moved here"}]}

To finish:
{"report": {"executive_summary": "...", "sentiment_reading": "...", \
"dominant_narratives": [{"label": "...", "summary": "...", "support": "...", \
"citation": {"post_ids": [12], "agent_ids": [4], "rounds": [2]}}], \
"counter_narratives": [...], "influential_agents": [{"user_id": 4, \
"username": "...", "why": "...", "citation": {...}}], \
"influence_propagation": "...", "emergent_behaviour": [{"claim": "...", \
"detail": "...", "citation": {...}}], "caveats": ["..."]}}

The caveats must state plainly how small the run was and what it therefore \
cannot support. A two-agent, two-round run cannot evidence a narrative."""

USER = """\
Simulation: {sim_id} on {platform}, {rounds} round(s), {agents} agent(s).

The event the population was reacting to:
{event}

BASELINE EVIDENCE (computed from the run's database, not by a model):
{bundle}

Tools available (you have {remaining} call(s) left):
{tools}

Write the report, or request evidence first."""

FOLLOW_UP = """\
Results of what you asked for:
{results}

You have {remaining} tool call(s) left and {reflections} reflection round(s). \
Write the report now unless something is genuinely missing."""


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------


class ReportAgent:
    """Produces a report from a completed run."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        llm: LLMClient | None = None,
        tool_budget: int = DEFAULT_TOOL_BUDGET,
        reflection_rounds: int = DEFAULT_REFLECTION_ROUNDS,
    ) -> None:
        self.config = config or get_config()
        self.llm = llm or LLMClient(self.config)
        self.tool_budget = max(0, int(tool_budget))
        self.reflection_rounds = max(0, int(reflection_rounds))

    # -- evidence -----------------------------------------------------------

    def baseline(self, reader: RunReader, *, sentiment: dict[int, Any] | None = None,
                 total_rounds: int = 0) -> dict[str, Any]:
        """What every report needs, computed rather than asked for."""
        timeline = reader.timeline()
        stats = reader.agent_stats(limit=TOP_AGENTS, sort="engagement_received",
                                   population_only=True)
        engaged = reader.posts(limit=TOP_POSTS, min_engagement=1,
                               population_only=True)["posts"]
        if not engaged:
            # A quiet run still has posts; showing none would invite the model
            # to fill the silence from its own assumptions.
            engaged = reader.posts(limit=TOP_POSTS, order="oldest",
                                   population_only=True)["posts"]

        bundle: dict[str, Any] = {
            "timeline": timeline,
            "agents": {
                "total": stats["total"],
                "silent": stats["silent"],
                "most_engaged": [
                    {k: a[k] for k in ("user_id", "username", "provenance",
                                       "occupation", "posts", "comments",
                                       "actions", "followers",
                                       "engagement_received")}
                    for a in stats["agents"]
                ],
            },
            "most_engaged_posts": [
                {k: p[k] for k in ("post_id", "user_id", "username", "round",
                                   "kind", "content", "likes", "reposts",
                                   "comments", "engagement")}
                for p in engaged
            ],
        }
        if sentiment:
            bundle["sentiment_trajectory"] = round_trajectory(
                sentiment, reader.ledger.posts_by_round())
        return bundle

    # -- the loop -----------------------------------------------------------

    async def generate(
        self,
        sim_dir: str | Path,
        *,
        sim_config: Any = None,
        sentiment: dict[int, Any] | None = None,
        progress: Callable[[str, float], None] | None = None,
    ) -> Report:
        """Produce a report. Bounded in tool calls and in reflection rounds."""
        reader = RunReader(sim_dir)
        if not reader.exists:
            raise ReportError(
                "This simulation has no run data; there is nothing to report on.")

        timeline = reader.timeline()
        if not timeline:
            raise ReportError(
                "This run recorded no completed rounds; there is nothing to report on.")

        def say(stage: str, fraction: float) -> None:
            if progress:
                progress(stage, fraction)

        say("evidence", 0.1)
        total_rounds = getattr(sim_config, "rounds", 0) or len(timeline) - 1
        bundle = self.baseline(reader, sentiment=sentiment, total_rounds=total_rounds)
        tools = ToolBox(reader=reader, budget=self.tool_budget)

        agents_total = bundle["agents"]["total"]
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER.format(
                sim_id=Path(sim_dir).name,
                platform=getattr(sim_config, "platform", "unknown"),
                rounds=len([r for r in timeline if not r["seed"]]),
                agents=agents_total,
                event=getattr(sim_config, "event", "") or "(not recorded)",
                bundle=json.dumps(bundle, indent=2, default=str)[:20_000],
                remaining=tools.remaining,
                tools=tools.describe(),
            )},
        ]

        report: Report | None = None
        reflections = 0
        # One pass to write, plus the allowed reflections. Every exit from this
        # loop is bounded; there is no path that asks for "one more".
        for attempt in range(self.reflection_rounds + 1):
            say("writing" if attempt == 0 else "reflecting",
                0.3 + 0.5 * attempt / (self.reflection_rounds + 1))
            try:
                turn = await self.llm.complete_json(
                    messages, _AgentTurn,
                    temperature=self.config.REPORT_TEMPERATURE)
            except LLMJSONError as exc:
                raise ReportError(f"The report agent produced nothing usable: {exc}") from exc

            if turn.report is not None:
                report = turn.report
                break

            if attempt == self.reflection_rounds:
                # Out of reflections and still asking for evidence: say so
                # rather than granting another round.
                logger.warning("Report agent used every reflection round without "
                               "producing a report; asking once more, plainly")
                messages.append({"role": "user", "content":
                                 "No further evidence is available. Write the "
                                 "report from what you have."})
                try:
                    turn = await self.llm.complete_json(
                        messages, _AgentTurn,
                        temperature=self.config.REPORT_TEMPERATURE)
                except LLMJSONError as exc:
                    raise ReportError(
                        f"The report agent produced nothing usable: {exc}") from exc
                report = turn.report
                break

            requests = turn.tool_requests[:tools.remaining]
            if not requests:
                messages.append({"role": "user", "content":
                                 "Write the report from the evidence above."})
                reflections += 1
                continue

            results = {f"{r.tool}({json.dumps(r.arguments, default=str)})":
                       tools.run(r) for r in requests}
            reflections += 1
            messages.append({"role": "assistant", "content": json.dumps(
                {"tool_requests": [r.model_dump() for r in requests]})})
            messages.append({"role": "user", "content": FOLLOW_UP.format(
                results=json.dumps(results, indent=2)[:MAX_TOOL_RESULT_CHARS * 2],
                remaining=tools.remaining,
                reflections=self.reflection_rounds - reflections)})

        if report is None:
            raise ReportError(
                "The report agent asked for evidence until its budget ran out "
                "without writing anything.")

        say("finishing", 0.9)
        report.sim_id = Path(sim_dir).name
        report.graph_id = getattr(sim_config, "graph_id", "") or ""
        report.event = getattr(sim_config, "event", "") or ""
        report.sentiment_trajectory = bundle.get("sentiment_trajectory", [])
        report.tool_calls_used = tools.used
        report.reflection_rounds_used = reflections
        report.evidence = {
            "timeline": bundle["timeline"],
            "agents": bundle["agents"],
            "tool_calls": tools.calls,
        }
        report.caveats = list(report.caveats) + _scale_caveats(bundle, timeline)
        return report

    async def aclose(self) -> None:
        await self.llm.aclose()


def _scale_caveats(bundle: dict[str, Any], timeline: list[dict[str, Any]]) -> list[str]:
    """Caveats the data forces, whatever the model chose to say.

    The model is asked for caveats and usually gives them, but "usually" is not
    a property. These are computed, so a thin run always says it is thin.
    """
    out = []
    agents = bundle["agents"]["total"]
    rounds = len([r for r in timeline if not r["seed"]])
    posts = sum(r["posts"] for r in timeline)

    if agents < 20:
        out.append(
            f"This run had {agents} agent(s). A population this small cannot "
            f"evidence how a narrative spreads; treat every pattern here as "
            f"illustrative rather than measured.")
    if rounds < 5:
        out.append(
            f"Only {rounds} simulated round(s) ran, which is too few to show a "
            f"trajectory as opposed to a starting position.")
    if posts < 20:
        out.append(f"The population produced {posts} post(s) in total.")
    if bundle["agents"]["silent"]:
        out.append(
            f"{bundle['agents']['silent']} agent(s) never acted at all, so the "
            f"findings describe the ones that did.")
    trajectory = bundle.get("sentiment_trajectory") or []
    unscored = [r["round"] for r in trajectory if r["scored"] < r["posts"]]
    if unscored:
        out.append(
            f"Sentiment could not be scored for every post in round(s) "
            f"{', '.join(str(r) for r in unscored)}; those means rest on fewer "
            f"posts than the round produced.")
    return out
