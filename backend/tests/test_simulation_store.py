"""Phase 5 Step 3 — config persistence and operator override.

The review pass exists because generated scenarios are frequently *almost*
right. The risk it introduces is that review becomes the way around the checks:
an operator can attribute an invented sentence to a real named person more
easily than the model can, since they can type plausible ``source_start`` and
``source_end`` values by hand and make a fabrication look evidenced.

So the tests that matter here are the ones asserting an edit is not privileged:
it is re-verified against the source, offsets are recomputed rather than
believed, and every correction is reported back.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import pytest

from app.services.action_space import TWITTER_ACTIONS
from app.services.simulation_config_generator import SimulationConfig
from app.services.simulation_store import (
    CONFIG_FILE,
    META_FILE,
    SimulationMeta,
    SimulationNotFound,
    SimulationState,
    SimulationStore,
    new_sim_id,
)

DOC = """Riverbend City Council — Draft Housing Density Policy 2026

The Planning Committee published its draft density policy on 14 March.
Councillor Jane Doe, who chairs the committee, said the proposal would permit
four-storey development along the Eastgate corridor. The Riverbend Residents
Association immediately objected, arguing that the consultation period of
twenty-one days was inadequate."""

NAMED = ["Councillor Jane Doe", "Riverbend Residents Association", "Riverbend Gazette"]

BASE = {
    "graph_id": "g-1",
    "event": "The council published a draft housing density policy.",
    "rounds": 6,
    "broadcaster": {"name": "Riverbend Wire"},
    "seed_posts": [{"content": "Council publishes draft density policy for Eastgate."}],
    "scheduled_events": [
        {"round": 3, "description": "Developer rebuttal", "content": "Developer responds."}
    ],
}


@pytest.fixture
def store(tmp_path):
    return SimulationStore(tmp_path / "simulations")


@pytest.fixture
def config():
    return SimulationConfig.model_validate(BASE)


@pytest.fixture
def sim(store, config):
    return store.create(config)


def edited(**overrides) -> dict:
    return {**BASE, **overrides}


# --------------------------------------------------------------------------
# Identity and layout
# --------------------------------------------------------------------------


def test_the_spec_layout_is_what_lands_on_disk(store, sim):
    assert store.config_path(sim.sim_id) == store.base_dir / sim.sim_id / CONFIG_FILE
    assert store.config_path(sim.sim_id).is_file()


def test_ids_sort_chronologically():
    early = new_sim_id(now=datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc),
                       rng=random.Random(1))
    late = new_sim_id(now=datetime(2026, 3, 14, 17, 0, tzinfo=timezone.utc),
                      rng=random.Random(1))
    assert early < late
    assert early.startswith("sim-20260314-090000-")


def test_ids_made_in_the_same_second_differ():
    now = datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)
    assert new_sim_id(now=now) != new_sim_id(now=now)


def test_two_scenarios_from_one_graph_coexist(store, config):
    first = store.create(config)
    second = store.create(config)
    assert first.sim_id != second.sim_id
    assert store.load_config(first.sim_id).graph_id == "g-1"
    assert store.load_config(second.sim_id).graph_id == "g-1"


@pytest.mark.parametrize("bad", ["../../etc", "sim-1", "", "sim-20260314-090000-XYZ",
                                 "sim-20260314-090000-a1b2c3/../.."])
def test_a_bogus_id_cannot_escape_the_directory(store, bad):
    """sim_id arrives from a URL path segment."""
    with pytest.raises(SimulationNotFound):
        store.sim_dir(bad)


def test_an_unknown_simulation_is_reported_not_invented(store):
    with pytest.raises(SimulationNotFound):
        store.load_config("sim-20260314-090000-abcdef")


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_the_config_round_trips_unchanged(store, sim, config):
    loaded = store.load_config(sim.sim_id)
    assert loaded.model_dump() == config.model_dump()


def test_the_action_space_survives_persistence(store, sim):
    """The trusted-context path has to hold through the store, not just save/load."""
    assert store.load_config(sim.sim_id).actions == list(TWITTER_ACTIONS)


def test_run_state_is_kept_out_of_the_operators_file(store, sim):
    """An operator editing the scenario must not be able to corrupt run state."""
    written = json.loads(store.config_path(sim.sim_id).read_text())
    assert "state" not in written and "started_at" not in written
    assert store.meta_path(sim.sim_id).is_file()


def test_a_new_simulation_is_a_draft(store, sim):
    assert sim.state == SimulationState.DRAFT
    assert not sim.locked


def test_a_config_written_by_hand_still_opens(store, config):
    """Metadata is recoverable; the scenario is the half that matters."""
    meta = store.create(config)
    store.meta_path(meta.sim_id).unlink()

    rebuilt = store.load_meta(meta.sim_id)
    assert rebuilt.sim_id == meta.sim_id
    assert rebuilt.graph_id == "g-1"
    assert store.meta_path(meta.sim_id).is_file(), "and is written back"


def test_a_half_written_config_is_never_visible(store, config):
    """Writes are atomic; the UI polls these files while they are being saved."""
    store.create(config)
    leftovers = list(store.base_dir.rglob("*.tmp"))
    assert leftovers == []


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


def test_simulations_list_newest_first(store, config):
    ids = [store.create(config).sim_id for _ in range(3)]
    assert [m.sim_id for m in store.list()] == sorted(ids, reverse=True)


def test_listing_filters_by_graph(store, config):
    store.create(config)
    other = SimulationConfig.model_validate({**BASE, "graph_id": "g-2"})
    store.create(other)
    assert [m.graph_id for m in store.list(graph_id="g-2")] == ["g-2"]


def test_listing_an_empty_store(store):
    assert store.list() == []


def test_stray_directories_are_ignored(store, config):
    store.create(config)
    (store.base_dir / "not-a-simulation").mkdir()
    (store.base_dir / "README.txt").write_text("x")
    assert len(store.list()) == 1


def test_one_unreadable_simulation_does_not_hide_the_rest(store, config):
    good = store.create(config)
    broken = store.create(config)
    store.meta_path(broken.sim_id).write_text("{ not json")
    store.config_path(broken.sim_id).unlink()

    listed = [m.sim_id for m in store.list()]
    assert good.sim_id in listed


# --------------------------------------------------------------------------
# Editing — the reason this step needs tests at all
# --------------------------------------------------------------------------


def test_an_ordinary_edit_is_taken_as_written(store, sim):
    result = store.update_config(
        sim.sim_id, edited(event="The council published a revised policy."),
        document=DOC, named_entities=NAMED,
    )
    assert result.changes == []
    assert not result.forked
    assert store.load_config(sim.sim_id).event.endswith("revised policy.")


def test_an_edit_can_enable_a_counterfactual_event(store, sim):
    """Exactly what review is for: Step 1 leaves these off by default."""
    payload = edited(scheduled_events=[
        {"round": 3, "description": "Developer rebuttal", "content": "Developer responds.",
         "counterfactual": True, "enabled": True}])
    result = store.update_config(sim.sim_id, payload, document=DOC, named_entities=NAMED)

    assert len(result.config.enabled_events()) == 1
    assert result.config.scheduled_events[0].counterfactual, "and stays marked as such"


def test_an_edit_may_narrow_the_action_space(store, sim):
    """Operator edits are trusted here, unlike generated output."""
    payload = edited(action_space={"platform": "twitter",
                                   "actions": ["CREATE_POST", "LIKE_POST", "DO_NOTHING"]})
    result = store.update_config(sim.sim_id, payload, document=DOC, named_entities=NAMED)
    assert result.config.actions == ["CREATE_POST", "LIKE_POST", "DO_NOTHING"]


def test_an_edit_cannot_introduce_an_unusable_action(store, sim):
    payload = edited(action_space={"platform": "twitter",
                                   "actions": ["CREATE_POST", "SIGNUP", "DO_NOTHING"]})
    with pytest.raises(ValueError):
        store.update_config(sim.sim_id, payload, document=DOC, named_entities=NAMED)


def test_switching_platform_moves_the_action_space(store, sim):
    result = store.update_config(sim.sim_id, edited(platform="reddit"),
                                 document=DOC, named_entities=NAMED)
    assert "CREATE_COMMENT" in result.config.actions
    assert "REPOST" not in result.config.actions


def test_an_invalid_edit_is_refused_and_changes_nothing(store, sim):
    before = store.load_config(sim.sim_id).model_dump()
    with pytest.raises(ValueError):
        store.update_config(sim.sim_id, edited(seed_posts=[]),
                            document=DOC, named_entities=NAMED)
    assert store.load_config(sim.sim_id).model_dump() == before


def test_edits_are_counted(store, sim):
    for _ in range(3):
        store.update_config(sim.sim_id, BASE, document=DOC, named_entities=NAMED)
    assert store.load_meta(sim.sim_id).edits == 3


# --------------------------------------------------------------------------
# Editing cannot bypass attribution verification
# --------------------------------------------------------------------------


def test_a_genuine_quote_added_by_an_operator_is_accepted(store, sim):
    payload = edited(seed_posts=[{
        "content": "the proposal would permit four-storey development",
        "attribution": "named_quote", "speaker": "Councillor Jane Doe"}])
    result = store.update_config(sim.sim_id, payload, document=DOC, named_entities=NAMED)

    post = result.config.seed_posts[0]
    assert post.attribution == "named_quote"
    assert post.verified
    assert result.changes == []


def test_AN_OPERATOR_CANNOT_ATTRIBUTE_AN_INVENTED_QUOTE_TO_A_REAL_PERSON(store, sim):
    payload = edited(seed_posts=[{
        "content": "Jane Doe said this policy will destroy our community",
        "attribution": "named_quote", "speaker": "Councillor Jane Doe"}])
    result = store.update_config(sim.sim_id, payload, document=DOC, named_entities=NAMED)

    post = result.config.seed_posts[0]
    assert post.attribution == "broadcaster"
    assert post.speaker == ""
    assert "not in the source" in post.demoted_reason


def test_hand_written_offsets_are_recomputed_not_believed(store, sim):
    """The subtlest bypass: fabricated evidence that a fabrication is real."""
    payload = edited(seed_posts=[{
        "content": "Jane Doe called the plan a catastrophe",
        "attribution": "named_quote", "speaker": "Councillor Jane Doe",
        "source_start": 100, "source_end": 140}])
    result = store.update_config(sim.sim_id, payload, document=DOC, named_entities=NAMED)

    post = result.config.seed_posts[0]
    assert post.attribution == "broadcaster"
    assert post.source_start is None and post.source_end is None


def test_a_quote_from_someone_the_document_never_names_is_demoted(store, sim):
    payload = edited(seed_posts=[{
        "content": "the proposal would permit four-storey development",
        "attribution": "named_quote", "speaker": "Someone Invented"}])
    result = store.update_config(sim.sim_id, payload, document=DOC, named_entities=NAMED)
    assert result.config.seed_posts[0].attribution == "broadcaster"


def test_a_correction_is_reported_never_silent(store, sim):
    payload = edited(seed_posts=[{
        "content": "Jane Doe said something she did not say at all",
        "attribution": "named_quote", "speaker": "Councillor Jane Doe"}])
    result = store.update_config(sim.sim_id, payload, document=DOC, named_entities=NAMED)

    assert result.changes, "an operator must be told their edit was changed"
    assert "seed post 0" in result.changes[0]
    assert store.load_meta(sim.sim_id).last_edit_changes == result.changes


def test_an_operator_cannot_take_a_named_organisations_identity(store, sim):
    result = store.update_config(
        sim.sim_id, edited(broadcaster={"name": "Riverbend Gazette"}),
        document=DOC, named_entities=NAMED,
    )
    assert result.config.broadcaster.name != "Riverbend Gazette"
    assert result.changes


def test_a_quote_cannot_buy_acceptance_by_withholding_the_document(store, sim):
    """Otherwise the check is optional: omit the evidence, skip the check."""
    payload = edited(seed_posts=[{
        "content": "Jane Doe said the plan is a disaster",
        "attribution": "named_quote", "speaker": "Councillor Jane Doe"}])
    with pytest.raises(ValueError, match="not available to check it against"):
        store.update_config(sim.sim_id, payload, document="", named_entities=NAMED)


def test_an_edit_cannot_repoint_the_scenario_at_another_graph(store, sim):
    """Otherwise quotes verify against a document the simulation is not about."""
    with pytest.raises(ValueError, match="cannot be repointed"):
        store.update_config(sim.sim_id, edited(graph_id="g-2"),
                            document=DOC, named_entities=NAMED)


def test_an_unquoted_edit_needs_no_document(store, sim):
    result = store.update_config(sim.sim_id, edited(event="Revised."), document="")
    assert result.config.event == "Revised."


# --------------------------------------------------------------------------
# Locking and forking
# --------------------------------------------------------------------------


def test_starting_a_run_locks_the_config(store, sim):
    meta = store.mark_started(sim.sim_id)
    assert meta.state == SimulationState.RUNNING
    assert meta.locked and meta.started_at


def test_editing_a_started_simulation_forks_it(store, sim):
    store.mark_started(sim.sim_id)
    result = store.update_config(sim.sim_id, edited(event="Changed my mind."),
                                 document=DOC, named_entities=NAMED)

    assert result.forked
    assert result.sim_id != sim.sim_id
    assert result.forked_from == sim.sim_id
    assert store.load_meta(result.sim_id).forked_from == sim.sim_id


def test_the_original_is_untouched_by_a_fork(store, sim):
    original = store.load_config(sim.sim_id).model_dump()
    store.mark_started(sim.sim_id)
    store.update_config(sim.sim_id, edited(event="Changed my mind."),
                        document=DOC, named_entities=NAMED)

    assert store.load_config(sim.sim_id).model_dump() == original
    assert store.load_meta(sim.sim_id).state == SimulationState.RUNNING


def test_a_fork_starts_as_an_editable_draft(store, sim):
    store.mark_started(sim.sim_id)
    result = store.update_config(sim.sim_id, edited(event="Variant."),
                                 document=DOC, named_entities=NAMED)

    assert result.meta.state == SimulationState.DRAFT
    assert not result.meta.locked
    assert store.load_config(result.sim_id).event == "Variant."


def test_a_fork_still_re_verifies(store, sim):
    """Forking is not a way around the check either."""
    store.mark_started(sim.sim_id)
    payload = edited(seed_posts=[{
        "content": "Jane Doe said words she never said",
        "attribution": "named_quote", "speaker": "Councillor Jane Doe"}])
    result = store.update_config(sim.sim_id, payload, document=DOC, named_entities=NAMED)

    assert result.forked
    assert result.config.seed_posts[0].attribution == "broadcaster"


@pytest.mark.parametrize("failed", [False, True])
def test_a_finished_run_stays_locked(store, sim, failed):
    store.mark_started(sim.sim_id)
    meta = store.mark_finished(sim.sim_id, failed=failed)
    assert meta.locked
    assert meta.state == (SimulationState.FAILED if failed
                          else SimulationState.COMPLETE)

    result = store.update_config(sim.sim_id, edited(event="After the fact."),
                                 document=DOC, named_entities=NAMED)
    assert result.forked


def test_describe_tells_the_ui_whether_editing_is_allowed(store, sim):
    assert store.describe(sim.sim_id)["editable"] is True
    store.mark_started(sim.sim_id)
    assert store.describe(sim.sim_id)["editable"] is False


def test_meta_round_trips(store, sim):
    meta = store.load_meta(sim.sim_id)
    assert SimulationMeta.model_validate(meta.model_dump()) == meta
    assert json.loads(store.meta_path(sim.sim_id).read_text())["sim_id"] == sim.sim_id
