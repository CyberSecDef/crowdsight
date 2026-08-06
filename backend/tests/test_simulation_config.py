"""Phase 5 Step 4 — the scenario config itself.

Step 1 built this and was verified with a throwaway script. That script is not
in the repo, so until now the load-bearing property of the whole phase — a
paraphrase must not end up attributed to a real, named person — had no standing
coverage of its *generation* path. `test_simulation_store.py` exercises
`verify_scenario` through operator edits; nothing exercised the generator, the
verbatim matcher, or the round arithmetic.

The mocked tests assert against a model I control, which means they can only
prove the code does what I assumed the model does. The `integration` case at the
bottom runs a real scenario through `qwen2.5:14b` and asserts the result
validates — the only check here that can catch the model drifting into output
the schema rejects.
"""

from __future__ import annotations

import json

import pytest
import respx
from pydantic import ValidationError

from app.services.simulation_config_generator import (
    POST_LENGTH_LIMIT,
    Broadcaster,
    ScenarioError,
    ScheduledEvent,
    SeedPost,
    SimulationConfig,
    SimulationConfigGenerator,
    find_verbatim,
)
from app.utils.llm_client import LLMClient
from app.utils.retry import RetryPolicy
from tests.conftest import chat_completion

CHAT = "http://ollama:11434/v1/chat/completions"

DOC = """Riverbend City Council — Draft Housing Density Policy 2026

The Planning Committee published its draft density policy on 14 March.
Councillor Jane Doe, who chairs the committee, said the proposal would permit
four-storey development along the Eastgate corridor. The Riverbend Residents
Association immediately objected, arguing that the consultation period of
twenty-one days was inadequate.

Mayor Alan Reyes defended the timetable, noting that the Regional Housing Board
had set a September deadline for submissions."""

NAMED = ["Councillor Jane Doe", "Mayor Alan Reyes", "Riverbend Residents Association",
         "Regional Housing Board", "Riverbend Gazette"]

QUOTE = "the proposal would permit four-storey development"

BASE = {
    "event": "The council published a draft housing density policy.",
    "rounds": 8,
    "hours_per_round": 6,
    "broadcaster": {"name": "Riverbend Wire", "description": "Local news account"},
    "seed_posts": [{"content": "Council publishes draft density policy for Eastgate.",
                    "attribution": "broadcaster"}],
    "scheduled_events": [
        {"round": 4, "description": "Developer rebuttal", "content": "Developer responds."},
        {"round": 7, "description": "Leaked modelling", "content": "Traffic data leaks."},
    ],
}


@pytest.fixture
def generator(config):
    def _make(**kwargs):
        return SimulationConfigGenerator(config, llm=LLMClient(
            config, retry_policy=RetryPolicy(max_attempts=1), **kwargs))
    return _make


def scenario(**overrides) -> dict:
    return {**BASE, **overrides}


# ==========================================================================
# Verbatim matching — what separates a quote from a fabrication
# ==========================================================================


def test_a_real_quote_is_located():
    span = find_verbatim(DOC, QUOTE)
    assert span is not None
    assert DOC[span[0]:span[1]].lower().replace("\n", " ") == QUOTE


def test_offsets_index_the_original_document():
    """A reviewer checks the quote by slicing the source with these numbers."""
    start, end = find_verbatim(DOC, QUOTE)
    assert DOC[start:end] == "the proposal would permit\nfour-storey development"


def test_whitespace_differences_do_not_defeat_a_match():
    """A model reproducing a line across a chunk boundary collapses newlines."""
    assert find_verbatim(DOC, "the proposal would permit\n   four-storey   development") \
        == find_verbatim(DOC, QUOTE)


def test_case_differences_do_not_defeat_a_match():
    assert find_verbatim(DOC, QUOTE.upper()) == find_verbatim(DOC, QUOTE)


@pytest.mark.parametrize("wrapped", [f'"{QUOTE}"', f"“{QUOTE}”", f"'{QUOTE}'"])
def test_surrounding_quote_marks_are_stripped(wrapped):
    assert find_verbatim(DOC, wrapped) == find_verbatim(DOC, QUOTE)


@pytest.mark.parametrize("text", [
    "the plan allows buildings of four storeys",     # paraphrase
    "this policy will destroy our community",        # invention
    "the proposal would forbid four-storey development",  # one word changed
])
def test_text_that_is_not_in_the_document_is_not_found(text):
    assert find_verbatim(DOC, text) is None


