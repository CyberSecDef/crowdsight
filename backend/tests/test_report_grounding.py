"""Phase 8 Step 2 — every citation resolves to real data.

The spec is blunt about why this matters: a report that cannot be traced back
to simulated evidence is indistinguishable from the model's prior assumptions,
and would have read just as well had the run never happened. So the tests here
are mostly adversarial — they hand the checker reports that cite things which
do not exist and assert those claims do not survive.

Three distinctions the tests hold apart, because conflating them would hide the
thing worth knowing:

* **not citing** is unsupported — the model did not show its working;
* **citing wrongly** is fabricated evidence, which is worse;
* **dropping silently** is its own dishonesty, so a removed claim stays visible
  in the verification record.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.services.report_agent import (
    Citation,
    Finding,
    InfluentialAgent,
    Narrative,
    Report,
)
from app.services.report_grounding import (
    RunFacts,
    check_report,
    extract_references,
)
from app.services.simulation_persistence import RoundRecord, RunLedger

POPULATION = [
    {"user_id": 0, "username": "dawn_mercer", "name": "Dawn Mercer",
     "provenance": "synthetic"},
    {"user_id": 1, "username": "ray_nkemelu", "name": "Ray Nkemelu",
     "provenance": "named"},
]


@pytest.fixture
def run(tmp_path):
    """Posts 1-3, agents 0-2, rounds 0-1. Nothing else exists."""
    from oasis.social_platform.database import create_db

    directory = tmp_path / "sim-20260101-000000-abcdef"
    (directory / "profiles").mkdir(parents=True)
    (directory / "profiles" / "profiles.json").write_text(json.dumps(POPULATION))

    path = directory / "simulation.db"
    connection, _ = create_db(str(path))
    connection.close()

    ledger = RunLedger(path)
    ledger.ensure_schema()

    with sqlite3.connect(path) as db:
        for user_id, name in ((0, "dawn_mercer"), (1, "ray_nkemelu"), (2, "wire")):
            db.execute(
                "INSERT INTO user (user_id, agent_id, user_name, name, bio,"
                " created_at, num_followings, num_followers)"
                " VALUES (?, ?, ?, ?, '', '0', 0, 0)", (user_id, user_id, name, name))
        db.execute("INSERT INTO post (user_id, content, created_at)"
                   " VALUES (2, 'Council publishes a policy.', '0')")
    ledger.record_round(RoundRecord(round=0, invoked=1, acted=1))

    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO post (user_id, content, created_at)"
                   " VALUES (0, 'This will ruin the corridor.', '0')")
        db.execute("INSERT INTO post (user_id, content, created_at)"
                   " VALUES (1, 'I disagree.', '0')")
    ledger.record_round(RoundRecord(round=1, invoked=2, acted=2))
    return directory


@pytest.fixture
def facts(run):
    return RunFacts.load(run)


def narrative(label="Too fast", **citation):
    return Narrative(label=label, summary="s", citation=Citation(**citation))


def report_with(**sections):
    return Report(executive_summary="A summary.", **sections)


# --------------------------------------------------------------------------
# What the run actually contains
# --------------------------------------------------------------------------


def test_the_facts_are_read_from_the_run(facts):
    assert facts.post_ids == {1, 2, 3}
    assert facts.agent_ids == {0, 1, 2}
    assert facts.rounds == {0, 1}
    assert facts.usernames == {"dawn_mercer", "ray_nkemelu", "wire"}


def test_a_run_with_no_database_has_no_facts(tmp_path):
    assert RunFacts.load(tmp_path / "nothing").empty


# --------------------------------------------------------------------------
# Citations that resolve
# --------------------------------------------------------------------------


def test_a_fully_grounded_report_survives_intact(run):
    report = report_with(dominant_narratives=[
        narrative(post_ids=[1, 2], agent_ids=[0], rounds=[1])])
    checked, grounding = check_report(report, run)

    assert len(checked.dominant_narratives) == 1
    assert grounding.grounded is True
    assert grounding.checked == 4 and grounding.resolved == 4
    assert grounding.dropped == []


def test_every_kind_of_citation_is_checked(run):
    report = report_with(dominant_narratives=[
        narrative(post_ids=[1], agent_ids=[0], rounds=[0])])
    _, grounding = check_report(report, run)
    assert grounding.checked == 3


# --------------------------------------------------------------------------
# Citations that do not
# --------------------------------------------------------------------------


def test_A_CLAIM_CITING_A_POST_THAT_DOES_NOT_EXIST_IS_DROPPED(run):
    """The failure this whole step exists to catch."""
    report = report_with(dominant_narratives=[
        narrative("Invented", post_ids=[47])])
    checked, grounding = check_report(report, run)

    assert checked.dominant_narratives == []
    assert len(grounding.dropped) == 1
    assert grounding.dropped[0].claim == "Invented"
    assert "post 47" in grounding.dropped[0].reason


def test_A_DROPPED_CLAIM_STAYS_VISIBLE(run):
    """Deleting it silently would be its own dishonesty."""
    report = report_with(dominant_narratives=[narrative("Invented", post_ids=[47])])
    _, grounding = check_report(report, run)

    assert grounding.dropped[0].section == "dominant_narratives"
    assert grounding.dropped[0].claim
    assert grounding.dropped[0].reason
    assert grounding.grounded is False


@pytest.mark.parametrize(("kind", "citation"), [
    ("post", {"post_ids": [999]}),
    ("agent", {"agent_ids": [42]}),
    ("round", {"rounds": [9]}),
])
def test_any_kind_of_invented_reference_drops_the_claim(run, kind, citation):
    report = report_with(dominant_narratives=[narrative("x", **citation)])
    checked, grounding = check_report(report, run)

    assert checked.dominant_narratives == []
    assert grounding.unresolved[0].kind == kind


def test_one_bad_reference_is_enough_to_drop_a_claim(run):
    """A claim resting partly on invented evidence is not partly true."""
    report = report_with(dominant_narratives=[
        narrative("Mixed", post_ids=[1, 999], agent_ids=[0])])
    checked, grounding = check_report(report, run)

    assert checked.dominant_narratives == []
    assert grounding.resolved == 2, "the good references still resolved"


def test_a_good_claim_survives_beside_a_bad_one(run):
    report = report_with(dominant_narratives=[
        narrative("Real", post_ids=[1]),
        narrative("Invented", post_ids=[999]),
    ])
    checked, _ = check_report(report, run)
    assert [n.label for n in checked.dominant_narratives] == ["Real"]


@pytest.mark.parametrize("section", ["dominant_narratives", "counter_narratives"])
def test_both_narrative_sections_are_checked(run, section):
    report = report_with(**{section: [narrative("x", post_ids=[999])]})
    checked, _ = check_report(report, run)
    assert getattr(checked, section) == []


def test_an_influential_agent_citing_nothing_real_is_dropped(run):
    report = report_with(influential_agents=[InfluentialAgent(
        user_id=99, username="nobody", why="w",
        citation=Citation(agent_ids=[99]))])
    checked, grounding = check_report(report, run)

    assert checked.influential_agents == []
    assert grounding.dropped[0].section == "influential_agents"


def test_emergent_behaviour_is_checked_too(run):
    report = report_with(emergent_behaviour=[Finding(
        claim="Agents coordinated", citation=Citation(post_ids=[500]))])
    checked, _ = check_report(report, run)
    assert checked.emergent_behaviour == []


# --------------------------------------------------------------------------
# Claims that cite nothing at all
# --------------------------------------------------------------------------


def test_AN_UNCITED_CLAIM_IS_KEPT_BUT_RECORDED(run):
    """Not citing and citing wrongly are different failures."""
    report = report_with(dominant_narratives=[narrative("No evidence given")])
    checked, grounding = check_report(report, run)

    assert len(checked.dominant_narratives) == 1, "nothing about it is false"
    assert grounding.uncited_claims, "but a reader should know it rests on nothing"
    assert grounding.dropped == []


def test_uncited_and_fabricated_are_counted_separately(run):
    report = report_with(dominant_narratives=[
        narrative("Uncited"),
        narrative("Fabricated", post_ids=[999]),
    ])
    _, grounding = check_report(report, run)

    assert len(grounding.uncited_claims) == 1
    assert len(grounding.dropped) == 1


# --------------------------------------------------------------------------
# Prose
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("post 12 drove it", [("post", "12")]),
    ("Post #12 and post 13", [("post", "12"), ("post", "13")]),
    ("agent 4 led", [("agent", "4")]),
    ("user 7 replied", [("agent", "7")]),
    ("in round 3", [("round", "3")]),
    ("@dawn_mercer said", [("username", "dawn_mercer")]),
    ("post_id 5", [("post", "5")]),
])
def test_references_are_found_in_prose(text, expected):
    assert extract_references(text) == expected


@pytest.mark.parametrize("text", [
    "four-storey development along the corridor",
    "twenty-one days of consultation",
    "3 of the agents were quiet",
    "",
])
def test_ordinary_prose_is_not_read_as_citations(text):
    """Reading every number as a reference would drown the real findings."""
    assert extract_references(text) == []


def test_AN_INVENTED_REFERENCE_IN_THE_SUMMARY_IS_FLAGGED(run):
    """The summary is the part people actually read."""
    report = Report(executive_summary="Post 47 drove the backlash.")
    _, grounding = check_report(report, run)

    assert grounding.prose_unresolved
    assert grounding.prose_unresolved[0].value == "47"
    assert grounding.prose_unresolved[0].where == "executive_summary"
    assert grounding.grounded is False


def test_a_real_reference_in_prose_passes(run):
    report = Report(executive_summary="Post 2 drove the backlash in round 1.")
    _, grounding = check_report(report, run)

    assert grounding.prose_unresolved == []
    assert grounding.prose_references == 2


def test_an_invented_handle_in_prose_is_flagged(run):
    report = Report(executive_summary="@nobody_at_all led the opposition.")
    _, grounding = check_report(report, run)
    assert grounding.prose_unresolved[0].kind == "username"


def test_a_real_handle_in_prose_passes(run):
    report = Report(executive_summary="@dawn_mercer led the opposition.")
    _, grounding = check_report(report, run)
    assert grounding.prose_unresolved == []


@pytest.mark.parametrize("field_name", ["executive_summary", "sentiment_reading",
                                        "influence_propagation"])
def test_every_prose_field_is_scanned(run, field_name):
    report = Report(**{"executive_summary": "ok", field_name: "post 999 mattered"})
    _, grounding = check_report(report, run)
    assert any(r.where == field_name for r in grounding.prose_unresolved)


def test_prose_is_flagged_not_rewritten(run):
    """Editing the model's words would be a different kind of dishonesty."""
    report = Report(executive_summary="Post 47 drove the backlash.")
    checked, _ = check_report(report, run)
    assert checked.executive_summary == "Post 47 drove the backlash."


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


