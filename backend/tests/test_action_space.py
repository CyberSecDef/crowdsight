"""Phase 5 Step 2 — the action set agents may choose from.

The failure this guards against is quiet. OASIS warns about an unrecognised
action through its own logger and then filters it out, so a typo costs one
behaviour and a wrong list costs all of them — an agent with no tools sits out
the entire run and the transcript reads as an apathetic crowd.

So the central test is not "our list looks right", it is *OASIS accepts every
action we configure*: the contract section below reproduces the filter from
``SocialAgent.__init__`` against the real tool list and asserts nothing is
dropped.
"""

from __future__ import annotations

import json
import random

import pytest
from pydantic import ValidationError

from app.services.action_space import (
    ACTIVITY_PARTICIPATION,
    AGENT_INVOKABLE,
    DO_NOTHING,
    ENGINE_ONLY,
    OFF_SCENARIO,
    PLATFORM_ACTIONS,
    REDDIT_ACTIONS,
    TWITTER_ACTIONS,
    ActionSpace,
    ActionSpaceError,
    default_action_space,
    expected_calls_per_round,
    participation_rate,
    select_active,
)
from app.services.simulation_config_generator import SimulationConfig


@pytest.fixture(scope="module")
def oasis_action_type():
    """The real enum. Imported once — `import oasis` is slow."""
    from oasis.social_platform.typing import ActionType

    return ActionType


@pytest.fixture(scope="module")
def oasis_tool_names():
    """Exactly the names `SocialAgent.__init__` filters against."""
    from oasis.social_agent.agent_action import SocialAction
    from oasis.social_platform.channel import Channel

    return {tool.func.__name__ for tool in SocialAction(0, Channel()).get_openai_function_list()}


# ==========================================================================
# The contract: OASIS must accept everything we configure
# ==========================================================================


@pytest.mark.parametrize("platform", ["twitter", "reddit"])
def test_oasis_drops_nothing_from_our_action_space(platform, oasis_tool_names):
    """Reproduces the filter in `SocialAgent.__init__` (agent.py:92-102)."""
    space = default_action_space(platform)
    requested = space.to_oasis()

    kept = [action for action in requested if action.value in oasis_tool_names]
    assert len(kept) == len(requested), (
        "OASIS would silently drop "
        f"{[a.name for a in requested if a.value not in oasis_tool_names]}"
    )
    assert kept, "an empty tool list leaves the agent unable to act at all"


def test_our_mirror_of_the_enum_is_current(oasis_action_type, oasis_tool_names):
    """A camel-oasis upgrade that changes the action list must fail here."""
    invokable = {a.name for a in oasis_action_type if a.value in oasis_tool_names}
    assert AGENT_INVOKABLE == invokable, (
        f"missing: {sorted(invokable - AGENT_INVOKABLE)}; "
        f"stale: {sorted(AGENT_INVOKABLE - invokable)}"
    )


def test_engine_only_actions_exist_but_have_no_tool(oasis_action_type, oasis_tool_names):
    names = {a.name for a in oasis_action_type}
    assert ENGINE_ONLY <= names, "these must still be real enum members"
    assert not {oasis_action_type[n].value for n in ENGINE_ONLY} & oasis_tool_names


def test_to_oasis_returns_real_enum_members(oasis_action_type):
    actions = default_action_space("reddit").to_oasis()
    assert all(isinstance(a, oasis_action_type) for a in actions)
    assert [a.name for a in actions] == list(REDDIT_ACTIONS)


# ==========================================================================
# Per-platform sets
# ==========================================================================


def test_the_spec_lists_are_what_we_ship():
    assert set(TWITTER_ACTIONS) == {
        "CREATE_POST", "LIKE_POST", "REPOST", "FOLLOW", "QUOTE_POST", "DO_NOTHING",
    }
    assert set(REDDIT_ACTIONS) == {
        "LIKE_POST", "DISLIKE_POST", "CREATE_POST", "CREATE_COMMENT",
        "LIKE_COMMENT", "DISLIKE_COMMENT", "SEARCH_POSTS", "SEARCH_USER",
        "TREND", "REFRESH", "FOLLOW", "MUTE", "DO_NOTHING",
    }


@pytest.mark.parametrize("platform", ["twitter", "reddit"])
def test_every_platform_action_is_invokable(platform):
    assert set(PLATFORM_ACTIONS[platform]) <= AGENT_INVOKABLE


@pytest.mark.parametrize("platform", ["twitter", "reddit"])
def test_every_platform_offers_doing_nothing(platform):
    """A population that always acts is unrealistic and inflates cost."""
    assert DO_NOTHING in PLATFORM_ACTIONS[platform]