@pytest.mark.parametrize("fragment", ["the plan", "said", "", "  ", "March"])
def test_a_fragment_too_short_to_be_evidence_is_refused(fragment):
    """"the plan" appears in every planning document ever written."""
    assert find_verbatim(DOC, fragment) is None


def test_an_empty_document_matches_nothing():
    assert find_verbatim("", QUOTE) is None


def test_every_sentence_of_the_document_is_findable():
    """A guard that cannot succeed is as useless as one that cannot fail."""
    sentences = [s.strip() for s in DOC.replace("\n", " ").split(".") if len(s.strip()) > 20]
    assert sentences
    assert all(find_verbatim(DOC, s) is not None for s in sentences)


# ==========================================================================
# Schema
# ==========================================================================


def test_a_valid_scenario_parses():
    config = SimulationConfig.model_validate(BASE)
    assert config.event.startswith("The council")
    assert len(config.seed_posts) == 1


@pytest.mark.parametrize(("bad", "why"), [
    ({"event": ""}, "an empty event"),
    ({"event": "   "}, "a whitespace event"),
    ({"seed_posts": []}, "no seed posts"),
    ({"rounds": 0}, "zero rounds"),
    ({"rounds": -3}, "negative rounds"),
    ({"hours_per_round": 0}, "a zero cadence"),
    ({"hours_per_round": -1}, "a negative cadence"),
    ({"hours_per_round": 10_000}, "an absurd cadence"),
    ({"broadcaster": {"name": ""}}, "a nameless broadcaster"),
    ({"broadcaster": {"name": "@"}}, "a broadcaster named only @"),
    ({"platform": "mastodon"}, "an unsupported platform"),
])
def test_an_unusable_scenario_is_refused(bad, why):
    with pytest.raises(ValidationError):
        SimulationConfig.model_validate(scenario(**bad))


def test_a_seed_post_needs_content():
    with pytest.raises(ValidationError):
        SeedPost(content="   ")


def test_an_event_before_round_one_is_refused():
    with pytest.raises(ValidationError):
        ScheduledEvent(round=0, description="x", content="y")


@pytest.mark.parametrize("field", ["description", "content"])
def test_an_empty_scheduled_event_field_is_refused(field):
    with pytest.raises(ValidationError):
        ScheduledEvent(**{"round": 1, "description": "d", "content": "c", field: "  "})


def test_an_unknown_attribution_is_refused():
    with pytest.raises(ValidationError):
        SeedPost(content="Something happened.", attribution="anonymous_source")


# --------------------------------------------------------------------------
# Broadcaster
# --------------------------------------------------------------------------


def test_a_handle_is_derived_from_the_name():
    assert Broadcaster(name="Riverbend Wire").handle == "riverbend_wire"


def test_a_handle_the_model_already_decorated_is_normalised():
    """Observed on the first real run: a display layer produced "@@RB_Echo"."""
    assert Broadcaster(name="Riverbend Echo", handle="@RB_Echo").handle == "rb_echo"


def test_an_at_sign_in_the_display_name_is_stripped():
    assert Broadcaster(name="@RiverbendNews").name == "RiverbendNews"


@pytest.mark.parametrize(("handle", "expected"), [
    ("The Wire!!", "the_wire"),
    ("  spaced  out  ", "spaced_out"),
    ("!!!", "wire"),
])
def test_a_handle_is_always_usable(handle, expected):
    assert Broadcaster(name="X", handle=handle).handle == expected


def test_a_very_long_handle_is_bounded():
    assert len(Broadcaster(name="A" * 200).handle) <= 24


# ==========================================================================
# Rounds and the run window
# ==========================================================================


def test_a_start_time_is_always_present():
    assert SimulationConfig.model_validate(BASE).start_time


def test_round_times_advance_by_the_cadence():
    config = SimulationConfig.model_validate(
        scenario(start_time="2026-03-14T09:00:00+00:00", hours_per_round=6))
    assert config.round_time(0).startswith("2026-03-14T09:00")
    assert config.round_time(2).startswith("2026-03-14T21:00")


def test_an_unparseable_start_time_does_not_crash_round_timing():
    config = SimulationConfig.model_validate(scenario(start_time="not a date"))
    assert config.round_time(1)


