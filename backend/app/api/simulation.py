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
from app.services.simulation_manager import CapacityError
from app.services.simulation_store import SimulationNotFound, SimulationState
from app.services.tasks import TaskProgress, TaskStatus

logger = logging.getLogger(__name__)

bp = Blueprint("simulation", __name__, url_prefix="/api/simulations")

#: Enough names for the quote check to be meaningful without loading a whole
#: large graph into memory on every edit.
ENTITY_LIMIT = 500

#: Population size when the caller does not say. Small enough to finish in
#: minutes on one GPU; the spec's headline figure of 300 is an overnight job.
DEFAULT_AGENTS = 20

#: Share of the population drawn from people the document actually names.
DEFAULT_NAMED_RATIO = 0.25


def _error(message: str, status: int, **extra: Any):
    return jsonify({"error": message, **extra}), status


@bp.errorhandler(SimulationNotFound)
def _missing_simulation(exc: SimulationNotFound):
    return _error(str(exc), 404)


async def graph_context(graph_id: str) -> tuple[str, list[str]]:
    """The document and entity names an edit is verified against.

    Async, and awaited directly by the background jobs. A job already runs *on*
    the runner's event loop, so calling the synchronous ``runtime.run`` facade
    from inside one submits work to the loop that is waiting for it and
    deadlocks until the 60-second timeout fires. The sync wrapper below exists
    only for request handlers, which have no loop of their own.
    """
    if not graph_id:
        return "", []
    runtime = get_runtime()
    try:
        document = runtime.builder.load_document(graph_id)
    except Exception as exc:  # noqa: BLE001 - a missing document is not fatal here
        logger.warning("No document for graph %r: %s", graph_id, exc)
        document = ""
    page = await runtime.graphs.list_entities(graph_id, limit=ENTITY_LIMIT)
    names = [str(item.get("name", "")) for item in page.items]
    return document, [name for name in names if name]


def _graph_context(graph_id: str) -> tuple[str, list[str]]:
    """Blocking form, for Flask handlers only. Never call this from a job."""
    return get_runtime().run(graph_context(graph_id))


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
    document, named = await graph_context(graph_id)
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


# ==========================================================================
# Phase 6 Step 5 — the control API
#
# The spec names singular routes; the plural ones above came from Phase 5
# Step 3 and stay, because the edit-and-fork flow built on
# `PUT /api/simulations/<id>/config` has no equivalent in the spec's list.
# Where both reach the same data they read the same store.
# ==========================================================================

control = Blueprint("simulation_control", __name__, url_prefix="/api/simulation")

control.register_error_handler(SimulationNotFound, _missing_simulation)


def _body() -> dict[str, Any]:
    return request.get_json(silent=True) or {}


def _sim_id_from(payload: dict[str, Any]) -> str:
    sim_id = str(payload.get("sim_id") or "").strip()
    if not sim_id:
        raise SimulationNotFound("sim_id is required")
    return sim_id


# --------------------------------------------------------------------------
# Create and prepare
# --------------------------------------------------------------------------