def test_the_summary_reads_as_a_verdict(run):
    report = report_with(dominant_narratives=[
        narrative("Real", post_ids=[1]),
        narrative("Fabricated", post_ids=[999]),
        narrative("Uncited"),
    ])
    _, grounding = check_report(report, run)
    text = grounding.summary()

    assert "resolved" in text
    assert "dropped" in text
    assert "cited nothing" in text


def test_a_run_with_no_data_verifies_nothing_rather_than_everything(tmp_path):
    """Absent evidence must not read as evidence of absence."""
    report = report_with(dominant_narratives=[narrative("x", post_ids=[999])])
    checked, grounding = check_report(report, tmp_path / "sim-20260101-000000-zzzzzz")

    assert grounding.empty_run is True
    assert len(checked.dominant_narratives) == 1, "nothing could be checked"
    assert "no data" in grounding.summary()


def test_the_record_serialises_for_publication(run):
    report = report_with(dominant_narratives=[narrative("x", post_ids=[999])])
    _, grounding = check_report(report, run)
    payload = grounding.model_dump()

    assert payload["dropped"][0]["claim"] == "x"
    assert json.dumps(payload), "must survive being written to disk"


# --------------------------------------------------------------------------
# Wired into the agent
# --------------------------------------------------------------------------


@pytest.fixture
def agent(config):
    from app.services.report_agent import ReportAgent
    from app.utils.llm_client import LLMClient
    from app.utils.retry import RetryPolicy

    return ReportAgent(config, llm=LLMClient(
        config, retry_policy=RetryPolicy(max_attempts=1)))