@pytest.mark.parametrize("platform", ["twitter", "reddit"])
def test_no_platform_offers_another_scenarios_actions(platform):
    assert not set(PLATFORM_ACTIONS[platform]) & OFF_SCENARIO
    assert not set(PLATFORM_ACTIONS[platform]) & ENGINE_ONLY


def test_the_platforms_are_behaviourally_distinct():
    """Reddit threads and downvotes; Twitter amplifies."""
    assert {"CREATE_COMMENT", "DISLIKE_POST"} <= set(REDDIT_ACTIONS)
    assert {"CREATE_COMMENT", "DISLIKE_POST"}.isdisjoint(TWITTER_ACTIONS)
    assert {"REPOST", "QUOTE_POST"} <= set(TWITTER_ACTIONS)
    assert {"REPOST", "QUOTE_POST"}.isdisjoint(REDDIT_ACTIONS)


def test_an_unknown_platform_is_refused():
    with pytest.raises(ActionSpaceError, match="Unknown platform"):
        default_action_space("mastodon")  # type: ignore[arg-type]


def test_a_default_space_is_the_platform_default():
    assert ActionSpace(platform="reddit").actions == list(REDDIT_ACTIONS)


# ==========================================================================
# Validation — at configuration time, not run time
# ==========================================================================


def test_an_unknown_action_is_rejected():
    with pytest.raises(ActionSpaceError, match="not an OASIS action"):
        ActionSpace(platform="twitter", actions=["CREATE_POST", "YELL_LOUDLY", DO_NOTHING])


def test_a_near_miss_is_diagnosed():
    """`LIKE_POSTS` is the kind of typo OASIS answers with silence."""
    with pytest.raises(ActionSpaceError, match="did you mean 'LIKE_POST'"):
        ActionSpace(platform="twitter", actions=["LIKE_POSTS", DO_NOTHING])


def test_an_off_platform_action_is_rejected():
    """Valid in OASIS, wrong for the platform: Reddit does not repost."""
    with pytest.raises(ActionSpaceError, match="not part of the reddit action set"):
        ActionSpace(platform="reddit", actions=["CREATE_POST", "REPOST", DO_NOTHING])


def test_an_engine_only_action_is_rejected():
    with pytest.raises(ActionSpaceError, match="driven by the OASIS engine"):
        ActionSpace(platform="twitter", actions=["CREATE_POST", "SIGNUP", DO_NOTHING])


def test_another_scenarios_action_is_rejected():
    with pytest.raises(ActionSpaceError, match="another OASIS scenario"):
        ActionSpace(platform="twitter", actions=["CREATE_POST", "PURCHASE_PRODUCT",
                                                 DO_NOTHING])


def test_do_nothing_cannot_be_removed():
    with pytest.raises(ActionSpaceError, match="DO_NOTHING must be available"):
        ActionSpace(platform="twitter", actions=["CREATE_POST", "LIKE_POST"])


def test_do_nothing_alone_is_rejected():
    with pytest.raises(ActionSpaceError, match="unable to do anything"):
        ActionSpace(platform="twitter", actions=[DO_NOTHING])


def test_all_problems_are_reported_together():
    with pytest.raises(ActionSpaceError) as caught:
        ActionSpace(platform="reddit", actions=["REPOST", "NONSENSE", DO_NOTHING])
    assert "REPOST" in str(caught.value) and "NONSENSE" in str(caught.value)


def test_a_narrower_set_is_allowed():
    """Restricting is legitimate; only invalid actions are refused."""
    space = ActionSpace(platform="reddit", actions=["CREATE_COMMENT", "LIKE_POST",
                                                    DO_NOTHING])
    assert space.acting == ["CREATE_COMMENT", "LIKE_POST"]


# ==========================================================================
# Normalisation
# ==========================================================================


def test_case_and_whitespace_are_forgiven():
    assert ActionSpace(platform="twitter",
                       actions=[" create_post ", "Like_Post", "do_nothing"]).actions == [
        "CREATE_POST", "LIKE_POST", "DO_NOTHING"]


def test_duplicates_collapse_and_order_is_kept():
    space = ActionSpace(platform="twitter",
                        actions=["LIKE_POST", "CREATE_POST", "LIKE_POST", DO_NOTHING])
    assert space.actions == ["LIKE_POST", "CREATE_POST", DO_NOTHING]


def test_actiontype_members_are_accepted(oasis_action_type):
    space = ActionSpace(platform="twitter", actions=[
        oasis_action_type.CREATE_POST, oasis_action_type.DO_NOTHING])
    assert space.actions == ["CREATE_POST", "DO_NOTHING"]