@control.post("/create")
def create():
    """Reserve a simulation. Cheap: no inference, no population, no scenario.

    Separate from `prepare` because preparing costs minutes of local inference
    and an operator needs an id to watch it against before it starts.
    """
    runtime = get_runtime()
    payload = _body()
    graph_id = str(payload.get("graph_id") or "").strip()
    if not graph_id:
        return _error("graph_id is required", 400)
    if runtime.run(runtime.graphs.get_graph(graph_id)) is None:
        return _error(f"No graph {graph_id!r}", 404)

    platform = str(payload.get("platform") or "twitter").strip().lower()
    if platform not in {"twitter", "reddit"}:
        return _error(f"Unsupported platform {platform!r}", 400)

    try:
        rounds = _optional_int(payload, "rounds", minimum=1)
        total_agents = _optional_int(payload, "total_agents", minimum=1)
    except ValueError as exc:
        return _error(str(exc), 400)

    named_ratio = payload.get("named_ratio")
    if named_ratio is not None:
        try:
            named_ratio = float(named_ratio)
        except (TypeError, ValueError):
            return _error("named_ratio must be a number between 0 and 1", 400)
        if not 0.0 <= named_ratio <= 1.0:
            return _error("named_ratio must be between 0 and 1", 400)

    if total_agents and total_agents > runtime.config.MAX_AGENTS:
        return _error(
            f"total_agents {total_agents} exceeds MAX_AGENTS "
            f"({runtime.config.MAX_AGENTS})", 400)

    meta = runtime.sims.create_pending(
        graph_id=graph_id, platform=platform, rounds=rounds,
        total_agents=total_agents, named_ratio=named_ratio,
    )
    return jsonify({
        "sim_id": meta.sim_id,
        "graph_id": graph_id,
        "platform": platform,
        "state": meta.state,
        "prepared": False,
        "next": "/api/simulation/prepare",
    }), 201


def _optional_int(payload: dict[str, Any], name: str, *, minimum: int) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number") from None
    if number < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return number


async def prepare_job(
    progress: TaskProgress,
    *,
    sim_id: str,
    graph_id: str,
    platform: str,
    rounds: int | None,
    total_agents: int,
    named_ratio: float,
) -> dict[str, Any]:
    """Build the population and derive the scenario. The expensive half.

    Resumable by construction: Phase 4 Step 4 keys generation on a plan
    fingerprint, so an interrupted prepare finishes the agents it had not got
    to rather than paying for the whole population again.
    """
    from app.services.oasis_profiles import write_profiles
    from app.services.ontology_generator import Ontology
    from app.services.population import plan_population, sketch_population
    from app.services.profile_generator import EntityContext, ProfileGenerator
    from app.services.profile_job import generate_population

    runtime = get_runtime()
    progress.update(stage="prepare", progress=0.02, graph_id=graph_id,
                    message=f"Reading graph {graph_id}")
    document, named = await graph_context(graph_id)
    if not document:
        raise ScenarioError(f"Graph {graph_id!r} has no stored document.")

    entities = await runtime.graphs.list_entities(graph_id, limit=ENTITY_LIMIT)
    contexts = [
        EntityContext(
            uuid=str(item.get("uuid") or ""), name=str(item.get("name") or ""),
            type=str(item.get("type") or "Person"),
            attributes={k: str(v) for k, v in (item.get("attributes") or {}).items()},
        )
        for item in entities.items if item.get("name")
    ]

    ontology_path = runtime.builder.ontology_path(graph_id)
    ontology = Ontology.load(ontology_path) if ontology_path.is_file() else None

    progress.update(stage="sketch", progress=0.08,
                    message="Sketching the affected population")
    sketch = await sketch_population(ontology, document, runtime.llm)

    generator = ProfileGenerator(runtime.config, llm=runtime.llm)
    plan = plan_population(contexts, total=total_agents, sketch=sketch,
                           generator=generator, named_ratio=named_ratio)

    profiles_dir = runtime.sims.profiles_dir(sim_id)

    def report(done: int, total: int, name: str) -> None:
        progress.update(
            stage="personas", progress=0.10 + 0.65 * (done / max(total, 1)),
            message=f"Generated {done}/{total} personas ({name})")

    result = await generate_population(generator, plan, profiles_dir,
                                       progress=report)
    if not result.profiles:
        raise ScenarioError("No personas could be generated for this population.")

    progress.update(stage="profiles", progress=0.80,
                    message=f"Writing {len(result.profiles)} agent profile(s)")
    bundle = write_profiles(result.profiles, profiles_dir)

    progress.update(stage="scenario", progress=0.88,
                    message="Deriving the scenario")
    config = await runtime.scenarios.generate(
        document, ontology=ontology, named_entities=named, graph_id=graph_id,
        platform=platform, rounds=rounds,
    )
    runtime.sims.save_config(sim_id, config)

    payload = {
        "sim_id": sim_id,
        "graph_id": graph_id,
        "profiles": bundle.count,
        "named": bundle.named,
        "synthetic": bundle.synthetic,
        "failures": result.failures,
        "summary": config.summary(),
        "warnings": config.warnings(),
        "config": config.model_dump(),
    }
    progress.await_review(
        payload,
        f"Ready for review: {bundle.count} agent(s), {config.summary()}",
        stage="simulation_review",
    )
    return payload


