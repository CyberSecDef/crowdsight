"""Phase 5 Step 3 — the operator's review-and-edit surface for a scenario.

Deliberately shaped like the ontology approval flow in ``api/graph.py``: a
proposal is generated, written to disk, and left for a human. The same rule
applies here as there — an operator's edit goes through exactly the validation
the generated version did, so review can improve a scenario but cannot bypass
the guarantees the generator enforces.

The one addition is a fork. Ontology approval happens once, before anything is
built; a simulation config is reviewable *and* has a run attached to it, so an
edit arriving after the run started cannot be applied in place without
falsifying the report. It lands in a new simulation instead.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from app.services.runtime import get_runtime
from app.services.simulation_config_generator import ScenarioError
from app.services.simulation_store import SimulationNotFound
from app.services.tasks import TaskProgress, TaskStatus

logger = logging.getLogger(__name__)

bp = Blueprint("simulation", __name__, url_prefix="/api/simulations")

#: Enough names for the quote check to be meaningful without loading a whole
#: large graph into memory on every edit.
ENTITY_LIMIT = 500


def _error(message: str, status: int, **extra: Any):
    return jsonify({"error": message, **extra}), status


@bp.errorhandler(SimulationNotFound)
def _missing_simulation(exc: SimulationNotFound):
    return _error(str(exc), 404)


def _graph_context(graph_id: str) -> tuple[str, list[str]]:
    """The document and entity names an edit is verified against."""
    if not graph_id:
        return "", []
    runtime = get_runtime()
    try:
        document = runtime.builder.load_document(graph_id)
    except Exception as exc:  # noqa: BLE001 - a missing document is not fatal here
        logger.warning("No document for graph %r: %s", graph_id, exc)
        document = ""
    page = runtime.run(runtime.graphs.list_entities(graph_id, limit=ENTITY_LIMIT))
    names = [str(item.get("name", "")) for item in page.items]
    return document, [name for name in names if name]


# --------------------------------------------------------------------------
# Generate
# --------------------------------------------------------------------------


async def derive_scenario_job(
    progress: TaskProgress,
    *,
    graph_id: str,
    platform: str,
    rounds: int | None,
) -> dict[str, Any]:
    """Derive a scenario from a built graph and park it for review."""
    runtime = get_runtime()
    progress.update(stage="scenario", progress=0.1, graph_id=graph_id,
                    message=f"Reading graph {graph_id}")
    document, named = _graph_context(graph_id)
    if not document:
        raise ScenarioError(f"Graph {graph_id!r} has no stored document to derive from.")

    progress.update(stage="scenario", progress=0.3,
                    message=f"Deriving a {platform} scenario from {len(named)} entities")
    config = await runtime.scenarios.generate(
        document, named_entities=named, graph_id=graph_id,
        platform=platform, rounds=rounds,
    )

    meta = runtime.sims.create(config)
    result = {
        "sim_id": meta.sim_id,
        "graph_id": graph_id,
        "summary": config.summary(),
        "meta": meta.model_dump(),
        "config": config.model_dump(),
    }
    progress.await_review(
        result,
        f"Scenario ready for review: {config.summary()}",
        stage="scenario_review",
    )
    return result


@bp.post("")
@bp.post("/")
def create_simulation():
    """Derive a scenario from a graph. Returns a task to poll."""
    runtime = get_runtime()
    payload = request.get_json(silent=True) or {}
    graph_id = str(payload.get("graph_id") or "").strip()
    if not graph_id:
        return _error("graph_id is required", 400)
    if runtime.run(runtime.graphs.get_graph(graph_id)) is None:
        return _error(f"No graph {graph_id!r}", 404)

    platform = str(payload.get("platform") or "twitter").strip().lower()
    if platform not in {"twitter", "reddit"}:
        return _error(f"Unsupported platform {platform!r}", 400)

    rounds = payload.get("rounds")
    if rounds is not None:
        try:
            rounds = int(rounds)
        except (TypeError, ValueError):
            return _error("rounds must be a whole number", 400)
        if rounds < 1:
            return _error("rounds must be at least 1", 400)

    task = runtime.tasks.create("simulation.scenario", graph_id=graph_id)
    runtime.runner.submit(task, lambda p: derive_scenario_job(
        p, graph_id=graph_id, platform=platform, rounds=rounds,
    ))
    return jsonify({
        "graph_id": graph_id,
        "task_id": task.id,
        "status": TaskStatus.RUNNING,
        "poll": f"/api/graph/status/{task.id}",
    }), 202


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------


@bp.get("")
@bp.get("/")
def list_simulations():
    runtime = get_runtime()
    metas = runtime.sims.list(graph_id=request.args.get("graph_id"))
    return jsonify({"simulations": [m.model_dump() for m in metas]})


@bp.get("/<sim_id>")
def get_simulation(sim_id: str):
    return jsonify(get_runtime().sims.describe(sim_id))


@bp.get("/<sim_id>/config")
def get_config(sim_id: str):
    return jsonify(get_runtime().sims.load_config(sim_id).model_dump())


@bp.put("/<sim_id>/config")
def update_config(sim_id: str):
    """Accept an edited scenario.

    The edit is re-verified against the source document exactly as generated
    output is: a quote that is not in the text is demoted to the broadcaster
    rather than believed, and ``changes`` reports every correction so nothing is
    altered silently. Editing a started simulation forks it — the response says
    which id the edit actually landed in.
    """
    runtime = get_runtime()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        return _error("A config body is required", 400)

    meta = runtime.sims.load_meta(sim_id)
    document, named = _graph_context(meta.graph_id)

    try:
        result = runtime.sims.update_config(
            sim_id, payload, document=document, named_entities=named,
        )
    except ValueError as exc:
        return _error(f"Invalid config: {exc}", 400)

    if result.changes:
        logger.info("Edit to %s corrected: %s", sim_id, "; ".join(result.changes))
    return jsonify(result.to_dict()), 201 if result.forked else 200
