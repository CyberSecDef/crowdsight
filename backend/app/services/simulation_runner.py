"""Phase 6 Step 1 — OASIS bound to local inference.

This is the integration point the whole project rests on. Every one of the
thousands of agent turns in a run goes through the model built by
:func:`build_model`; if that binding is wrong, a run quietly ships the document,
the personas and every generated post to somebody else's API. Nothing else in
the system would notice, because a cloud model answers perfectly well.

So the binding is guarded rather than merely written correctly. :func:`build_model`
refuses any platform other than ``ModelPlatformType.OLLAMA`` and re-checks the
URL through the same :func:`classify_host` the configuration uses, and there is
no parameter through which a caller can ask for anything else.

Three things learned from reading the installed camel-oasis 0.2.5 shape the rest:

* **``OllamaModel`` starts its own server when no URL is given.** With ``url``
  unset it falls back to ``OLLAMA_BASE_URL`` and then calls ``_start_server()``,
  shelling out to an ``ollama`` binary that does not exist in this image. An
  empty URL therefore has to be caught here, where the error can say why.

* **``OasisEnv`` takes ``semaphore=128`` by default.** That is OASIS's own
  concurrency limiter, and 128 simultaneous completions against one 12 GB GPU is
  exactly the exhaustion Phase 2's gate exists to prevent. It is bound to
  ``LLM_CONCURRENCY`` here; Phase 6 Step 2 divides that budget across worker
  processes.

* **``env.step()`` ends in ``asyncio.gather(*tasks)`` with no
  ``return_exceptions``.** One agent raising — a timeout, a malformed tool call —
  propagates out and aborts the round, losing every other agent's turn with it
  and killing a run that may be hours in. :func:`harden_agent` wraps each agent's
  LLM turn so it cannot raise: a failure is logged, counted, and treated as
  having done nothing. Manual actions are deliberately left unwrapped — a seed
  post that cannot be published is a broken run, not a lost turn.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence
from urllib.parse import urlparse

from app.config import Config, classify_host, get_config
from app.services.action_space import ActionSpace, default_action_space, select_active
from app.services.simulation_config_generator import SimulationConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from camel.models import BaseModelBackend
    from oasis.social_agent.agent import SocialAgent
    from oasis.social_agent.agent_graph import AgentGraph

logger = logging.getLogger(__name__)

__all__ = [
    "BROADCASTER_USER_ID",
    "ModelBindingError",
    "RoundSummary",
    "SimulationError",
    "SimulationRunner",
    "build_model",
    "harden_agent",
    "trim_agent_memory",
]

#: Sentinel for the broadcaster's slot in our own records. The agent's real
#: OASIS id is assigned after the population, so it never collides; this marks
#: it as "not one of the population" for Phase 7's analytics.
BROADCASTER_USER_ID = -1

PROFILES_FILE = "profiles.json"
TWITTER_PROFILE_FILE = "twitter.csv"
REDDIT_PROFILE_FILE = "reddit.json"


class SimulationError(RuntimeError):
    """A simulation could not be prepared or run."""


class ModelBindingError(SimulationError):
    """A model was requested that would send prompts off this host."""


# --------------------------------------------------------------------------
# The binding
# --------------------------------------------------------------------------


def build_model(
    config: Config | None = None,
    *,
    temperature: float | None = None,
    model_name: str | None = None,
) -> "BaseModelBackend":
    """A CAMEL model backend bound to the local Ollama. The only way to get one.

    There is deliberately no platform parameter. A caller cannot ask for
    OpenAI, Anthropic or anything else, so "no code path can construct a cloud
    model" is a property of the signature rather than a convention.
    """
    from camel.models import ModelFactory
    from camel.types import ModelPlatformType

    config = config or get_config()
    url = (config.LLM_BASE_URL or "").strip()

    if not url:
        # camel would fall back to OLLAMA_BASE_URL and then shell out to an
        # `ollama` binary this image does not contain.
        raise ModelBindingError(
            "LLM_BASE_URL is empty. camel would try to start its own Ollama "
            "server rather than use the local one."
        )

    host = urlparse(url).hostname or ""
    kind = classify_host(host, config.ALLOWED_HOSTS)
    if kind == "public":
        raise ModelBindingError(
            f"Refusing to bind the simulation model to {url!r}: {host!r} is a "
            f"public host. Every agent turn would leave this machine."
        )

    temperature = config.SIMULATION_TEMPERATURE if temperature is None else temperature
    model = ModelFactory.create(
        model_platform=ModelPlatformType.OLLAMA,
        model_type=model_name or config.LLM_MODEL_NAME,
        url=url,
        api_key=config.LLM_API_KEY.get_secret_value() or "ollama",
        model_config_dict={"temperature": temperature},
        timeout=config.LLM_TIMEOUT,
    )
    logger.info(
        "Simulation model bound to %s (%s) at %s [%s]",
        model_name or config.LLM_MODEL_NAME, ModelPlatformType.OLLAMA.value, url, kind,
    )
    return model


# --------------------------------------------------------------------------
# Failure isolation
# --------------------------------------------------------------------------


@dataclass
class RoundSummary:
    """What one round did. Enough for progress reporting and a later report."""

    index: int
    invoked: int = 0
    failed: int = 0
    skipped: int = 0
    events_fired: int = 0
    #: What the agents actually did, counted from OASIS's trace table.
    action_counts: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def acted(self) -> int:
        return self.invoked - self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.index, "invoked": self.invoked, "acted": self.acted,
            "failed": self.failed, "skipped": self.skipped,
            "events_fired": self.events_fired, "action_counts": self.action_counts,
            "failures": self.failures[:20],
        }


def harden_agent(agent: "SocialAgent", summary_ref: list[RoundSummary]) -> None:
    """Make one agent's LLM turn unable to abort the round.

    OASIS gathers every agent's turn without ``return_exceptions``, so an
    unhandled error in any single turn propagates out of ``env.step()`` and ends
    the run. Patched per instance rather than on the class: two simulations in
    one process must not reconfigure each other.
    """
    if getattr(agent, "_crowdsight_hardened", False):
        return

    original = agent.perform_action_by_llm

    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return await original(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the entire point is not to raise
            name = getattr(agent.user_info, "user_name", None) or agent.social_agent_id
            logger.warning("Agent %s failed its turn (%s: %s)", name,
                           type(exc).__name__, exc)
            if summary_ref:
                summary_ref[-1].failed += 1
                summary_ref[-1].failures.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

    agent.perform_action_by_llm = guarded  # type: ignore[method-assign]
    agent._crowdsight_hardened = True  # type: ignore[attr-defined]


def trim_agent_memory(agent: "SocialAgent", keep_turns: int) -> int:
    """Bound one agent's memory to its most recent turns.

    CAMEL records both sides of every turn and OASIS never resets, so an
    agent's context grows for the whole run. Trimming keeps cost per round flat
    and makes a resumed run's agents the same shape as the ones it replaced —
    a fresh process has no memory at all, so an unbounded run could never be
    resumed faithfully.

    Trimmed at user-message boundaries: a tool result whose preceding assistant
    tool-call has been dropped is rejected by the completions API, so slicing
    blindly would break the very next turn.
    """
    if keep_turns <= 0:
        return 0
    try:
        from camel.types import OpenAIBackendRole

        records = [context.memory_record for context in agent.memory.retrieve()]
    except Exception:  # noqa: BLE001 - never fail a run over bookkeeping
        logger.debug("Could not read memory for agent %s", agent.social_agent_id)
        return 0

    system = [r for r in records if r.role_at_backend == OpenAIBackendRole.SYSTEM]
    rest = [r for r in records if r.role_at_backend != OpenAIBackendRole.SYSTEM]

    # Each turn is a user message plus whatever it produced.
    starts = [i for i, r in enumerate(rest)
              if r.role_at_backend == OpenAIBackendRole.USER]
    if len(starts) <= keep_turns:
        return 0

    kept = rest[starts[-keep_turns]:]
    dropped = len(rest) - len(kept)
    try:
        agent.memory.clear()
        agent.memory.write_records(system + kept)
    except Exception:  # noqa: BLE001
        logger.exception("Could not trim memory for agent %s", agent.social_agent_id)
        return 0
    return dropped


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------


class SimulationRunner:
    """Prepares and drives one OASIS simulation.

    Step 1 covers construction and the round mechanics. Persistence,
    checkpointing and resume are Step 3; process isolation is Step 2.
    """

    def __init__(
        self,
        sim_config: SimulationConfig,
        profiles_dir: str | Path,
        database_path: str | Path,
        *,
        config: Config | None = None,
        concurrency: int | None = None,
        rng_seed: int | None = None,
        resume: bool = False,
    ) -> None:
        self.config = config or get_config()
        self.sim_config = sim_config
        self.profiles_dir = Path(profiles_dir)
        self.database_path = Path(database_path)
        self.concurrency = concurrency or self.config.LLM_CONCURRENCY
        self.resume = resume
        self.memory_rounds = self.config.SIMULATION_MEMORY_ROUNDS
        self.action_space: ActionSpace = (
            sim_config.action_space or default_action_space(sim_config.platform)
        )

        import random as _random

        self.rng = _random.Random(rng_seed)
        self.env: Any = None
        self.agent_graph: "AgentGraph | None" = None
        self.broadcaster: "SocialAgent | None" = None
        self.model: "BaseModelBackend | None" = None
        #: agent_id -> our own full profile record, and the activity level
        #: drawn from it for the participation roll.
        self.profiles: dict[int, dict[str, Any]] = {}
        self.activity: dict[int, str] = {}
        self.rounds_run: list[RoundSummary] = []
        self._current: list[RoundSummary] = []

    # -- preparation --------------------------------------------------------

    @property
    def profile_path(self) -> Path:
        name = (TWITTER_PROFILE_FILE if self.sim_config.platform == "twitter"
                else REDDIT_PROFILE_FILE)
        return self.profiles_dir / name

    def _load_profile_record(self) -> dict[int, dict[str, Any]]:
        """Our own full record of the population, keyed by OASIS agent id.

        The OASIS files are lossy: neither loader's schema carries
        ``activity_level``, which the participation roll needs, and the Twitter
        loader drops the username too. Both live here.
        """
        path = self.profiles_dir / PROFILES_FILE
        if not path.is_file():
            logger.warning("No %s; every agent will be invoked every round", path)
            return {}
        entries = json.loads(path.read_text(encoding="utf-8"))
        return {int(e["user_id"]): e for e in entries if "user_id" in e}

    def _name_the_agents(self, graph: "AgentGraph") -> int:
        """Give every agent the username its persona was written with.

        ``generate_twitter_agent_graph`` builds ``UserInfo(name=..., description=...)``
        and never sets ``user_name``, so ``generate_custom_agents`` signs each
        agent up as NULL and the run database cannot say who posted what. Must
        happen before ``reset()``, which is where signup runs.
        """
        named = 0
        for agent_id, agent in graph.get_agents():
            record = self.profiles.get(agent_id)
            if not record or getattr(agent.user_info, "user_name", None):
                continue
            username = str(record.get("username") or "").strip()
            if username:
                agent.user_info.user_name = username
                named += 1
        if named:
            logger.info("Named %d agent(s) OASIS would have signed up as NULL", named)
        return named

    async def build_agent_graph(self) -> "AgentGraph":
        """Load the population OASIS will run, bound to the local model.

        Both generators are coroutines despite reading as plain factories.
        """
        from oasis.social_agent.agents_generator import (
            generate_reddit_agent_graph,
            generate_twitter_agent_graph,
        )

        if not self.profile_path.is_file():
            raise SimulationError(
                f"No {self.sim_config.platform} profile file at {self.profile_path}. "
                f"Generate the population before starting a run."
            )

        self.model = build_model(self.config)
        actions = self.action_space.to_oasis()
        generate = (generate_twitter_agent_graph
                    if self.sim_config.platform == "twitter"
                    else generate_reddit_agent_graph)
        graph = await generate(str(self.profile_path), self.model, actions)

        count = len(graph.get_agents())
        if count > self.config.MAX_AGENTS:
            raise SimulationError(
                f"{count} agents exceeds MAX_AGENTS={self.config.MAX_AGENTS}"
            )
        logger.info("Loaded %d agents with %d actions each", count,
                    len(self.action_space.actions))
        return graph

    def add_broadcaster(self, graph: "AgentGraph") -> "SocialAgent":
        """Add the account that posts the seed content.

        Kept out of the profile files on purpose: written in among the personas
        it would be indistinguishable from one downstream, and it would land in
        Phase 7's sentiment and influence statistics as though it were a member
        of the public.
        """
        from oasis.social_agent.agent import SocialAgent
        from oasis.social_platform.config.user import UserInfo

        broadcaster = self.sim_config.broadcaster
        agent_id = max((agent_id for agent_id, _ in graph.get_agents()), default=-1) + 1
        description = broadcaster.description or f"{broadcaster.name}, a local news account."

        agent = SocialAgent(
            agent_id=agent_id,
            user_info=UserInfo(
                user_name=broadcaster.handle,
                name=broadcaster.name,
                description=description,
                profile={"nodes": [], "edges": [],
                         "other_info": {"user_profile": description,
                                        "mbti": "ENTJ", "gender": "organisation",
                                        "age": 0, "country": ""}},
                recsys_type=self.sim_config.platform,
            ),
            model=self.model,
            available_actions=self.action_space.to_oasis(),
        )
        graph.add_agent(agent)
        logger.info("Broadcaster @%s added as agent %d (not part of the population)",
                    broadcaster.handle, agent_id)
        return agent

    async def setup(self) -> None:
        """Build the environment and sign every agent up."""
        import oasis
        from oasis.social_platform.typing import DefaultPlatformType

        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        # OASIS resolves the database in two different ways. `oasis.make()`
        # takes the path we pass, but `SocialEnvironment` calls `get_db_path()`
        # on every agent turn to build that agent's feed — and with
        # OASIS_DB_PATH unset that falls back to a single shared
        # `social_media.db` inside the installed package. Unwritable as a
        # non-root user, and worse if it ever were writable: every run in the
        # image would read and write one file. Setting it here is what makes
        # the agents read the run they are actually in.
        #
        # Process-global, so two runners sharing one process would fight over
        # it. Step 2 gives every run its own process, which is the real fix;
        # until then, run them one at a time.
        os.environ["OASIS_DB_PATH"] = str(self.database_path)

        if self.database_path.exists() and not self.resume:
            # OASIS appends rather than replacing: `create_db` reports "table
            # already exists" to stdout and carries on with the old data. A
            # stale file would silently mix two runs together.
            raise SimulationError(
                f"{self.database_path} already exists. Pass resume=True to "
                f"continue that run, or delete it to start over."
            )

        graph = await self.build_agent_graph()
        self.profiles = self._load_profile_record()
        self.activity = {agent_id: str(record.get("activity_level") or "")
                         for agent_id, record in self.profiles.items()}
        self._name_the_agents(graph)
        self.broadcaster = self.add_broadcaster(graph)
        self.agent_graph = graph

        for _, agent in graph.get_agents():
            harden_agent(agent, self._current)

        platform = (DefaultPlatformType.TWITTER
                    if self.sim_config.platform == "twitter"
                    else DefaultPlatformType.REDDIT)
        self.env = oasis.make(
            agent_graph=graph,
            platform=platform,
            database_path=str(self.database_path),
            # OASIS defaults this to 128. On one local GPU that is the
            # exhaustion the concurrency bound exists to prevent.
            semaphore=self.concurrency,
        )
        await self.env.reset()
        logger.info("Environment ready: %s, %d agents, concurrency %d",
                    self.sim_config.platform, len(graph.get_agents()), self.concurrency)

    # -- running ------------------------------------------------------------

    def population(self) -> list[tuple[int, "SocialAgent"]]:
        """Every agent except the broadcaster."""
        if self.agent_graph is None:
            return []
        broadcaster_id = self.broadcaster.social_agent_id if self.broadcaster else None
        return [(agent_id, agent) for agent_id, agent in self.agent_graph.get_agents()
                if agent_id != broadcaster_id]

    async def seed(self) -> RoundSummary:
        """Publish the seed posts, so the population has something to react to."""
        from oasis.environment.env_action import ManualAction
        from oasis.social_platform.typing import ActionType

        if self.env is None or self.broadcaster is None:
            raise SimulationError("setup() must run before seeding")

        summary = RoundSummary(index=0)
        posts = [
            ManualAction(action_type=ActionType.CREATE_POST,
                         action_args={"content": post.content})
            for post in self.sim_config.seed_posts
        ]
        if not posts:
            raise SimulationError("The scenario has no seed posts to publish")

        # Left unhardened deliberately: a seed post that cannot be published
        # means the population has nothing to react to, which is not a run.
        await self.env.step({self.broadcaster: posts})
        summary.invoked = len(posts)
        logger.info("Seeded %d post(s) from @%s", len(posts),
                    self.sim_config.broadcaster.handle)
        return summary

    async def run_round(self, index: int) -> RoundSummary:
        """One round: fire any enabled event, then let the active agents act."""
        from oasis.environment.env_action import LLMAction, ManualAction
        from oasis.social_platform.typing import ActionType

        if self.env is None:
            raise SimulationError("setup() must run before stepping")

        summary = RoundSummary(index=index)
        self._current.append(summary)
        try:
            actions: dict[Any, Any] = {}

            events = self.sim_config.events_for_round(index)
            if events and self.broadcaster is not None:
                actions[self.broadcaster] = [
                    ManualAction(action_type=ActionType.CREATE_POST,
                                 action_args={"content": event.content})
                    for event in events
                ]
                summary.events_fired = len(events)
                logger.info("Round %d: firing %d enabled event(s)", index, len(events))

            everyone = self.population()
            active = select_active(
                everyone, rng=self.rng,
                activity_of=lambda pair: self.activity.get(pair[0]),
            )
            summary.invoked = len(active)
            summary.skipped = len(everyone) - len(active)
            for _, agent in active:
                actions[agent] = LLMAction()

            if actions:
                await self.env.step(actions)
            else:
                logger.info("Round %d: nobody active", index)
        finally:
            self._current.pop()

        trimmed = sum(trim_agent_memory(agent, self.memory_rounds)
                      for _, agent in self.agent_graph.get_agents()) \
            if self.agent_graph is not None else 0
        if trimmed:
            logger.debug("Trimmed %d memory record(s) after round %d", trimmed, index)

        self.rounds_run.append(summary)
        logger.info("Round %d: %d acted, %d quiet, %d failed",
                    index, summary.acted, summary.skipped, summary.failed)
        return summary

    def advance_clock(self, rounds_done: int) -> None:
        """Move the sandbox clock past rounds that already happened.

        A resumed run gets a fresh ``Clock`` starting at step zero, so new
        posts would carry timestamps earlier than ones already in the database
        — and the Twitter recommender orders by recency.
        """
        clock = getattr(getattr(self.env, "platform", None), "sandbox_clock", None)
        if clock is not None and hasattr(clock, "time_step"):
            clock.time_step = max(int(clock.time_step), int(rounds_done))
            logger.info("Sandbox clock advanced to step %d", clock.time_step)

    async def run(self, rounds: int | None = None) -> list[RoundSummary]:
        """Seed, then run every round. Step 3 adds persistence between them."""
        total = rounds or self.sim_config.rounds
        summaries = [await self.seed()]
        for index in range(1, total + 1):
            summaries.append(await self.run_round(index))
        return summaries

    async def close(self) -> None:
        if self.env is not None:
            await self.env.close()
            self.env = None
