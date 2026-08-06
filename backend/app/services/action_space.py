"""Phase 5 Step 2 — the action set agents may choose from.

OASIS does not validate this for us. ``SocialAgent.__init__`` logs a warning for
an unrecognised action and then *filters it out*::

    for action in available_actions:
        if action_name not in all_possible_actions:
            agent_log.warning(f"Action {action_name} is not supported. ...")
    self.action_tools = [tool for tool in all_tools if tool.func.__name__ in ...]

So a typo costs one silently-missing behaviour, and a wholly wrong list leaves
``action_tools`` empty — an agent with no tools, which does nothing for the
entire run. That reads as an apathetic population, not a broken config, and the
warning is buried in OASIS's own logger. Hence validation here, up front.

Two further facts established by reading the installed camel-oasis 0.2.5:

* Of the 32 ``ActionType`` members, 29 are agent-invokable. ``EXIT``, ``SIGNUP``
  and ``UPDATE_REC_TABLE`` are driven by the engine and have no tool.
* ``recsys_type`` selects the recommender and the system-message wording. It
  does *not* restrict actions — every action works on both platforms. The
  per-platform split below is our realism constraint, not OASIS's.

``AGENT_INVOKABLE`` is mirrored rather than imported: ``import oasis`` costs
about four seconds, which is too much for a module the API imports. The mirror
is not trusted on faith — ``tests/test_action_space.py`` diffs it against the
real enum, so a version bump that adds or removes an action fails the suite.
"""

from __future__ import annotations

import difflib
import logging
import random
from typing import TYPE_CHECKING, Callable, Iterable, Literal, Sequence, TypeVar

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from oasis.social_platform.typing import ActionType

logger = logging.getLogger(__name__)

Platform = Literal["twitter", "reddit"]

DO_NOTHING = "DO_NOTHING"

#: Members of ``ActionType`` the engine drives itself; never agent choices.
ENGINE_ONLY: frozenset[str] = frozenset({"EXIT", "SIGNUP", "UPDATE_REC_TABLE"})

#: Every action an OASIS agent can actually be given a tool for.
AGENT_INVOKABLE: frozenset[str] = frozenset({
    "CREATE_POST", "REPOST", "QUOTE_POST", "CREATE_COMMENT",
    "LIKE_POST", "UNLIKE_POST", "DISLIKE_POST", "UNDO_DISLIKE_POST",
    "LIKE_COMMENT", "UNLIKE_COMMENT", "DISLIKE_COMMENT", "UNDO_DISLIKE_COMMENT",
    "FOLLOW", "UNFOLLOW", "MUTE", "UNMUTE", "REPORT_POST",
    "SEARCH_POSTS", "SEARCH_USER", "TREND", "REFRESH",
    "DO_NOTHING",
    # Belong to OASIS's other scenarios (shopping, groups, research probes).
    "PURCHASE_PRODUCT", "INTERVIEW",
    "JOIN_GROUP", "LEAVE_GROUP", "CREATE_GROUP", "SEND_TO_GROUP",
    "LISTEN_FROM_GROUP",
})

#: Invokable, but not part of a discourse simulation. Enabling ``PURCHASE_PRODUCT``
#: in a housing-policy run gives agents a shopping tool they will occasionally
#: reach for, which is noise. ``INTERVIEW`` is a research probe driven from
#: outside the population, not something an agent does to its own timeline.
OFF_SCENARIO: frozenset[str] = frozenset({
    "PURCHASE_PRODUCT", "INTERVIEW",
    "JOIN_GROUP", "LEAVE_GROUP", "CREATE_GROUP", "SEND_TO_GROUP",
    "LISTEN_FROM_GROUP",
})

#: Broadcast-shaped: write, amplify, endorse, follow.
TWITTER_ACTIONS: tuple[str, ...] = (
    "CREATE_POST", "LIKE_POST", "REPOST", "FOLLOW", "QUOTE_POST", DO_NOTHING,
)

#: Forum-shaped: threaded replies, bidirectional voting, and active search.
REDDIT_ACTIONS: tuple[str, ...] = (
    "CREATE_POST", "CREATE_COMMENT",
    "LIKE_POST", "DISLIKE_POST", "LIKE_COMMENT", "DISLIKE_COMMENT",
    "SEARCH_POSTS", "SEARCH_USER", "TREND", "REFRESH",
    "FOLLOW", "MUTE", DO_NOTHING,
)

PLATFORM_ACTIONS: dict[str, tuple[str, ...]] = {
    "twitter": TWITTER_ACTIONS,
    "reddit": REDDIT_ACTIONS,
}

# --------------------------------------------------------------------------
# Participation
# --------------------------------------------------------------------------

#: Probability an agent of each activity level is invoked in a given round.
#:
#: DO_NOTHING alone does not model a quiet population: choosing it still costs a
#: full inference, so a 300-agent run pays 300 calls a round however inert the
#: crowd. Rolling participation first makes lurking free, which is what lurking
#: is. Agents who *are* invoked keep DO_NOTHING, so "looked and said nothing"
#: stays distinct from "was not looking".
ACTIVITY_PARTICIPATION: dict[str, float] = {
    "low": 0.20,
    "moderate": 0.55,
    "high": 0.90,
}

DEFAULT_PARTICIPATION = 0.55

