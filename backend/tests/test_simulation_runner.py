"""Phase 6 Step 1 — driving OASIS.

Three things here are not obvious from the OASIS API and would each fail
silently or expensively in a real run, so each has a test that fails if the
workaround is removed:

* ``env.step()`` gathers agent turns without ``return_exceptions``, so one
  failure aborts a round — and with it a run that may be hours old.
* ``get_db_path()`` falls back to a database *inside the installed package*
  when ``OASIS_DB_PATH`` is unset, and agents read their feed through it.
* ``OasisEnv`` defaults its concurrency limiter to 128.

The smoke test at the bottom is the one Step 1 actually asks for: three agents,
two rounds, real local inference.
"""

from __future__ import annotations

import json
import os

import pytest

from app.services.action_space import REDDIT_ACTIONS, TWITTER_ACTIONS
from app.services.simulation_config_generator import SimulationConfig
from app.services.simulation_runner import (
    RoundSummary,
    SimulationError,
    SimulationRunner,
    harden_agent,
)

SCENARIO = {
    "graph_id": "g-1",
    "event": "The council published a draft housing density policy.",
    "rounds": 3,
    "broadcaster": {"name": "Riverbend Wire", "description": "Local news"},
    "seed_posts": [{"content": "Council publishes draft density policy for Eastgate."}],
    "scheduled_events": [
        {"round": 2, "description": "Developer rebuttal", "content": "Developers respond."}
    ],
}

PROFILE_RECORD = [
    {"user_id": 0, "username": "dawn_mercer", "name": "Dawn Mercer",
     "activity_level": "high"},
    {"user_id": 1, "username": "ray_nkemelu", "name": "Ray Nkemelu",
     "activity_level": "low"},
]


@pytest.fixture
def sim_config():
    return SimulationConfig.model_validate(SCENARIO)


@pytest.fixture
def profiles_dir(tmp_path):
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "profiles.json").write_text(json.dumps(PROFILE_RECORD))
    (directory / "twitter.csv").write_text("user_id\n0\n1\n")
    return directory


@pytest.fixture
def runner(sim_config, profiles_dir, tmp_path, config):
    return SimulationRunner(sim_config, profiles_dir, tmp_path / "run.db",
                            config=config, rng_seed=1)


# --------------------------------------------------------------------------
# Failure isolation
# --------------------------------------------------------------------------


class FakeUserInfo:
    def __init__(self, user_name="someone"):
        self.user_name = user_name


class FakeAgent:
    """Just enough of a SocialAgent for the wrapper."""

    def __init__(self, exc: Exception | None = None, user_name="someone"):
        self.social_agent_id = 7
        self.user_info = FakeUserInfo(user_name)
        self._exc = exc
        self.calls = 0

    async def perform_action_by_llm(self, *args, **kwargs):
        self.calls += 1
        if self._exc:
            raise self._exc
        return "acted"


async def test_a_failing_turn_does_not_propagate():
    """OASIS gathers turns without return_exceptions; this is what stops a round dying."""
    agent = FakeAgent(RuntimeError("model timed out"))
    summary = RoundSummary(index=1)
    harden_agent(agent, [summary])

    assert await agent.perform_action_by_llm() is None
    assert summary.failed == 1


async def test_the_failure_is_recorded_with_the_agents_name():
    agent = FakeAgent(ValueError("bad tool call"), user_name="dawn_mercer")
    summary = RoundSummary(index=1)
    harden_agent(agent, [summary])
    await agent.perform_action_by_llm()

    assert "dawn_mercer" in summary.failures[0]
    assert "ValueError" in summary.failures[0]


async def test_a_successful_turn_is_untouched():
    agent = FakeAgent()
    harden_agent(agent, [RoundSummary(index=1)])
    assert await agent.perform_action_by_llm() == "acted"


async def test_hardening_is_idempotent():
    """Setup runs over every agent; wrapping twice would double-count failures."""
    agent = FakeAgent(RuntimeError("x"))
    summary = RoundSummary(index=1)
    harden_agent(agent, [summary])
    harden_agent(agent, [summary])
    await agent.perform_action_by_llm()

    assert summary.failed == 1


async def test_a_failure_outside_a_round_does_not_crash_the_wrapper():
    """Seeding and interviews call agents with no round in progress."""
    agent = FakeAgent(RuntimeError("x"))
    harden_agent(agent, [])
    assert await agent.perform_action_by_llm() is None


async def test_baseexceptions_still_propagate():
    """Cancellation must not be swallowed, or a stop request would be ignored."""
    agent = FakeAgent(KeyboardInterrupt())
    harden_agent(agent, [RoundSummary(index=1)])
    with pytest.raises(KeyboardInterrupt):
        await agent.perform_action_by_llm()


# --------------------------------------------------------------------------
# Round accounting
# --------------------------------------------------------------------------


def test_acted_excludes_failures():
    assert RoundSummary(index=1, invoked=10, failed=3).acted == 7


def test_a_summary_serialises_for_progress_reporting():
    payload = RoundSummary(index=2, invoked=5, failed=1, skipped=3,
                           events_fired=1).to_dict()
    assert payload == {"round": 2, "invoked": 5, "acted": 4, "failed": 1,
                       "skipped": 3, "events_fired": 1, "failures": []}


def test_a_flood_of_failures_does_not_bloat_the_progress_payload():
    summary = RoundSummary(index=1, failures=[f"agent{i}: boom" for i in range(500)])
    assert len(summary.to_dict()["failures"]) == 20