def test_events_past_the_last_round_are_dropped():
    """An event scheduled past the end simply never fires."""
    config = SimulationConfig.model_validate(scenario(rounds=5))
    assert [e.round for e in config.scheduled_events] == [4]


def test_every_surviving_event_falls_inside_the_window():
    config = SimulationConfig.model_validate(scenario(rounds=6))
    assert all(1 <= e.round <= config.rounds for e in config.scheduled_events)


def test_events_are_kept_when_they_fit():
    config = SimulationConfig.model_validate(scenario(rounds=8))
    assert len(config.scheduled_events) == 2


def test_set_rounds_drops_the_events_it_orphans():
    """Assigning `rounds` alone would leave events that can never fire."""
    config = SimulationConfig.model_validate(BASE)
    config.set_rounds(5)
    assert config.rounds == 5
    assert [e.round for e in config.scheduled_events] == [4]


def test_set_rounds_never_goes_below_one():
    config = SimulationConfig.model_validate(BASE)
    config.set_rounds(0)
    assert config.rounds == 1


def test_widening_the_window_keeps_what_is_left():
    config = SimulationConfig.model_validate(BASE)
    config.set_rounds(5)
    config.set_rounds(20)
    assert [e.round for e in config.scheduled_events] == [4], "dropped is dropped"


def test_scheduled_events_start_counterfactual_and_disabled():
    """A baseline run must reflect the document, not an invented rebuttal."""
    config = SimulationConfig.model_validate(BASE)
    assert all(e.counterfactual and not e.enabled for e in config.scheduled_events)
    assert config.enabled_events() == []


def test_enabling_an_event_surfaces_it_for_its_round():
    config = SimulationConfig.model_validate(BASE)
    config.scheduled_events[0].enabled = True
    assert len(config.enabled_events()) == 1
    assert len(config.events_for_round(4)) == 1
    assert config.events_for_round(7) == []


# ==========================================================================
# MAX_ROUNDS
# ==========================================================================


@respx.mock
async def test_the_model_cannot_exceed_max_rounds(generator, config):
    """The cost ceiling: rounds x agents is the whole bill for a run."""
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(scenario(rounds=999))))
    result = await generator().generate(DOC, named_entities=NAMED)
    assert result.rounds == config.MAX_ROUNDS


@respx.mock
async def test_a_caller_can_ask_for_fewer_rounds(generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(BASE)))
    result = await generator().generate(DOC, named_entities=NAMED, rounds=3)
    assert result.rounds == 3


@respx.mock
async def test_a_caller_cannot_ask_for_more_than_max_rounds(generator, config):
    """The ceiling binds, but does not inflate a modest request to meet it."""
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(scenario(rounds=999))))
    result = await generator().generate(DOC, named_entities=NAMED, rounds=10_000)
    assert result.rounds == config.MAX_ROUNDS

    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(BASE)))
    modest = await generator().generate(DOC, named_entities=NAMED, rounds=10_000)
    assert modest.rounds == 8


@respx.mock
async def test_capping_rounds_also_drops_the_events_it_orphans(generator, config):
    """Otherwise a capped run carries events that can never fire."""
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(scenario(rounds=999))))
    result = await generator().generate(DOC, named_entities=NAMED, rounds=3)
    assert result.rounds == 3
    assert all(e.round <= 3 for e in result.scheduled_events)


# ==========================================================================
# Generation
# ==========================================================================


@respx.mock
async def test_a_generated_config_validates_and_is_complete(generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(BASE)))
    result = await generator().generate(DOC, named_entities=NAMED, graph_id="g-1",
                                        platform="reddit")
    assert result.graph_id == "g-1"
    assert result.platform == "reddit"
    assert result.event and result.seed_posts and result.broadcaster.name
    assert result.actions, "an action space is attached"


@respx.mock
async def test_the_prompt_carries_the_document_and_its_named_entities(generator):
    route = respx.post(CHAT).mock(return_value=chat_completion(json.dumps(BASE)))
    await generator().generate(DOC, named_entities=NAMED)

    prompt = json.loads(route.calls[0].request.content)["messages"][-1]["content"]
    assert "Councillor Jane Doe" in prompt
    assert "Eastgate corridor" in prompt
    assert "do not invent quotes" in prompt.lower()


