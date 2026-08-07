"""Phase 8 Step 3 — report storage, the API, and export.

Two properties here are worth more than the route shapes.

**Rendering escapes everything.** A report carries model-written prose *and*
agent-written post content, and an agent can be persuaded to write whatever a
prompt asks for. Text reaching a browser unescaped is how a simulation becomes
a cross-site scripting vector against the person reading its report.

**The verification section is always rendered.** A report that quietly dropped
three fabricated claims looks identical to one that never made any, and the
difference is exactly what a reader needs in order to judge the rest. Omitting
the section when clean would leave "verified and sound" indistinguishable from
"never verified".
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from typing import Any

import pytest

from app.services.report_store import (
    REPORT_FILE,
    ReportNotFound,
    ReportStore,
    new_report_id,
    render_html,
    render_markdown,
)
from app.services.tasks import TaskStore

REPORT = {
    "sim_id": "sim-20260101-000000-abcdef",
    "graph_id": "g-1",
    "event": "The council published a draft housing density policy.",
    "executive_summary": "Opposition formed quickly around the consultation period.",
    "sentiment_reading": "Negative throughout, softening slightly by the end.",
    "sentiment_trajectory": [
        {"round": 0, "posts": 3, "scored": 3, "mean_score": -0.43,
         "stances": {"opposed": 2, "neutral": 1}},
        {"round": 1, "posts": 5, "scored": 4, "mean_score": -0.32,
         "stances": {"opposed": 3, "neutral": 1}},
        {"round": 2, "posts": 3, "scored": 0, "mean_score": None, "stances": {}},
    ],
    "dominant_narratives": [{
        "label": "Too fast", "summary": "The consultation is too short.",
        "support": "Carried by dawn_mercer.",
        "citation": {"post_ids": [4, 7], "agent_ids": [0], "rounds": [1]}}],
    "counter_narratives": [{
        "label": "Housing is needed", "summary": "Supply matters more.",
        "support": "One voice.", "citation": {"post_ids": [9], "agent_ids": [1]}}],
    "influential_agents": [{
        "user_id": 0, "username": "dawn_mercer",
        "why": "Wrote the most reposted post.",
        "citation": {"post_ids": [4], "rounds": [1]}}],
    "influence_propagation": "One post was reposted twice and drew the only comment.",
    "emergent_behaviour": [{
        "claim": "Agents converged on the consultation window",
        "detail": "Three of four posts mentioned it.",
        "citation": {"post_ids": [4, 7, 9]}}],
    "caveats": ["This run had 3 agent(s)."],
    "grounding": {
        "checked": 9, "resolved": 9, "unresolved": [], "uncited_claims": [],
        "dropped": [], "prose_references": 0, "prose_unresolved": [],
        "empty_run": False},
}


@pytest.fixture
def store(tmp_path):
    return ReportStore(tmp_path / "reports")


@pytest.fixture
def saved(store):
    return store.save(dict(REPORT), sim_id=REPORT["sim_id"])


# --------------------------------------------------------------------------
# Identity and storage
# --------------------------------------------------------------------------


def test_report_ids_sort_chronologically():
    early = new_report_id(now=datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc),
                          rng=random.Random(1))
    late = new_report_id(now=datetime(2026, 3, 14, 17, 0, tzinfo=timezone.utc),
                         rng=random.Random(1))
    assert early < late
    assert early.startswith("rep-20260314-090000-")


def test_two_reports_on_one_run_coexist(store):
    """A run can reasonably be reported on more than once."""
    first = store.save(dict(REPORT), sim_id="sim-1")
    second = store.save(dict(REPORT), sim_id="sim-1")
    assert first != second
    assert len(store.list(sim_id="sim-1")) == 2


@pytest.mark.parametrize("bad", ["../../etc/passwd", "rep-1", "",
                                 "rep-20260314-090000-ZZZZZZ",
                                 "rep-20260314-090000-abcdef/../.."])
def test_A_BOGUS_ID_CANNOT_ESCAPE_THE_DIRECTORY(store, bad):
    """report_id arrives from a URL path segment."""
    with pytest.raises(ReportNotFound):
        store.report_dir(bad)


def test_a_saved_report_round_trips(store, saved):
    loaded = store.load(saved)
    assert loaded["executive_summary"] == REPORT["executive_summary"]
    assert loaded["report_id"] == saved
    assert loaded["generated_at"]


def test_the_report_lands_where_the_spec_says(store, saved):
    assert (store.base_dir / saved / REPORT_FILE).is_file()


def test_a_half_written_report_is_never_visible(store, saved):
    assert list(store.base_dir.rglob("*.tmp")) == []


def test_an_unknown_report_is_reported_not_invented(store):
    with pytest.raises(ReportNotFound):
        store.load("rep-20260314-090000-abcdef")


def test_listing_is_newest_first(store):
    ids = [store.save(dict(REPORT), sim_id="sim-1") for _ in range(3)]
    assert [r["report_id"] for r in store.list()] == sorted(ids, reverse=True)


def test_listing_carries_the_verification_summary(store, saved):
    entry = store.list()[0]
    assert entry["citations_resolved"] == 9
    assert entry["claims_dropped"] == 0


def test_listing_filters_by_simulation(store):
    store.save(dict(REPORT), sim_id="sim-1")
    store.save(dict(REPORT), sim_id="sim-2")
    assert len(store.list(sim_id="sim-2")) == 1


def test_stray_directories_are_ignored(store, saved):
    (store.base_dir / "not-a-report").mkdir()
    assert len(store.list()) == 1


def test_a_report_can_be_deleted(store, saved):
    assert store.delete(saved) is True
    assert not store.exists(saved)
    assert store.delete(saved) is False


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def test_markdown_has_every_section(saved, store):
    text = render_markdown(store.load(saved))
    for heading in ("# Simulation report", "## Executive summary",
                    "## Sentiment trajectory", "## Dominant narratives",
                    "## Counter-narratives", "## Influential agents",
                    "## Emergent behaviour", "## Caveats", "## Verification"):
        assert heading in text, f"{heading} missing"


def test_EVIDENCE_SITS_WITH_THE_CLAIM_IT_SUPPORTS(saved, store):
    """A reader checking a claim should not have to hunt for its basis."""
    text = render_markdown(store.load(saved))
    body = text.split("### Too fast")[1].split("##")[0]
    assert "*Evidence:*" in body
    assert "posts 4, 7" in body and "agent 0" in body and "round 1" in body


def test_a_claim_with_no_evidence_says_so(store):
    payload = dict(REPORT)
    payload["dominant_narratives"] = [
        {"label": "Unsupported", "summary": "s", "citation": {}}]
    text = render_markdown(payload)
    assert "No evidence cited" in text


def test_the_sentiment_table_shows_what_each_round_rests_on(saved, store):
    text = render_markdown(store.load(saved))
    assert "| 0 | -0.43 | 3/3 |" in text
    assert "| 1 | -0.32 | 4/5 |" in text, "a partly-scored round must show it"


def test_an_unscored_round_renders_as_unknown_not_zero(saved, store):
    text = render_markdown(store.load(saved))
    assert "| 2 | — | 0/3 |" in text


def test_a_report_with_nothing_in_it_still_renders():
    text = render_markdown({"executive_summary": ""})
    assert "## Verification" in text
    assert "None" in text


def test_markdown_ends_with_a_newline(saved, store):
    assert render_markdown(store.load(saved)).endswith("\n")


# --------------------------------------------------------------------------
# The verification section
# --------------------------------------------------------------------------


def test_THE_VERIFICATION_SECTION_IS_RENDERED_EVEN_WHEN_CLEAN(saved, store):
    """Otherwise "verified and sound" reads exactly like "never verified"."""
    text = render_markdown(store.load(saved))
    assert "## Verification" in text
    assert "9 of 9 citation(s) resolved" in text
    assert "No fabricated references were found" in text


def test_dropped_claims_are_named_in_the_document(store):
    payload = dict(REPORT)
    payload["grounding"] = {**REPORT["grounding"], "checked": 3, "resolved": 2,
                            "dropped": [{"section": "dominant_narratives",
                                         "claim": "Invented",
                                         "reason": "post 47 does not exist"}]}
    text = render_markdown(payload)
    assert "1 claim(s) were removed" in text
    assert "Invented" in text and "post 47 does not exist" in text


def test_invented_prose_references_are_named(store):
    payload = dict(REPORT)
    payload["grounding"] = {**REPORT["grounding"],
                            "prose_unresolved": [{"where": "executive_summary",
                                                  "kind": "post", "value": "47"}]}
    text = render_markdown(payload)
    assert "do not exist in this run" in text
    assert "executive_summary: post 47" in text


def test_an_unverified_report_says_it_was_not_verified():
    text = render_markdown({"executive_summary": "x"})
    assert "not verified against its run" in text


def test_a_run_with_no_data_says_nothing_could_be_checked():
    payload = {"executive_summary": "x", "grounding": {"empty_run": True}}
    assert "no citation could be checked" in render_markdown(payload)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------


def test_html_is_a_standalone_document(saved, store):
    text = render_html(store.load(saved))
    assert text.startswith("<!DOCTYPE html>")
    assert "<style>" in text, "must not need an external stylesheet"
    assert text.rstrip().endswith("</html>")


def test_html_has_every_section(saved, store):
    text = render_html(store.load(saved))
    for heading in ("Executive summary", "Sentiment trajectory",
                    "Dominant narratives", "Counter-narratives",
                    "Influential agents", "Emergent behaviour", "Caveats",
                    "Verification"):
        assert f">{heading}<" in text, f"{heading} missing"


@pytest.mark.parametrize("field", ["executive_summary", "sentiment_reading",
                                   "influence_propagation", "event"])
def test_AGENT_WRITTEN_TEXT_CANNOT_INJECT_SCRIPT(store, field):
    """A report carries agent-written content; an agent writes what it is asked to."""
    payload = dict(REPORT)
    payload[field] = "<script>alert('xss')</script> and <img src=x onerror=1>"
    text = render_html(payload)

    assert "<script>alert" not in text
    assert "&lt;script&gt;" in text
    assert "onerror=1>" not in text


def test_a_narrative_label_cannot_inject(store):
    payload = dict(REPORT)
    payload["dominant_narratives"] = [{
        "label": "<img src=x onerror=alert(1)>", "summary": "</p><script>bad()</script>",
        "citation": {"post_ids": [4]}}]
    text = render_html(payload)

    # The payload survives as visible text, which is correct and harmless; what
    # matters is that no tag or attribute of it reaches the parser.
    assert "<script>bad()" not in text
    assert "<img src=x" not in text
    assert "&lt;img src=x onerror=alert(1)&gt;" in text
    assert "&lt;script&gt;bad()" in text


def test_a_username_cannot_inject(store):
    payload = dict(REPORT)
    payload["influential_agents"] = [{
        "user_id": 0, "username": "<svg onload=alert(1)>", "why": "w",
        "citation": {}}]
    text = render_html(payload)

    assert "<svg" not in text, "no tag may reach the parser"
    assert "&lt;svg onload=alert(1)&gt;" in text, "it should still be readable"


def test_a_dropped_claim_cannot_inject_through_the_verification_section(store):
    payload = dict(REPORT)
    payload["grounding"] = {**REPORT["grounding"],
                            "dropped": [{"section": "s",
                                         "claim": "<script>x</script>",
                                         "reason": "r"}]}
    assert "<script>x</script>" not in render_html(payload)


def test_html_shows_evidence_with_each_claim(saved, store):
    text = render_html(store.load(saved))
    assert 'class="evidence"' in text
    assert "posts 4, 7" in text


def test_html_marks_an_uncited_claim(store):
    payload = dict(REPORT)
    payload["dominant_narratives"] = [{"label": "x", "summary": "s", "citation": {}}]
    assert 'class="uncited"' in render_html(payload)


# ==========================================================================
# The API
# ==========================================================================


class StubManager:
    def __init__(self):
        self.running: set[str] = set()

    def is_running(self, sim_id):
        return sim_id in self.running


class StubRuntime:
    def __init__(self, tmp_path, config, sims, reports):
        self.config = config
        self.tasks = TaskStore(tmp_path / "tasks.db")
        self.sims = sims
        self.reports = reports
        self.manager = StubManager()
        self.llm = None
        self.submitted: list[Any] = []
        self.runner = self

    def submit(self, task, job):
        self.submitted.append((task, job))
        return task

    def run(self, coro, timeout=60.0):
        import asyncio

        return asyncio.run(coro)


@pytest.fixture
def client(tmp_path, config, monkeypatch, store):
    from app.main import create_app
    from app.services.simulation_config_generator import SimulationConfig
    from app.services.simulation_store import SimulationStore

    sims = SimulationStore(tmp_path / "sims")
    meta = sims.create(SimulationConfig.model_validate({
        "graph_id": "g-1", "event": "e", "rounds": 2,
        "broadcaster": {"name": "Wire"}, "seed_posts": [{"content": "c"}]}))
    (sims.sim_dir(meta.sim_id) / "simulation.db").write_bytes(b"")

    runtime = StubRuntime(tmp_path, config, sims, store)
    monkeypatch.setattr("app.api.report.get_runtime", lambda **_: runtime)
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        test_client.runtime = runtime  # type: ignore[attr-defined]
        test_client.sim_id = meta.sim_id  # type: ignore[attr-defined]
        yield test_client


def test_generation_is_async(client):
    response = client.post("/api/report/generate", json={"sim_id": client.sim_id})
    body = response.get_json()

    assert response.status_code == 202
    assert body["task_id"]
    assert body["poll"].startswith("/api/report/status/")
    assert len(client.runtime.submitted) == 1


def test_generation_needs_a_simulation(client):
    assert client.post("/api/report/generate", json={}).status_code == 400


def test_generating_for_an_unknown_simulation_is_a_404(client):
    response = client.post("/api/report/generate",
                           json={"sim_id": "sim-20260101-000000-999999"})
    assert response.status_code == 404


def test_A_RUNNING_SIMULATION_CANNOT_BE_REPORTED_ON(client):
    """A report on a run in progress describes a moment, not the run."""
    client.runtime.manager.running.add(client.sim_id)
    response = client.post("/api/report/generate", json={"sim_id": client.sim_id})

    assert response.status_code == 409
    assert "still running" in response.get_json()["error"]


def test_a_simulation_with_no_run_data_is_refused(client, tmp_path):
    from app.services.simulation_config_generator import SimulationConfig

    meta = client.runtime.sims.create(SimulationConfig.model_validate({
        "graph_id": "g-1", "event": "e", "rounds": 2,
        "broadcaster": {"name": "Wire"}, "seed_posts": [{"content": "c"}]}))
    response = client.post("/api/report/generate", json={"sim_id": meta.sim_id})

    assert response.status_code == 409
    assert "no run data" in response.get_json()["error"]


@pytest.mark.parametrize("payload", [{"tool_budget": -1}, {"tool_budget": 99},
                                     {"reflection_rounds": 50},
                                     {"tool_budget": "lots"}])
def test_nonsense_budgets_are_refused(client, payload):
    response = client.post("/api/report/generate",
                           json={"sim_id": client.sim_id, **payload})
    assert response.status_code == 400


def test_status_polling_works(client):
    task_id = client.post("/api/report/generate",
                          json={"sim_id": client.sim_id}).get_json()["task_id"]
    body = client.get(f"/api/report/status/{task_id}").get_json()
    assert body["id"] == task_id


def test_status_on_an_unknown_task_is_a_404(client):
    assert client.get("/api/report/status/nope").status_code == 404


def test_a_report_can_be_fetched(client, saved):
    body = client.get(f"/api/report/{saved}").get_json()
    assert body["executive_summary"] == REPORT["executive_summary"]


def test_reports_can_be_listed(client, saved):
    body = client.get("/api/report").get_json()
    assert [r["report_id"] for r in body["reports"]] == [saved]


def test_an_unknown_report_is_a_404(client):
    assert client.get("/api/report/rep-20260101-000000-abcdef").status_code == 404


def test_a_bogus_report_id_is_a_404_not_a_500(client):
    assert client.get("/api/report/../../etc/passwd").status_code in (404, 308)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def test_markdown_export(client, saved):
    response = client.get(f"/api/report/{saved}/export")
    assert response.status_code == 200
    assert "text/markdown" in response.headers["Content-Type"]
    assert response.get_data(as_text=True).startswith("# Simulation report")


def test_html_export(client, saved):
    response = client.get(f"/api/report/{saved}/export?format=html")
    assert response.status_code == 200
    assert "text/html" in response.headers["Content-Type"]
    assert response.get_data(as_text=True).startswith("<!DOCTYPE html>")


def test_export_defaults_to_markdown(client, saved):
    assert "text/markdown" in client.get(
        f"/api/report/{saved}/export").headers["Content-Type"]


def test_an_unsupported_format_is_refused(client, saved):
    response = client.get(f"/api/report/{saved}/export?format=pdf")
    assert response.status_code == 400
    assert "markdown or html" in response.get_json()["error"]


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_export_can_be_downloaded(client, saved, fmt):
    response = client.get(f"/api/report/{saved}/export?format={fmt}&download=1")
    assert saved in response.headers["Content-Disposition"]


def test_exporting_an_unknown_report_is_a_404(client):
    assert client.get(
        "/api/report/rep-20260101-000000-abcdef/export").status_code == 404


def test_a_report_can_be_deleted_over_http(client, saved):
    assert client.delete(f"/api/report/{saved}").status_code == 200
    assert client.get(f"/api/report/{saved}").status_code == 404