async def test_A_GENERATED_REPORT_IS_VERIFIED_BEFORE_IT_IS_RETURNED(agent, run):
    """A caller that forgot to check would publish unsupported claims."""
    import respx

    from tests.conftest import chat_completion

    payload = {"report": {
        "executive_summary": "Post 999 mattered.",
        "dominant_narratives": [
            {"label": "Real", "summary": "s",
             "citation": {"post_ids": [1], "rounds": [0]}},
            {"label": "Fabricated", "summary": "s",
             "citation": {"post_ids": [999]}},
        ],
        "caveats": [],
    }}

    with respx.mock:
        respx.post("http://ollama:11434/v1/chat/completions").mock(
            return_value=chat_completion(json.dumps(payload)))
        report = await agent.generate(run)

    assert [n.label for n in report.dominant_narratives] == ["Real"]
    assert report.grounding["dropped"][0]["claim"] == "Fabricated"
    assert report.grounding["prose_unresolved"], "the summary was scanned too"


async def test_verification_findings_reach_the_caveats(agent, run):
    import respx

    from tests.conftest import chat_completion

    payload = {"report": {
        "executive_summary": "A summary.",
        "dominant_narratives": [{"label": "Fabricated", "summary": "s",
                                 "citation": {"post_ids": [999]}}],
        "caveats": [],
    }}
    with respx.mock:
        respx.post("http://ollama:11434/v1/chat/completions").mock(
            return_value=chat_completion(json.dumps(payload)))
        report = await agent.generate(run)

    assert any("removed because they cited" in c for c in report.caveats), \
        "a reader must be told in the report itself, not only in the record"