@control.post("/prepare")
def prepare():
    """Generate the population and derive the scenario. Returns a task to poll."""
    runtime = get_runtime()
    payload = _body()
    sim_id = _sim_id_from(payload)
    meta = runtime.sims.load_meta(sim_id)

    if runtime.manager.is_running(sim_id):
        return _error(f"Simulation {sim_id} is running; stop it first", 409)
    if meta.locked:
        return _error(
            f"Simulation {sim_id} is {meta.state} and its configuration is frozen", 409)

    force = bool(payload.get("force"))
    if runtime.sims.prepared(sim_id) and not force:
        return jsonify({
            "sim_id": sim_id, "prepared": True, "task_id": None,
            "message": "Already prepared. Pass force=true to build it again.",
        }), 200

    request_payload = runtime.sims.request(sim_id)
    graph_id = str(payload.get("graph_id") or request_payload.get("graph_id")
                   or meta.graph_id or "")
    if not graph_id:
        return _error("This simulation has no graph_id to prepare from", 400)

    platform = str(payload.get("platform") or request_payload.get("platform")
                   or meta.platform or "twitter")
    rounds = payload.get("rounds") or request_payload.get("rounds")
    total_agents = int(payload.get("total_agents")
                       or request_payload.get("total_agents") or 0) \
        or min(runtime.config.MAX_AGENTS, DEFAULT_AGENTS)
    named_ratio = payload.get("named_ratio")
    if named_ratio is None:
        named_ratio = request_payload.get("named_ratio")
    named_ratio = DEFAULT_NAMED_RATIO if named_ratio is None else float(named_ratio)

    if total_agents > runtime.config.MAX_AGENTS:
        return _error(
            f"total_agents {total_agents} exceeds MAX_AGENTS "
            f"({runtime.config.MAX_AGENTS})", 400)

    if force:
        _discard_profiles(runtime.sims.profiles_dir(sim_id))

    task = runtime.tasks.create("simulation.prepare", graph_id=graph_id)
    runtime.runner.submit(task, lambda p: prepare_job(
        p, sim_id=sim_id, graph_id=graph_id, platform=platform,
        rounds=int(rounds) if rounds else None,
        total_agents=total_agents, named_ratio=named_ratio,
    ))
    return jsonify({
        "sim_id": sim_id,
        "task_id": task.id,
        "status": TaskStatus.RUNNING,
        "agents": total_agents,
        "poll": f"/api/simulation/prepare/status?task_id={task.id}",
    }), 202


def _discard_profiles(directory: Any) -> None:
    """Remove a previous population so generation genuinely starts over."""
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


@control.get("/prepare/status")
def prepare_status():
    """Poll a preparation task."""
    runtime = get_runtime()
    task_id = request.args.get("task_id", "").strip()
    if not task_id:
        return _error("task_id is required", 400)
    task = runtime.tasks.get(task_id)
    if task is None:
        return _error(f"No task {task_id!r}", 404)
    return jsonify(task.to_dict())


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@control.get("/list")
def list_all():
    runtime = get_runtime()
    metas = runtime.sims.list(graph_id=request.args.get("graph_id"))
    running = set(runtime.manager.running())
    return jsonify({"simulations": [
        {**meta.model_dump(),
         "prepared": runtime.sims.prepared(meta.sim_id),
         "running": meta.sim_id in running}
        for meta in metas
    ]})