@respx.mock
async def test_a_long_document_is_excerpted_not_sent_whole(generator):
    route = respx.post(CHAT).mock(return_value=chat_completion(json.dumps(BASE)))
    await generator().generate(DOC * 500, named_entities=NAMED, excerpt_limit=1000)

    prompt = json.loads(route.calls[0].request.content)["messages"][-1]["content"]
    assert len(prompt) < 5000


async def test_an_empty_document_is_refused_without_calling_the_model(generator):
    with pytest.raises(ScenarioError):
        await generator().generate("   ", named_entities=NAMED)


@respx.mock
async def test_unusable_model_output_raises_rather_than_half_working(generator):
    respx.post(CHAT).mock(return_value=chat_completion("not json at all"))
    with pytest.raises(ScenarioError):
        await generator(max_json_attempts=1).generate(DOC, named_entities=NAMED)


@respx.mock
async def test_a_model_that_omits_a_required_field_raises(generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps({"event": "x"})))
    with pytest.raises(ScenarioError):
        await generator(max_json_attempts=1).generate(DOC, named_entities=NAMED)


# ==========================================================================
# Attribution — the property the whole phase exists for
# ==========================================================================


@respx.mock
async def test_a_genuine_quote_keeps_its_attribution_and_records_its_source(generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(scenario(
        seed_posts=[{"content": QUOTE, "attribution": "named_quote",
                     "speaker": "Councillor Jane Doe"}]))))
    result = await generator().generate(DOC, named_entities=NAMED)

    post = result.seed_posts[0]
    assert post.attribution == "named_quote"
    assert post.verified
    assert DOC[post.source_start:post.source_end].lower().replace("\n", " ") == QUOTE


@respx.mock
async def test_A_PARAPHRASE_IS_NOT_ATTRIBUTED_TO_A_REAL_PERSON(generator):
    """The model paraphrases constantly. This fired on the first real run."""
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(scenario(
        seed_posts=[{"content": "Jane Doe said the plan allows tall buildings everywhere",
                     "attribution": "named_quote", "speaker": "Councillor Jane Doe"}]))))
    result = await generator().generate(DOC, named_entities=NAMED)

    post = result.seed_posts[0]
    assert post.attribution == "broadcaster"
    assert post.speaker == ""
    assert "not in the source" in post.demoted_reason
    assert "tall buildings" in post.content, "demoted, not dropped"
    assert not post.verified


@respx.mock
async def test_a_quote_from_someone_the_document_never_names_is_demoted(generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(scenario(
        seed_posts=[{"content": QUOTE, "attribution": "named_quote",
                     "speaker": "Someone Not In The Document"}]))))
    result = await generator().generate(DOC, named_entities=NAMED)

    assert result.seed_posts[0].attribution == "broadcaster"
    assert "not an entity" in result.seed_posts[0].demoted_reason


@respx.mock
async def test_a_named_quote_without_a_speaker_is_demoted(generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(scenario(
        seed_posts=[{"content": QUOTE, "attribution": "named_quote"}]))))
    result = await generator().generate(DOC, named_entities=NAMED)
    assert result.seed_posts[0].attribution == "broadcaster"


@respx.mock
async def test_a_broadcaster_post_cannot_smuggle_in_a_speaker_or_offsets(generator):
    """Offsets without a quote to anchor them would still read as evidence."""
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(scenario(
        seed_posts=[{"content": "Council publishes draft policy.",
                     "attribution": "broadcaster", "speaker": "Councillor Jane Doe",
                     "source_start": 5, "source_end": 9}]))))
    result = await generator().generate(DOC, named_entities=NAMED)

    post = result.seed_posts[0]
    assert post.speaker == ""
    assert post.source_start is None and post.source_end is None


@respx.mock
async def test_a_broadcaster_cannot_take_a_named_organisations_name(generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(scenario(
        broadcaster={"name": "Riverbend Gazette"}))))
    result = await generator().generate(DOC, named_entities=NAMED)

    assert result.broadcaster.name != "Riverbend Gazette"
    assert result.broadcaster.name and result.broadcaster.handle


@respx.mock
async def test_a_document_naming_nobody_still_yields_a_scenario(generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(BASE)))
    result = await generator().generate(DOC, named_entities=[])
    assert result.event and result.seed_posts


# ==========================================================================
# The action space is not the model's to choose
# ==========================================================================