def test_can_is_case_insensitive():
    space = default_action_space("twitter")
    assert space.can("create_post") and space.can("REPOST")
    assert not space.can("CREATE_COMMENT")


# ==========================================================================
# Participation — inactivity that costs nothing
# ==========================================================================


def test_activity_level_orders_participation():
    assert (participation_rate("low") < participation_rate("moderate")
            < participation_rate("high"))
    assert all(0 < rate <= 1 for rate in ACTIVITY_PARTICIPATION.values())


@pytest.mark.parametrize("level", [None, "", "unknown", "  HIGH  "])
def test_any_level_yields_a_usable_rate(level):
    assert 0 < participation_rate(level) <= 1


def test_a_quiet_agent_is_invoked_less_often_than_a_loud_one():
    class Agent:
        def __init__(self, level):
            self.activity_level = level

    quiet = [Agent("low") for _ in range(400)]
    loud = [Agent("high") for _ in range(400)]
    rng = random.Random(7)
    assert len(select_active(quiet, rng=rng)) < len(select_active(loud, rng=rng))


def test_selection_is_reproducible_from_a_seed():
    class Agent:
        activity_level = "moderate"

    agents = [Agent() for _ in range(50)]
    first = select_active(agents, rng=random.Random(3))
    second = select_active(agents, rng=random.Random(3))
    assert [id(a) for a in first] == [id(a) for a in second]


def test_selection_stays_within_the_population():
    class Agent:
        activity_level = "high"

    agents = [Agent() for _ in range(30)]
    chosen = select_active(agents, rng=random.Random(1))
    assert set(map(id, chosen)) <= set(map(id, agents))
    assert len(chosen) <= len(agents)


def test_an_empty_population_selects_nobody():
    assert select_active([], rng=random.Random(1)) == []


def test_expected_cost_is_below_one_call_per_agent():
    """The whole point: a quiet crowd is cheaper than a loud one."""
    levels = ["low"] * 100 + ["moderate"] * 100 + ["high"] * 100
    assert expected_calls_per_round(levels) < len(levels)
    assert expected_calls_per_round(["high"] * 10) > expected_calls_per_round(["low"] * 10)


# ==========================================================================
# Integration with the simulation config
# ==========================================================================


BASE = {
    "event": "The council published a draft housing density policy.",
    "rounds": 6,
    "broadcaster": {"name": "Riverbend Wire"},
    "seed_posts": [{"content": "Council publishes draft density policy."}],
}


def test_a_config_gets_its_platforms_action_space():
    assert SimulationConfig.model_validate({**BASE, "platform": "reddit"}).actions == list(
        REDDIT_ACTIONS)
    assert SimulationConfig.model_validate(BASE).actions == list(TWITTER_ACTIONS)


def test_set_platform_switches_the_action_space_too():
    """A plain attribute assignment would not — pydantic skips validators."""
    config = SimulationConfig.model_validate(BASE)
    config.set_platform("reddit")
    assert config.platform == "reddit"
    assert config.actions == list(REDDIT_ACTIONS)
    assert "CREATE_COMMENT" in config.actions and "REPOST" not in config.actions


def test_the_model_cannot_choose_the_action_space():
    """Generated output must not be able to fail on a field it was not asked for."""
    config = SimulationConfig.model_validate(
        {**BASE, "action_space": {"platform": "twitter", "actions": ["CREATE_POST"]}}
    )
    assert config.actions == list(TWITTER_ACTIONS)


def test_the_action_space_survives_a_round_trip(tmp_path):
    config = SimulationConfig.model_validate({**BASE, "platform": "reddit"})
    config.action_space = ActionSpace(platform="reddit",
                                      actions=["CREATE_COMMENT", "LIKE_POST", DO_NOTHING])
    path = config.save(tmp_path / "config.json")

    assert SimulationConfig.load(path).actions == ["CREATE_COMMENT", "LIKE_POST",
                                                   DO_NOTHING]


def test_a_saved_config_records_the_actions(tmp_path):
    path = SimulationConfig.model_validate(BASE).save(tmp_path / "c.json")
    assert "CREATE_POST" in json.loads(path.read_text())["action_space"]["actions"]


def test_a_config_whose_action_space_is_for_another_platform_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({
        **BASE, "platform": "reddit",
        "action_space": {"platform": "twitter", "actions": list(TWITTER_ACTIONS)},
    }))
    with pytest.raises(ValidationError, match="but the simulation runs on"):
        SimulationConfig.load(path)


def test_the_summary_mentions_the_action_count():
    assert "6 actions" in SimulationConfig.model_validate(BASE).summary()