# --------------------------------------------------------------------------
# Setup guards
# --------------------------------------------------------------------------


def test_the_action_space_comes_from_the_scenario(runner):
    assert runner.action_space.actions == list(TWITTER_ACTIONS)


def test_a_reddit_scenario_gets_the_reddit_action_space(profiles_dir, tmp_path, config):
    reddit = SimulationConfig.model_validate({**SCENARIO, "platform": "reddit"})
    runner = SimulationRunner(reddit, profiles_dir, tmp_path / "r.db", config=config)
    assert runner.action_space.actions == list(REDDIT_ACTIONS)
    assert runner.profile_path.name == "reddit.json"


def test_the_platform_selects_the_profile_file(runner):
    assert runner.profile_path.name == "twitter.csv"


def test_concurrency_defaults_to_the_configured_budget(runner, config):
    assert runner.concurrency == config.LLM_CONCURRENCY


def test_concurrency_can_be_divided_for_a_worker(sim_config, profiles_dir, tmp_path,
                                                 config):
    """Step 2 splits LLM_CONCURRENCY across simultaneous runs."""
    runner = SimulationRunner(sim_config, profiles_dir, tmp_path / "x.db",
                              config=config, concurrency=2)
    assert runner.concurrency == 2


async def test_a_missing_profile_file_is_refused_before_any_model_work(
        sim_config, tmp_path, config):
    runner = SimulationRunner(sim_config, tmp_path / "nothing", tmp_path / "x.db",
                              config=config)
    with pytest.raises(SimulationError, match="profile file"):
        await runner.build_agent_graph()


async def test_an_existing_database_is_refused_rather_than_appended_to(runner):
    """OASIS appends; a stale file silently mixes two runs together."""
    runner.database_path.parent.mkdir(parents=True, exist_ok=True)
    runner.database_path.write_text("")
    with pytest.raises(SimulationError, match="already exists"):
        await runner.setup()


def test_the_profile_record_supplies_activity_levels(runner):
    record = runner._load_profile_record()
    assert record[0]["activity_level"] == "high"
    assert record[1]["username"] == "ray_nkemelu"


def test_a_missing_profile_record_does_not_stop_a_run(sim_config, tmp_path, config):
    """Without activity levels everyone is invoked, which is worse but not fatal."""
    empty = tmp_path / "profiles"
    empty.mkdir()
    runner = SimulationRunner(sim_config, empty, tmp_path / "y.db", config=config)
    assert runner._load_profile_record() == {}


async def test_seeding_before_setup_is_an_error(runner):
    with pytest.raises(SimulationError, match="setup"):
        await runner.seed()


async def test_stepping_before_setup_is_an_error(runner):
    with pytest.raises(SimulationError, match="setup"):
        await runner.run_round(1)


def test_the_population_is_empty_before_setup(runner):
    assert runner.population() == []


# --------------------------------------------------------------------------
# The smoke test Step 1 asks for
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_three_agents_two_rounds_against_local_ollama(integration_config, tmp_path):
    """Step 1's stated acceptance criterion, end to end.

    Proves the whole chain: the local binding answers, OASIS builds a
    population from the files Phase 4 wrote, the broadcaster seeds the event,
    agents take real turns against local inference, and it all lands in the
    run's own database.
    """
    import sqlite3

    from app.services.oasis_profiles import write_profiles
    from app.services.profile_generator import PersonaProfile

    people = [
        PersonaProfile(name="Dawn Mercer", age=41, occupation="carpenter",
                       activity_level="high", gender="female",
                       country="United Kingdom",
                       background="Runs a small joinery business."),
        PersonaProfile(name="Ray Nkemelu", age=33, occupation="bus driver",
                       activity_level="high", gender="male",
                       country="United Kingdom",
                       background="Drives the Eastgate route."),
        PersonaProfile(name="Ursula Ferreira", age=67, occupation="full-time carer",
                       activity_level="high", gender="female",
                       country="United Kingdom",
                       background="Has lived on the corridor for forty years."),
    ]
    profiles = tmp_path / "profiles"
    write_profiles(people, profiles, default_country="United Kingdom")

    config = SimulationConfig.model_validate({**SCENARIO, "rounds": 2})
    config.scheduled_events[0].enabled = True

    runner = SimulationRunner(config, profiles, tmp_path / "sim.db",
                              config=integration_config, rng_seed=3)
    try:
        await runner.setup()

        assert len(runner.population()) == 3
        assert runner.broadcaster is not None
        assert runner.env.llm_semaphore._value == integration_config.LLM_CONCURRENCY
        assert os.environ["OASIS_DB_PATH"] == str(runner.database_path)

        summaries = await runner.run(rounds=2)
    finally:
        await runner.close()

    assert len(summaries) == 3, "a seed round plus two rounds"
    assert summaries[2].events_fired == 1, "the enabled event fired in its round"
    assert sum(s.invoked for s in summaries[1:]) > 0, "agents took real turns"

    connection = sqlite3.connect(runner.database_path)
    users = connection.execute("SELECT user_id, user_name FROM user").fetchall()
    posts = connection.execute("SELECT user_id, content FROM post").fetchall()
    connection.close()

    assert len(users) == 4, "three agents and the broadcaster"
    assert all(name for _, name in users), "OASIS would sign these up as NULL"
    assert any("density policy" in (c or "") for _, c in posts), "the seed post"
    assert any("Developers respond" in (c or "") for _, c in posts), "the event"
    assert len(posts) > 2, "agents posted of their own accord"