@control.get("/<sim_id>")
def describe(sim_id: str):
    runtime = get_runtime()
    payload = runtime.sims.describe(sim_id)
    payload["running"] = runtime.manager.is_running(sim_id)
    return jsonify(payload)


@control.get("/<sim_id>/config")
def config_of(sim_id: str):
    runtime = get_runtime()
    if not runtime.sims.prepared(sim_id):
        return _error(f"Simulation {sim_id!r} has not been prepared yet", 409)
    return jsonify(runtime.sims.load_config(sim_id).model_dump())


@control.get("/<sim_id>/profiles")
def profiles_of(sim_id: str):
    """The population, from our own record rather than the lossy OASIS files."""
    import json as _json

    runtime = get_runtime()
    path = runtime.sims.profiles_dir(sim_id) / "profiles.json"
    if not path.is_file():
        return _error(f"Simulation {sim_id!r} has no population yet", 409)
    try:
        profiles = _json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return _error(f"Unreadable population file: {exc}", 500)

    provenance = str(request.args.get("provenance") or "").strip().lower()
    if provenance in {"named", "synthetic"}:
        profiles = [p for p in profiles if p.get("provenance") == provenance]
    return jsonify({"sim_id": sim_id, "count": len(profiles), "profiles": profiles})


@control.get("/<sim_id>/status")
def status_of(sim_id: str):
    """Live progress, straight from the worker over its control socket."""
    return jsonify(get_runtime().manager.status(sim_id))


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


@control.post("/start")
def start():
    """Start a prepared simulation, or resume one that failed part-way."""
    runtime = get_runtime()
    payload = _body()
    sim_id = _sim_id_from(payload)
    meta = runtime.sims.load_meta(sim_id)

    if not runtime.sims.prepared(sim_id):
        return _error(
            f"Simulation {sim_id!r} has no scenario yet; prepare it first", 409)
    if not (runtime.sims.profiles_dir(sim_id) / "profiles.json").is_file():
        return _error(
            f"Simulation {sim_id!r} has no population yet; prepare it first", 409)

    resuming = meta.state == SimulationState.FAILED
    try:
        record = runtime.manager.start(sim_id)
    except CapacityError as exc:
        return _error(str(exc), 409, budget=runtime.manager.budget())

    return jsonify({
        "sim_id": sim_id,
        "pid": record.pid,
        "concurrency": record.concurrency,
        "resumed": resuming,
        "state": SimulationState.RUNNING,
        "poll": f"/api/simulation/{sim_id}/status",
    }), 202