@respx.mock
async def test_a_model_supplied_action_space_cannot_break_generation(generator):
    """It is in the schema the model sees, so it will occasionally fill it in."""
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(scenario(
        action_space={"platform": "twitter", "actions": ["CREATE_POST"]}))))
    result = await generator().generate(DOC, named_entities=NAMED)

    assert "DO_NOTHING" in result.actions, "the platform default won, not the model"


@respx.mock
async def test_the_action_space_follows_the_requested_platform(generator):
    respx.post(CHAT).mock(return_value=chat_completion(json.dumps(BASE)))
    result = await generator().generate(DOC, named_entities=NAMED, platform="reddit")

    assert "CREATE_COMMENT" in result.actions
    assert "REPOST" not in result.actions


# ==========================================================================
# Persistence and review warnings
# ==========================================================================


def test_a_config_round_trips_through_disk(tmp_path):
    config = SimulationConfig.model_validate(BASE)
    assert SimulationConfig.load(config.save(tmp_path / "c.json")).model_dump() \
        == config.model_dump()


def test_the_summary_describes_the_run():
    summary = SimulationConfig.model_validate(BASE).summary()
    assert "twitter" in summary and "8 rounds" in summary and "seed post" in summary


def test_a_clean_scenario_warns_about_nothing():
    assert SimulationConfig.model_validate(BASE).warnings() == []


def test_an_over_length_post_warns_rather_than_failing():
    """A 400-character tweet is a quality problem, not a corrupt config."""
    long_post = "x" * (POST_LENGTH_LIMIT["twitter"] + 50)
    config = SimulationConfig.model_validate(scenario(
        seed_posts=[{"content": long_post}]))

    assert config.warnings(), "the operator is told at review"
    assert "280" in config.warnings()[0]


def test_the_same_post_is_fine_on_reddit():
    """The limit is per platform, not a blanket rule."""
    long_post = "x" * (POST_LENGTH_LIMIT["twitter"] + 50)
    config = SimulationConfig.model_validate(scenario(
        platform="reddit", seed_posts=[{"content": long_post}]))
    assert config.warnings() == []


def test_a_demotion_is_surfaced_as_a_review_warning():
    config = SimulationConfig.model_validate(scenario(
        seed_posts=[{"content": "Something someone did not say.",
                     "demoted_reason": "the quoted text is not in the source document"}]))
    assert any("reassigned to the broadcaster" in w for w in config.warnings())


def test_warnings_are_computed_not_stored(tmp_path):
    """A stored warning would outlive the problem it describes."""
    config = SimulationConfig.model_validate(scenario(
        seed_posts=[{"content": "x" * 400}]))
    path = config.save(tmp_path / "c.json")

    assert "warnings" not in json.loads(path.read_text())
    config.seed_posts[0].content = "Short now."
    assert config.warnings() == []


# ==========================================================================
# Against the real model
# ==========================================================================


@pytest.mark.integration
async def test_a_real_model_produces_a_config_that_validates(integration_config):
    """The only check here that can catch the model drifting off-schema.

    Everything above asserts against output I wrote. If `qwen2.5:14b` starts
    returning a shape the schema rejects, this is what notices.
    """
    generator = SimulationConfigGenerator(integration_config)
    try:
        config = await generator.generate(
            DOC, named_entities=NAMED, graph_id="pytest", platform="twitter", rounds=4)
    finally:
        await generator.aclose()

    assert config.event.strip()
    assert config.seed_posts
    assert config.broadcaster.name and config.broadcaster.handle
    assert 1 <= config.rounds <= 4
    assert all(1 <= e.round <= config.rounds for e in config.scheduled_events)
    assert all(not e.enabled for e in config.scheduled_events)
    assert config.actions

    # Re-validating what the generator returned proves it is genuinely a
    # config, not merely an object that happened to survive construction.
    SimulationConfig.model_validate(config.model_dump(), context={"trusted": True})


@pytest.mark.integration
async def test_a_real_model_never_leaves_an_unverified_quote_attributed(integration_config):
    """Whatever the model claims, every surviving quote must be checkable."""
    generator = SimulationConfigGenerator(integration_config)
    try:
        config = await generator.generate(DOC, named_entities=NAMED, rounds=4)
    finally:
        await generator.aclose()

    for post in config.seed_posts:
        if post.attribution == "named_quote":
            assert post.source_start is not None
            assert find_verbatim(DOC, post.content) == (post.source_start, post.source_end)
            assert post.speaker in NAMED