T = TypeVar("T")


def participation_rate(activity_level: str | None) -> float:
    """Per-round probability of being invoked. Unknown levels get the middle."""
    if not activity_level:
        return DEFAULT_PARTICIPATION
    return ACTIVITY_PARTICIPATION.get(activity_level.strip().lower(), DEFAULT_PARTICIPATION)


def select_active(
    agents: Sequence[T],
    *,
    rng: random.Random | None = None,
    activity_of: Callable[[T], str | None] = lambda a: getattr(a, "activity_level", None),
) -> list[T]:
    """The subset of ``agents`` to invoke this round.

    Everyone else is omitted from the step dict entirely, costing nothing. A
    round in which nobody is selected is a legitimate quiet round, not an error.
    """
    rng = rng or random.Random()
    return [a for a in agents if rng.random() < participation_rate(activity_of(a))]


def expected_calls_per_round(activity_levels: Iterable[str | None]) -> float:
    """Expected inference count per round — for cost estimates before a run."""
    return sum(participation_rate(level) for level in activity_levels)


# --------------------------------------------------------------------------
# The action space
# --------------------------------------------------------------------------


class ActionSpaceError(ValueError):
    """A configured action would be dropped or misbehave at run time."""


def _explain(action: str, platform: str) -> str:
    """Say why an action is refused, and suggest the intended one."""
    if action in ENGINE_ONLY:
        return (
            f"{action!r} is driven by the OASIS engine and has no agent tool; "
            "it cannot be an agent action"
        )
    if action in OFF_SCENARIO:
        return (
            f"{action!r} belongs to another OASIS scenario (shopping, groups or "
            "research probes) and is not part of a discourse simulation"
        )
    if action in AGENT_INVOKABLE:
        allowed = ", ".join(PLATFORM_ACTIONS[platform])
        return (
            f"{action!r} is a valid OASIS action but not part of the {platform} "
            f"action set; permitted: {allowed}"
        )
    close = difflib.get_close_matches(action, sorted(AGENT_INVOKABLE), n=1, cutoff=0.6)
    suggestion = f"; did you mean {close[0]!r}?" if close else ""
    return f"{action!r} is not an OASIS action{suggestion}"


class ActionSpace(BaseModel):
    """The permitted actions for one platform, validated before a run starts.

    Constructing one directly raises :class:`ActionSpaceError`. Pydantic wraps
    any ``ValueError`` a validator raises into a ``ValidationError``, so without
    the unwrapping below ``except ActionSpaceError`` would never fire — the
    documented error type would be unreachable. Validated *as part of another
    model* (loading a ``SimulationConfig``) it stays a ``ValidationError``,
    since that carries the field path the caller needs.
    """

    platform: Platform = "twitter"
    actions: list[str] = Field(default_factory=list)

    def __init__(self, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise ActionSpaceError(
                "; ".join(error["msg"].removeprefix("Value error, ")
                          for error in exc.errors())
            ) from None

    @field_validator("actions", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> object:
        """Accept ``ActionType`` members, lowercase names, and stray whitespace."""
        if not isinstance(value, (list, tuple, set, frozenset)):
            return value
        seen: set[str] = set()
        out: list[str] = []
        for item in value:
            name = getattr(item, "name", None) or str(item)
            name = name.strip().upper()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    @model_validator(mode="after")
    def _every_action_is_usable(self) -> "ActionSpace":
        if not self.actions:
            object.__setattr__(self, "actions", list(PLATFORM_ACTIONS[self.platform]))
            return self

        permitted = set(PLATFORM_ACTIONS[self.platform])
        problems = [
            _explain(action, self.platform)
            for action in self.actions
            if action not in permitted
        ]
        if problems:
            raise ActionSpaceError(
                f"Invalid {self.platform} action space: " + "; ".join(problems)
            )

        if DO_NOTHING not in self.actions:
            raise ActionSpaceError(
                "DO_NOTHING must be available: without it an invoked agent is "
                "forced to act, which inflates engagement and cost"
            )
        if self.actions == [DO_NOTHING]:
            raise ActionSpaceError(
                "DO_NOTHING alone leaves the population unable to do anything"
            )
        return self

    # -- derived ------------------------------------------------------------

    def to_oasis(self) -> list["ActionType"]:
        """The list to hand to OASIS. Imports oasis lazily — it is slow."""
        from oasis.social_platform.typing import ActionType

        return [ActionType[name] for name in self.actions]

    def can(self, action: str) -> bool:
        return action.strip().upper() in set(self.actions)

    @property
    def acting(self) -> list[str]:
        """Actions that change the world — everything but DO_NOTHING."""
        return [a for a in self.actions if a != DO_NOTHING]

    def summary(self) -> str:
        return f"{self.platform}: {len(self.actions)} actions ({', '.join(self.actions)})"


def default_action_space(platform: Platform = "twitter") -> ActionSpace:
    """The permitted set for a platform, as configured above."""
    if platform not in PLATFORM_ACTIONS:
        raise ActionSpaceError(
            f"Unknown platform {platform!r}; expected one of "
            f"{', '.join(sorted(PLATFORM_ACTIONS))}"
        )
    return ActionSpace(platform=platform, actions=list(PLATFORM_ACTIONS[platform]))
