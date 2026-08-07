"""Phase 8 Step 3 — the report API.

Generation is a background task for the same reason preparation is: a report
scores every unscored post, then runs an agent loop against a 14b model, which
is minutes rather than seconds. Nothing else here is slow — a stored report is
a file, and Markdown and HTML are rendered from it on the way out.

The export deliberately does not offer a "raw" or unescaped mode. A report
carries agent-written post content, and an agent can be persuaded to write
anything; the HTML renderer escapes every value on the way in, and a bypass
would exist only to undo that.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, Response, jsonify, request

from app.services.report_store import ReportNotFound, ReportStore, render_html, render_markdown
from app.services.runtime import get_runtime
from app.services.simulation_store import SimulationNotFound
from app.services.tasks import TaskProgress, TaskStatus

logger = logging.getLogger(__name__)

bp = Blueprint("report", __name__, url_prefix="/api/report")


def _error(message: str, status: int, **extra: Any):
    return jsonify({"error": message, **extra}), status


@bp.errorhandler(ReportNotFound)
def _missing_report(exc: ReportNotFound):
    return _error(str(exc), 404)


@bp.errorhandler(SimulationNotFound)
def _missing_simulation(exc: SimulationNotFound):
    return _error(str(exc), 404)


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


async def generate_report_job(
    progress: TaskProgress,
    *,
    sim_id: str,
    tool_budget: int,
    reflection_rounds: int,
    rescore: bool,
) -> dict[str, Any]:
    """Score sentiment, write the report, verify it, store it."""
    from app.services.report_agent import ReportAgent
    from app.services.run_reader import RunReader
    from app.services.sentiment import SentimentScorer

    runtime = get_runtime()
    sim_dir = runtime.sims.sim_dir(sim_id)
    sim_config = (runtime.sims.load_config(sim_id)
                  if runtime.sims.prepared(sim_id) else None)

    progress.update(stage="sentiment", progress=0.05,
                    message="Scoring how the population felt")
    reader = RunReader(sim_dir)
    posts = reader.posts(limit=500, order="oldest")["posts"] if reader.exists else []

    scorer = SentimentScorer(runtime.config, llm=runtime.llm)
    scores = await scorer.score_run(
        sim_dir / "simulation.db", posts,
        event=getattr(sim_config, "event", "") or "", rescore=rescore,
        progress=lambda done, total: progress.update(
            progress=0.05 + 0.35 * (done / max(total, 1)),
            message=f"Scored {done}/{total} post(s)"),
    )

    agent = ReportAgent(runtime.config, llm=runtime.llm,
                        tool_budget=tool_budget,
                        reflection_rounds=reflection_rounds)
    report = await agent.generate(
        sim_dir, sim_config=sim_config, sentiment=scores,
        progress=lambda stage, fraction: progress.update(
            stage=stage, progress=0.4 + 0.55 * fraction,
            message=f"Report: {stage}"),
    )

    report_id = runtime.reports.save(report, sim_id=sim_id)
    grounding = report.grounding or {}
    return {
        "report_id": report_id,
        "sim_id": sim_id,
        "summary": report.executive_summary[:400],
        "citations_checked": grounding.get("checked", 0),
        "citations_resolved": grounding.get("resolved", 0),
        "claims_dropped": len(grounding.get("dropped") or []),
        "url": f"/api/report/{report_id}",
    }


@bp.post("/generate")
def generate():
    """Start a report. Returns a task to poll."""
    runtime = get_runtime()
    payload = request.get_json(silent=True) or {}
    sim_id = str(payload.get("sim_id") or "").strip()
    if not sim_id:
        return _error("sim_id is required", 400)

    meta = runtime.sims.load_meta(sim_id)
    if runtime.manager.is_running(sim_id):
        return _error(
            f"Simulation {sim_id} is still running; a report on a run in progress "
            f"would describe a moment rather than the run", 409)
    if not (runtime.sims.sim_dir(sim_id) / "simulation.db").is_file():
        return _error(f"Simulation {sim_id} has no run data to report on", 409)

    def bounded(name: str, default: int, ceiling: int) -> int:
        raw = payload.get(name)
        if raw is None:
            return default
        value = int(raw)
        if value < 0 or value > ceiling:
            raise ValueError(f"{name} must be between 0 and {ceiling}")
        return value

    try:
        from app.services.report_agent import (
            DEFAULT_REFLECTION_ROUNDS,
            DEFAULT_TOOL_BUDGET,
        )

        tool_budget = bounded("tool_budget", DEFAULT_TOOL_BUDGET, 20)
        reflection_rounds = bounded("reflection_rounds", DEFAULT_REFLECTION_ROUNDS, 5)
    except (TypeError, ValueError) as exc:
        return _error(str(exc) if "between" in str(exc)
                      else "tool_budget and reflection_rounds must be whole numbers",
                      400)

    task = runtime.tasks.create("report.generate", graph_id=meta.graph_id or None)
    runtime.runner.submit(task, lambda p: generate_report_job(
        p, sim_id=sim_id, tool_budget=tool_budget,
        reflection_rounds=reflection_rounds,
        rescore=bool(payload.get("rescore")),
    ))
    return jsonify({
        "sim_id": sim_id,
        "task_id": task.id,
        "status": TaskStatus.RUNNING,
        "poll": f"/api/report/status/{task.id}",
    }), 202


@bp.get("/status/<task_id>")
def status(task_id: str):
    """Poll a generation task."""
    task = get_runtime().tasks.get(task_id)
    if task is None:
        return _error(f"No task {task_id!r}", 404)
    return jsonify(task.to_dict())


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@bp.get("")
@bp.get("/")
def list_reports():
    runtime = get_runtime()
    return jsonify({"reports": runtime.reports.list(
        sim_id=request.args.get("sim_id"))})


@bp.get("/<report_id>")
def get_report(report_id: str):
    return jsonify(get_runtime().reports.load(report_id))


@bp.get("/<report_id>/export")
def export(report_id: str):
    """The report as Markdown or HTML, rendered from the stored JSON."""
    runtime = get_runtime()
    report = runtime.reports.load(report_id)

    fmt = str(request.args.get("format") or "markdown").lower()
    if fmt in {"md", "markdown"}:
        body, mime, extension = render_markdown(report), "text/markdown", "md"
    elif fmt == "html":
        body, mime, extension = render_html(report), "text/html", "html"
    else:
        return _error(f"Unsupported format {fmt!r}; use markdown or html", 400)

    response = Response(body, mimetype=f"{mime}; charset=utf-8")
    if request.args.get("download") in {"1", "true", "yes"}:
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{report_id}.{extension}"')
    return response


@bp.delete("/<report_id>")
def delete_report(report_id: str):
    runtime = get_runtime()
    if not runtime.reports.delete(report_id):
        return _error(f"No report {report_id!r}", 404)
    return jsonify({"report_id": report_id, "deleted": True})