@control.post("/stop")
def stop():
    """Ask a run to stop at the next round boundary, then insist."""
    runtime = get_runtime()
    payload = _body()
    sim_id = _sim_id_from(payload)
    runtime.sims.load_meta(sim_id)

    timeout = payload.get("timeout")
    try:
        seconds = float(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        return _error("timeout must be a number of seconds", 400)

    outcome = runtime.manager.stop(
        sim_id, **({"timeout": seconds} if seconds is not None else {}))
    return jsonify({
        "sim_id": sim_id,
        "outcome": outcome,
        "state": runtime.sims.load_meta(sim_id).state,
    })


@control.get("/budget")
def budget():
    """Where the inference budget went. An operator asks this when a run crawls."""
    return jsonify(get_runtime().manager.budget())


# ==========================================================================
# Phase 7 Step 1 — run status and timeline
#
# All four answer from the run's own database, so a finished run reads exactly
# like a live one. Live worker fields enrich the status when there is a worker
# to ask, and are marked stale rather than withheld when it does not answer.
# ==========================================================================


def _reader(sim_id: str):
    """A reader for one run, after checking the simulation exists at all."""
    from app.services.run_reader import RunReader

    runtime = get_runtime()
    runtime.sims.load_meta(sim_id)          # raises SimulationNotFound
    return RunReader(runtime.sims.sim_dir(sim_id))


def _live_status(sim_id: str) -> dict[str, Any] | None:
    """The worker's own view, or None when there is no worker to ask."""
    runtime = get_runtime()
    try:
        if not runtime.manager.is_running(sim_id):
            return None
        return runtime.manager.status(sim_id)
    except Exception as exc:  # noqa: BLE001 - enrichment must never fail a poll
        logger.debug("No live status for %s: %s", sim_id, exc)
        return {"unreachable": str(exc)}


def _range_arg(name: str) -> int | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number") from None


@control.get("/<sim_id>/run-status")
def run_status(sim_id: str):
    """State, progress and cumulative action counts. Safe to poll."""
    runtime = get_runtime()
    meta = runtime.sims.load_meta(sim_id)
    total_rounds = (runtime.sims.load_config(sim_id).rounds
                    if runtime.sims.prepared(sim_id) else 0)
    reader = _reader(sim_id)
    return jsonify(reader.status(meta=meta, total_rounds=total_rounds,
                                 live=_live_status(sim_id)))


@control.get("/<sim_id>/run-status/detail")
def run_status_detail(sim_id: str):
    """The recent action log: what agents actually did, newest first."""
    from app.services.run_reader import RunNotReadable

    reader = _reader(sim_id)
    try:
        limit = int(request.args.get("limit") or 50)
    except ValueError:
        return _error("limit must be a whole number", 400)

    try:
        actions = reader.recent_actions(limit=limit)
    except RunNotReadable as exc:
        return _error(str(exc), 409)
    return jsonify({"sim_id": sim_id, "count": len(actions), "actions": actions})


@control.get("/<sim_id>/timeline")
def timeline(sim_id: str):
    """Per-round aggregates, optionally over a range of rounds."""
    reader = _reader(sim_id)
    try:
        first = _range_arg("from_round")
        last = _range_arg("to_round")
    except ValueError as exc:
        return _error(str(exc), 400)
    if first is not None and last is not None and first > last:
        return _error("from_round must not be greater than to_round", 400)

    rounds = reader.timeline(from_round=first, to_round=last)
    return jsonify({
        "sim_id": sim_id,
        "count": len(rounds),
        "from_round": first,
        "to_round": last,
        "rounds": rounds,
    })


@control.get("/<sim_id>/agent-stats")
def agent_stats(sim_id: str):
    """Per-agent activity across the run, paginated and sortable."""
    from app.services.run_reader import RunNotReadable

    reader = _reader(sim_id)
    try:
        limit = int(request.args.get("limit") or 100)
        offset = int(request.args.get("offset") or 0)
    except ValueError:
        return _error("limit and offset must be whole numbers", 400)

    try:
        return jsonify(reader.agent_stats(
            limit=limit, offset=offset,
            sort=str(request.args.get("sort") or "actions"),
            population_only=request.args.get("population_only") in
            {"1", "true", "yes"},
        ))
    except RunNotReadable as exc:
        return _error(str(exc), 409)


# ==========================================================================
# Phase 7 Step 2 — content access
#
# Paginated, filterable reads of what a run produced. Page sizes are capped in
# the reader rather than trusted from the query string: a large run holds tens
# of thousands of rows and `limit=999999` would otherwise be a way to ask the
# API to build one enormous response.
# ==========================================================================


def _paging() -> tuple[int, int, str]:
    """limit, offset and order, from the query string."""
    try:
        limit = int(request.args.get("limit") or 50)
        offset = int(request.args.get("offset") or 0)
    except ValueError:
        raise ValueError("limit and offset must be whole numbers") from None
    if limit < 1 or offset < 0:
        raise ValueError("limit must be at least 1 and offset at least 0")
    order = str(request.args.get("order") or "newest").lower()
    if order not in {"newest", "oldest"}:
        raise ValueError("order must be 'newest' or 'oldest'")
    return limit, offset, order


def _optional_int_arg(name: str) -> int | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number") from None


def _check_platform(sim_id: str) -> None:
    """A run has exactly one platform, so filtering by another is a caller bug.

    The spec lists `platform` among the filters, but OASIS's trace table has no
    platform column and a simulation is configured with a single one. Silently
    ignoring the parameter would hand back a full result set to a caller who
    believes they filtered it, so it is validated instead.
    """
    requested = str(request.args.get("platform") or "").strip().lower()
    if not requested:
        return
    runtime = get_runtime()
    actual = runtime.sims.load_meta(sim_id).platform
    if requested != actual:
        raise ValueError(
            f"This simulation runs on {actual!r}, not {requested!r}; every action "
            f"in a run is on the same platform")


@control.get("/<sim_id>/actions")
def actions(sim_id: str):
    """Agent actions, filtered by agent, round or action type."""
    from app.services.run_reader import RunNotReadable

    reader = _reader(sim_id)
    try:
        limit, offset, order = _paging()
        _check_platform(sim_id)
        agent = _optional_int_arg("agent")
        round_index = _optional_int_arg("round")
    except ValueError as exc:
        return _error(str(exc), 400)

    raw = request.args.get("action") or ""
    action_types = [part for part in raw.replace(" ", ",").split(",") if part]

    try:
        return jsonify(reader.actions(
            limit=limit, offset=offset, order=order, agent=agent,
            round_index=round_index, action_types=action_types,
            include_engine=request.args.get("include_engine") in
            {"1", "true", "yes"}))
    except RunNotReadable as exc:
        return _error(str(exc), 409)


@control.get("/<sim_id>/posts")
def posts(sim_id: str):
    """Posts, with author, round and the engagement each drew."""
    from app.services.run_reader import RunNotReadable

    reader = _reader(sim_id)
    try:
        limit, offset, order = _paging()
        _check_platform(sim_id)
        agent = _optional_int_arg("agent")
        round_index = _optional_int_arg("round")
        min_engagement = _optional_int_arg("min_engagement") or 0
    except ValueError as exc:
        return _error(str(exc), 400)

    try:
        return jsonify(reader.posts(
            limit=limit, offset=offset, order=order, agent=agent,
            round_index=round_index, min_engagement=min_engagement,
            population_only=request.args.get("population_only") in
            {"1", "true", "yes"}))
    except RunNotReadable as exc:
        return _error(str(exc), 409)


@control.get("/<sim_id>/comments")
def comments(sim_id: str):
    """Comments, optionally only those replying to one post."""
    from app.services.run_reader import RunNotReadable

    reader = _reader(sim_id)
    try:
        limit, offset, order = _paging()
        _check_platform(sim_id)
        post_id = _optional_int_arg("post_id")
        agent = _optional_int_arg("agent")
        round_index = _optional_int_arg("round")
    except ValueError as exc:
        return _error(str(exc), 400)

    try:
        return jsonify(reader.comments(
            limit=limit, offset=offset, order=order, post_id=post_id,
            agent=agent, round_index=round_index))
    except RunNotReadable as exc:
        return _error(str(exc), 409)


# ==========================================================================
# Phase 7 Step 3 — agent interviews
#
# The one endpoint that asks a population *why*. A single interview answers
# inline because it is an interactive probe; batch and all run as background
# tasks, because three hundred agents is three hundred completions and no HTTP
# request should be held open for that.
# ==========================================================================


def _interview_request() -> tuple[str, str, dict[str, Any]]:
    """sim_id, question and the rest, from the body."""
    payload = _body()
    sim_id = _sim_id_from(payload)
    question = str(payload.get("question") or "").strip()
    if not question:
        raise ValueError("question is required")
    return sim_id, question, payload


@control.post("/interview")
def interview():
    """Ask one agent a question, in character and with its accumulated memory."""
    from app.services.interview import (
        InterviewError,
        NoSuchAgent,
        SimulationNotLive,
        conduct,
        interviewable,
    )

    runtime = get_runtime()
    try:
        sim_id, question, payload = _interview_request()
    except ValueError as exc:
        return _error(str(exc), 400)
    runtime.sims.load_meta(sim_id)

    agent = payload.get("agent", payload.get("user_id"))
    if agent is None:
        return _error("agent is required; use /interview/all to ask everyone", 400)
    try:
        agents = [int(agent)]
    except (TypeError, ValueError):
        return _error("agent must be an agent id", 400)

    # Checked here, not by the worker: an unknown id is a caller mistake and
    # deserves a 404, not a round trip that comes back as a transport failure.
    known = interviewable(runtime.sims.sim_dir(sim_id))
    if agents[0] not in known:
        return _error(
            f"No interviewable agent {agents[0]} in this simulation "
            f"(ids {min(known)}-{max(known)})" if known else
            f"This simulation has no population to interview", 404)

    try:
        result = conduct(runtime.manager, sim_id, question, agents=agents)
    except SimulationNotLive as exc:
        return _error(str(exc), 409)
    except InterviewError as exc:
        return _error(str(exc), 502)

    answers = result.get("answers") or []
    if not answers:
        return _error(f"No agent {agent} in this simulation", 404)
    return jsonify({
        "sim_id": sim_id,
        "round": result.get("round"),
        **answers[0],
    })


async def interview_job(
    progress: TaskProgress,
    *,
    sim_id: str,
    question: str,
    agents: list[int] | None,
) -> dict[str, Any]:
    """Interview many agents in the background, reporting as answers arrive."""
    import asyncio

    from app.services.interview import conduct

    runtime = get_runtime()
    scope = "every agent" if agents is None else f"{len(agents)} agent(s)"
    progress.update(stage="interview", progress=0.05,
                    message=f"Asking {scope}: {question[:60]}")

    # The worker answers them concurrently; this call blocks until they are all
    # back, so it runs off the event loop to keep the runner responsive.
    result = await asyncio.to_thread(
        conduct, runtime.manager, sim_id, question, agents=agents)

    answers = result.get("answers") or []
    failed = [a for a in answers if a.get("error")]
    progress.update(stage="interview", progress=1.0,
                    message=f"{len(answers) - len(failed)} of {len(answers)} answered")
    return {
        "sim_id": sim_id,
        "question": question,
        "round": result.get("round"),
        "count": len(answers),
        "failed": len(failed),
        "answers": answers,
    }


def _submit_interview(sim_id: str, question: str, agents: list[int] | None):
    runtime = get_runtime()
    task = runtime.tasks.create("simulation.interview")
    runtime.runner.submit(task, lambda p: interview_job(
        p, sim_id=sim_id, question=question, agents=agents))
    return jsonify({
        "sim_id": sim_id,
        "task_id": task.id,
        "status": TaskStatus.RUNNING,
        "agents": "all" if agents is None else len(agents),
        "poll": f"/api/simulation/prepare/status?task_id={task.id}",
    }), 202


@control.post("/interview/batch")
def interview_batch():
    """Ask several named agents the same question. Returns a task to poll."""
    from app.services.interview import SimulationNotLive

    runtime = get_runtime()
    try:
        sim_id, question, payload = _interview_request()
    except ValueError as exc:
        return _error(str(exc), 400)
    runtime.sims.load_meta(sim_id)

    raw = payload.get("agents")
    if not isinstance(raw, list) or not raw:
        return _error("agents must be a non-empty list of agent ids", 400)
    try:
        agents = [int(value) for value in raw]
    except (TypeError, ValueError):
        return _error("agents must be a list of agent ids", 400)

    from app.services.interview import interviewable

    known = interviewable(runtime.sims.sim_dir(sim_id))
    unknown = [value for value in agents if value not in known]
    if unknown:
        return _error(f"No interviewable agent(s) {unknown} in this simulation", 404)

    if not runtime.manager.is_running(sim_id):
        return _error(str(SimulationNotLive(
            f"Simulation {sim_id} is not running; there is nobody to ask")), 409)
    return _submit_interview(sim_id, question, agents)


@control.post("/interview/all")
def interview_all():
    """Ask the whole population the same question. Returns a task to poll."""
    from app.services.interview import SimulationNotLive

    runtime = get_runtime()
    try:
        sim_id, question, _ = _interview_request()
    except ValueError as exc:
        return _error(str(exc), 400)
    runtime.sims.load_meta(sim_id)

    if not runtime.manager.is_running(sim_id):
        return _error(str(SimulationNotLive(
            f"Simulation {sim_id} is not running; there is nobody to ask")), 409)
    return _submit_interview(sim_id, question, None)


@control.post("/interview/history")
def interview_history():
    """Interviews already conducted. Readable long after the run has ended."""
    from app.services.interview import history

    runtime = get_runtime()
    payload = _body()
    sim_id = _sim_id_from(payload)
    runtime.sims.load_meta(sim_id)

    # Explicit None checks, not `or`: a JSON body carries a real 0, and
    # `0 or 50` silently becomes 50 rather than being refused as out of range.
    raw_limit = payload.get("limit")
    raw_offset = payload.get("offset")
    try:
        limit = 50 if raw_limit is None else int(raw_limit)
        offset = 0 if raw_offset is None else int(raw_offset)
    except (TypeError, ValueError):
        return _error("limit and offset must be whole numbers", 400)
    if limit < 1 or offset < 0:
        return _error("limit must be at least 1 and offset at least 0", 400)

    agent = payload.get("agent", payload.get("user_id"))
    if agent is not None:
        try:
            agent = int(agent)
        except (TypeError, ValueError):
            return _error("agent must be an agent id", 400)

    order = str(payload.get("order") or "newest").lower()
    if order not in {"newest", "oldest"}:
        return _error("order must be 'newest' or 'oldest'", 400)

    return jsonify(history(runtime.sims.sim_dir(sim_id), agent=agent,
                           limit=limit, offset=offset, order=order))


# ==========================================================================
# Phase 7 Step 4 — environment health
#
# `env-status` answers "is it alive and taking commands", quickly enough to be
# polled. `close-env` is `stop` plus the question you actually have before
# archiving a run: was anything left behind?
# ==========================================================================


@control.post("/env-status")
def env_status():
    """Whether the environment is alive and accepting commands."""
    runtime = get_runtime()
    payload = _body()
    sim_id = _sim_id_from(payload)
    # Validated here rather than left to the manager, as every other route
    # does: a 404 for an unknown simulation is the route's own contract.
    runtime.sims.load_meta(sim_id)

    timeout = payload.get("timeout")
    try:
        seconds = float(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        return _error("timeout must be a number of seconds", 400)
    if seconds is not None and seconds <= 0:
        return _error("timeout must be greater than zero", 400)

    return jsonify(runtime.manager.env_status(
        sim_id, **({"timeout": seconds} if seconds is not None else {})))


@control.post("/close-env")
def close_env():
    """Shut the environment down and confirm it was released."""
    runtime = get_runtime()
    payload = _body()
    sim_id = _sim_id_from(payload)
    runtime.sims.load_meta(sim_id)

    timeout = payload.get("timeout")
    try:
        seconds = float(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        return _error("timeout must be a number of seconds", 400)
    if seconds is not None and seconds <= 0:
        return _error("timeout must be greater than zero", 400)

    result = runtime.manager.close_env(
        sim_id, **({"timeout": seconds} if seconds is not None else {}))
    # 200 when the environment is genuinely gone, 207 when something survived:
    # a caller about to archive or delete the run needs to know the difference.
    return jsonify(result), 200 if result["closed"] else 207
