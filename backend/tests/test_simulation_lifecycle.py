"""Phase 6 Step 6 — the lifecycle state machine.

`test_simulation_control_api.py` covers the same transitions over HTTP. This
covers them where they are actually decided: the store owns the state, the
manager owns the guards, and both are reachable without a request. A rule
enforced only in a route handler is a rule the worker, the scheduler and any
future caller can walk straight past.

The transitions that matter are the ones that must be *refused*. Starting a
simulation with no scenario, or one already running, or one that has finished,
has to fail immediately and say why — a run costs hours, and discovering at
minute forty that it was never going to work is the expensive way to find out.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from app.services.simulation_config_generator import SimulationConfig
from app.services.simulation_manager import CapacityError, SimulationManager
from app.services.simulation_store import (
    SimulationNotFound,
    SimulationState,
    SimulationStore,
)

SCENARIO = {
    "graph_id": "g-1",
    "event": "The council published a draft housing density policy.",
    "rounds": 2,
    "broadcaster": {"name": "Riverbend Wire"},
    "seed_posts": [{"content": "Council publishes draft density policy."}],
}

PROFILES = [{"user_id": 0, "username": "dawn_mercer", "name": "Dawn Mercer",
             "provenance": "synthetic", "activity_level": "high"}]


@pytest.fixture
def short_tmp():
    """Shallow path: the control socket is limited to 107 characters."""
    directory = Path(tempfile.mkdtemp(prefix="cs-", dir="/tmp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def store(short_tmp):
    return SimulationStore(short_tmp / "simulations")


@pytest.fixture
def manager(store, config):
    return SimulationManager(store, config=config)


@pytest.fixture
def created(store):
    """Step one: reserved, nothing generated."""
    return store.create_pending(graph_id="g-1", platform="twitter", rounds=2)


def prepare(store, sim_id):
    """Step two, without paying for it: a scenario and a population on disk."""
    store.save_config(sim_id, SimulationConfig.model_validate(SCENARIO))
    directory = store.profiles_dir(sim_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profiles.json").write_text(json.dumps(PROFILES))


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def test_a_created_simulation_is_a_draft(created):
    assert created.state == SimulationState.DRAFT
    assert not created.locked


def test_creating_generates_nothing(store, created):
    """Preparation costs minutes of inference; creating must not start it."""
    assert not store.prepared(created.sim_id)
    assert not store.profiles_dir(created.sim_id).exists()


def test_the_request_survives_until_prepare_needs_it(store, created):
    assert store.request(created.sim_id)["graph_id"] == "g-1"
    assert store.request(created.sim_id)["rounds"] == 2


def test_a_created_simulation_appears_in_the_listing(store, created):
    assert [m.sim_id for m in store.list()] == [created.sim_id]


def test_an_unprepared_simulation_describes_itself_as_such(store, created):
    described = store.describe(created.sim_id)
    assert described["prepared"] is False
    assert described["config"] is None


def test_asking_for_a_scenario_that_does_not_exist_yet(store, created):
    with pytest.raises(SimulationNotFound):
        store.load_config(created.sim_id)


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------


def test_preparing_attaches_the_scenario(store, created):
    prepare(store, created.sim_id)
    assert store.prepared(created.sim_id)
    assert store.load_config(created.sim_id).rounds == 2


def test_preparing_keeps_it_a_draft(store, created):
    """Only starting locks a simulation; a prepared one is still editable."""
    prepare(store, created.sim_id)
    assert store.load_meta(created.sim_id).state == SimulationState.DRAFT
    assert store.describe(created.sim_id)["editable"] is True


def test_the_platform_follows_the_prepared_scenario(store, created):
    store.save_config(created.sim_id, SimulationConfig.model_validate(
        {**SCENARIO, "platform": "reddit"}))
    assert store.load_meta(created.sim_id).platform == "reddit"


# --------------------------------------------------------------------------
# start — and what must be refused
# --------------------------------------------------------------------------


def test_STARTING_AN_UNPREPARED_SIMULATION_IS_REJECTED(manager, store, created):
    """The spec's named case. It must fail now, not forty minutes in.

    Enforced in the manager, not only in the route handler: found by this test,
    which previously watched a worker get spawned for a simulation with no
    configuration at all.
    """
    with pytest.raises(CapacityError, match="prepare it before starting"):
        manager.start(created.sim_id)


def test_the_error_says_what_is_missing(manager, store, created):
    with pytest.raises(CapacityError) as caught:
        manager.start(created.sim_id)
    assert "config.json" in str(caught.value)


def test_a_scenario_without_a_population_is_also_rejected(manager, store, created):
    store.save_config(created.sim_id, SimulationConfig.model_validate(SCENARIO))
    with pytest.raises(CapacityError, match="no population"):
        manager.start(created.sim_id)


def test_starting_an_unknown_simulation_is_refused(manager):
    with pytest.raises(SimulationNotFound):
        manager.start("sim-20260101-000000-abcdef")


def test_starting_a_running_simulation_is_refused(manager, store, created):
    prepare(store, created.sim_id)
    store.mark_started(created.sim_id)

    with pytest.raises(CapacityError, match="draft or a failed"):
        manager.start(created.sim_id)


def test_starting_a_completed_simulation_is_refused(manager, store, created):
    prepare(store, created.sim_id)
    store.mark_started(created.sim_id)
    store.mark_finished(created.sim_id)

    with pytest.raises(CapacityError, match="draft or a failed"):
        manager.start(created.sim_id)


def test_a_failed_simulation_may_be_started_again(manager, store, created):
    """Resuming is the point of the checkpoints; the guard must allow it."""
    prepare(store, created.sim_id)
    store.mark_started(created.sim_id)
    store.mark_finished(created.sim_id, failed=True)

    meta = store.load_meta(created.sim_id)
    assert meta.state == SimulationState.FAILED
    assert meta.state in (SimulationState.DRAFT, SimulationState.FAILED)


def test_capacity_is_checked_before_anything_is_spawned(manager, store, config):
    manager.config = config.model_copy(update={"MAX_CONCURRENT_SIMULATIONS": 0})
    first = store.create_pending(graph_id="g-1")
    prepare(store, first.sim_id)

    with pytest.raises(CapacityError, match="maximum"):
        manager.start(first.sim_id)
    assert store.load_meta(first.sim_id).state == SimulationState.DRAFT


# --------------------------------------------------------------------------
# the states themselves
# --------------------------------------------------------------------------


def test_starting_locks_the_configuration(store, created):
    prepare(store, created.sim_id)
    meta = store.mark_started(created.sim_id)

    assert meta.state == SimulationState.RUNNING
    assert meta.locked
    assert meta.started_at
    assert store.describe(created.sim_id)["editable"] is False


@pytest.mark.parametrize(("failed", "expected"), [
    (False, SimulationState.COMPLETE),
    (True, SimulationState.FAILED),
])
def test_finishing_records_which_way_it_ended(store, created, failed, expected):
    prepare(store, created.sim_id)
    store.mark_started(created.sim_id)
    meta = store.mark_finished(created.sim_id, failed=failed)

    assert meta.state == expected
    assert meta.finished_at
    assert meta.locked, "a finished run's configuration stays frozen"


@pytest.mark.parametrize("state", [
    SimulationState.RUNNING, SimulationState.COMPLETE, SimulationState.FAILED])
def test_every_state_but_draft_is_locked(state):
    assert state in SimulationState.LOCKED


def test_a_draft_is_the_only_editable_state():
    assert SimulationState.DRAFT not in SimulationState.LOCKED


# --------------------------------------------------------------------------
# stop
# --------------------------------------------------------------------------


def test_stopping_something_that_never_ran_is_not_an_error(manager, store, created):
    prepare(store, created.sim_id)
    assert manager.stop(created.sim_id) == "not running"


def test_stopping_reconciles_a_run_whose_process_has_gone(manager, store, created):
    """The state on disk must not outlive the process it describes."""
    prepare(store, created.sim_id)
    store.mark_started(created.sim_id)

    assert manager.stop(created.sim_id) == "not running"
    assert store.load_meta(created.sim_id).state != SimulationState.RUNNING


def test_stopping_an_unknown_simulation_is_refused(manager):
    with pytest.raises(SimulationNotFound):
        manager.stop("sim-20260101-000000-abcdef")


# --------------------------------------------------------------------------
# the whole sequence
# --------------------------------------------------------------------------


def test_the_full_sequence_create_prepare_start_stop(manager, store):
    meta = store.create_pending(graph_id="g-1", platform="twitter", rounds=2)
    assert store.load_meta(meta.sim_id).state == SimulationState.DRAFT

    prepare(store, meta.sim_id)
    assert store.prepared(meta.sim_id)
    assert store.load_meta(meta.sim_id).state == SimulationState.DRAFT

    store.mark_started(meta.sim_id)
    assert store.load_meta(meta.sim_id).state == SimulationState.RUNNING

    manager.stop(meta.sim_id)
    assert store.load_meta(meta.sim_id).state in {
        SimulationState.COMPLETE, SimulationState.FAILED}


def test_a_forked_edit_starts_a_new_lifecycle(store, created):
    """Editing a locked run produces a fresh draft rather than reopening it."""
    prepare(store, created.sim_id)
    store.mark_started(created.sim_id)

    result = store.update_config(created.sim_id, {**SCENARIO, "event": "Revised."},
                                 document="", named_entities=[])
    assert result.forked
    assert store.load_meta(result.sim_id).state == SimulationState.DRAFT
    assert store.load_meta(created.sim_id).state == SimulationState.RUNNING
