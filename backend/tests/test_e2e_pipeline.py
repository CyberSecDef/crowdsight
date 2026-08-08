"""Phase 10 Step 1 — the whole product, once, against real local services.

upload → graph → profiles → config → a 3-agent/2-round simulation → report,
driven over HTTP against the running stack exactly as the UI drives it. Marked
`integration` and excluded from the fast suite; the spec is explicit that it is
required before a release.

**What this catches that nothing else does: the handovers.** Every stage
already has tests proving it works — that is not what this is for. It asserts
that each stage's output actually feeds the next: that the population traces
back to entities from the graph *this document* built, that the run is of
*that* population, and that the report's citations resolve to posts from *that*
run. A regression in report generation slipped through precisely there — every
stage passed its own tests while the join between two of them was broken.

**It is not a replacement for `test_simulation_smoke.py`.** That one runs
create → prepare → start → complete from a fake graph id, so it exercises the
engine without needing a graph built first, and it is the faster gate. This one
is the full chain. They fail for different reasons: the smoke test when the
engine breaks, this when the stages stop joining up.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

pytestmark = pytest.mark.integration

#: The API as the frontend sees it — the running stack, not a test client.
BASE = "http://localhost:5000/api"

#: Small on purpose. The spec asks for three agents and two rounds; this test
#: exists to prove the chain joins up, not to produce an interesting society.
AGENTS = 3
ROUNDS = 2

#: Generous: every stage here is real local inference on a 14b model.
STAGE_TIMEOUT = 900.0


@pytest.fixture(scope="module")
def http():
    import httpx

    with httpx.Client(base_url=BASE, timeout=300.0) as client:
        yield client


def poll(http, path: str, *, timeout: float = STAGE_TIMEOUT,
         settled=("succeeded", "failed", "awaiting_review")) -> dict[str, Any]:
    """Wait for a background task, and fail loudly with its own message."""
    deadline = time.monotonic() + timeout
    task: dict[str, Any] = {}
    while time.monotonic() < deadline:
        task = http.get(path).json()
        if task.get("status") in settled:
            return task
        time.sleep(2)
    pytest.fail(f"{path} did not settle within {timeout}s; last was {task}")


def test_THE_WHOLE_PIPELINE(http, council_text):
    """A document goes in one end and a grounded report comes out the other."""

    # ---- 1. Upload, and review the ontology before paying for extraction ---
    response = http.post("/graph/upload", files={
        "file": ("council.txt", council_text.encode())},
        data={"review_ontology": "true"})
    assert response.status_code == 202, response.text
    graph_id = response.json()["graph_id"]
    task_id = response.json()["task_id"]

    proposal = poll(http, f"/graph/status/{task_id}")
    assert proposal["status"] == "awaiting_review", proposal.get("error")
    assert proposal["progress"] == 0.5, "a parked task sits at half done"

    ontology = http.get(f"/graph/{graph_id}/ontology").json()
    assert ontology["entity_types"], "an ontology needs at least one entity type"

    # ---- 2. Approve it and let extraction build the graph -----------------
    started = http.post(f"/graph/{graph_id}/ontology", json=ontology)
    assert started.status_code == 202, started.text
    built = poll(http, f"/graph/status/{started.json()['task_id']}")
    assert built["status"] == "succeeded", built.get("error")

    entities = http.get(f"/graph/{graph_id}/entities").json()
    assert entities["total"] > 0, "extraction produced no entities"
    graph_entity_uuids = {item["uuid"] for item in entities["items"]}

    # Provenance lives on the detail endpoint, not the list: the list is built
    # for browsing and would carry a span of source text per row otherwise.
    detail = http.get(
        f"/graph/{graph_id}/entities/{entities['items'][0]['uuid']}").json()
    assert detail["provenance"], "an entity must trace back to source text"

    # ---- 3. A simulation from that graph ----------------------------------
    created = http.post("/simulation/create", json={
        "graph_id": graph_id, "platform": "twitter",
        "rounds": ROUNDS, "total_agents": AGENTS})
    assert created.status_code == 201, created.text
    sim_id = created.json()["sim_id"]

    prepared = http.post("/simulation/prepare", json={
        "sim_id": sim_id, "total_agents": AGENTS})
    assert prepared.status_code == 202, prepared.text
    population = poll(
        http, f"/simulation/prepare/status?task_id={prepared.json()['task_id']}")
    assert population["status"] in {"awaiting_review", "succeeded"}, \
        population.get("error")

    # ---- HANDOVER: the population came from *this* graph -------------------
    profiles = http.get(f"/simulation/{sim_id}/profiles").json()
    assert profiles["count"] == AGENTS, profiles["count"]

    named = [p for p in profiles["profiles"] if p["provenance"] == "named"]
    assert named, "no agent stands for anyone the document actually names"
    sources = {p["source_entity_uuid"] for p in named if p.get("source_entity_uuid")}
    assert sources, "a named agent must record the entity it came from"
    assert sources <= graph_entity_uuids, (
        "a named agent cites an entity that is not in the graph this document "
        f"built: {sources - graph_entity_uuids}")

    synthetic = [p for p in profiles["profiles"] if p["provenance"] == "synthetic"]
    assert all(not p.get("source_entity_uuid") for p in synthetic), \
        "a synthetic agent must not claim to come from a real entity"

    # ---- 4. The scenario the graph produced -------------------------------
    config = http.get(f"/simulations/{sim_id}/config").json()
    assert config["event"], "the population needs something to react to"
    assert config["graph_id"] == graph_id
    assert config["rounds"] == ROUNDS
    assert config["action_space"]["actions"], "agents with no actions do nothing"

    # ---- 5. Run it ---------------------------------------------------------
    launched = http.post("/simulation/start", json={"sim_id": sim_id})
    assert launched.status_code == 202, launched.text

    deadline = time.monotonic() + STAGE_TIMEOUT
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = http.get(f"/simulation/{sim_id}/run-status").json()
        if status["state"] in {"complete", "failed"}:
            break
        time.sleep(3)
    assert status["state"] == "complete", status
    assert status["rounds_completed"] == ROUNDS, status

    # ---- HANDOVER: the run is of *that* population ------------------------
    actions = http.get(f"/simulation/{sim_id}/actions?limit=200").json()
    assert actions["total"] > 0, "a completed run with no agent actions"

    population_ids = {p["user_id"] for p in profiles["profiles"]}
    actors = {a["user_id"] for a in actions["actions"] if a["population"]}
    assert actors, "no population agent acted"
    assert actors <= population_ids, (
        f"an action came from outside the population: {actors - population_ids}")

    posts = http.get(f"/simulation/{sim_id}/posts?limit=200").json()
    assert posts["total"] > 0, "a run that produced no posts"

    # ---- 6. A report on that run ------------------------------------------
    generating = http.post("/report/generate", json={"sim_id": sim_id})
    assert generating.status_code == 202, generating.text
    written = poll(http, f"/report/status/{generating.json()['task_id']}",
                   settled=("succeeded", "failed"))
    assert written["status"] == "succeeded", written.get("error")

    report_id = written["result"]["report_id"]
    report = http.get(f"/report/{report_id}").json()
    assert report["sim_id"] == sim_id
    assert report["executive_summary"], "a report with nothing in it"

    # ---- HANDOVER: every citation resolves to *this* run -------------------
    grounding = report["grounding"]
    assert grounding["checked"] > 0, "a report that cited nothing at all"

    # `unresolved` is the record of what the check *caught*, not a failure.
    # A local model does sometimes cite a post number it invented; the promise
    # Phase 8 makes is not that it never happens but that it never survives.
    # Asserting an empty list here was asserting the model's luck.
    if grounding["unresolved"]:
        assert grounding["dropped"], (
            "a citation did not resolve and yet no claim was dropped: "
            f"{grounding['unresolved']}")

    post_ids = {p["post_id"] for p in posts["posts"]}
    cited: set[int] = set()
    for section in ("dominant_narratives", "counter_narratives",
                    "influential_agents", "emergent_behaviour"):
        for claim in report.get(section) or []:
            cited.update((claim.get("citation") or {}).get("post_ids") or [])
    # The guarantee that matters: whatever survived into the report is
    # supported by evidence this run actually holds.
    assert cited <= post_ids, (
        f"a surviving claim cites posts that are not in this run: "
        f"{cited - post_ids}")

    if cited:
        # The citation link the UI offers must actually find them.
        found = http.get(f"/simulation/{sim_id}/posts",
                         params={"post_ids": ",".join(str(i) for i in sorted(cited))})
        assert found.json()["total"] == len(cited), \
            "a cited post cannot be fetched by the id the report gives"

    # ---- 7. Both exports render -------------------------------------------
    markdown = http.get(f"/report/{report_id}/export", params={"format": "markdown"})
    assert markdown.status_code == 200
    assert markdown.text.startswith("#"), markdown.text[:60]

    html = http.get(f"/report/{report_id}/export", params={"format": "html"})
    assert html.status_code == 200
    assert "<!DOCTYPE html>" in html.text
    assert "Verification" in html.text, \
        "the verification record must be published with the report"
