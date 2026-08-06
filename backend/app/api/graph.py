"""HTTP surface for building and querying knowledge graphs.

Uploads never block. A graph build takes tens of seconds at best, so the route
validates the file, creates a task, and returns ``202`` with a ``graph_id`` and
a ``task_id`` to poll.

Errors are typed rather than generic. An unknown ``graph_id`` is a 404, a
malformed upload is a 400, and neither is a 500 — a client cannot tell "you
asked for something that does not exist" from "we broke" if both come back the
same way.
"""

from __future__ import annotations

import logging
import uuid as uuidlib
from typing import Any

from flask import Blueprint, jsonify, request

from app.services.graph_builder import GraphNotFound
from app.services.ingest import (
    build_graph_job,
    extract_and_build_job,
    propose_ontology_job,
)
from app.services.ontology_generator import Ontology
from app.services.runtime import get_runtime
from app.services.tasks import TaskStatus
from app.storage.graph_storage import ontology_path
from app.utils.file_parser import FileParseError

logger = logging.getLogger(__name__)

bp = Blueprint("graph", __name__, url_prefix="/api/graph")

TRUTHY = {"1", "true", "yes", "on"}


def _error(message: str, status: int, **extra: Any):
    return jsonify({"error": message, **extra}), status


@bp.errorhandler(FileParseError)
def _bad_upload(exc: FileParseError):
    return _error(str(exc), 400)


@bp.errorhandler(GraphNotFound)
def _missing_graph(exc: GraphNotFound):
    return _error(str(exc), 404)


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


@bp.post("/upload")
def upload():
    """Accept a document and start building its graph.

    Returns 202 with the identifiers to poll. Pass ``review_ontology=true`` to
    stop after the ontology so it can be edited before extraction runs — that
    is the expensive stage, and a wrong ontology wastes all of it.
    """
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return _error("No file was uploaded. Send multipart/form-data with a 'file' part.", 400)

    data = uploaded.read()
    runtime = get_runtime()
    graph_id = request.form.get("graph_id") or "g-" + uuidlib.uuid4().hex[:10]
    review = str(request.form.get("review_ontology", "")).lower() in TRUTHY

    # Validate before creating a task: a rejected file should be a 400 now, not
    # a failed task the client has to poll for.
    try:
        from app.utils.file_parser import parse_bytes

        parse_bytes(data, uploaded.filename, runtime.config)
    except FileParseError as exc:
        return _error(str(exc), 400)

    kind = "graph.ontology" if review else "graph.build"
    task = runtime.tasks.create(kind, graph_id=graph_id)

    if review:
        runtime.runner.submit(task, lambda p: propose_ontology_job(
            p, graph_id=graph_id, data=data, filename=uploaded.filename,
            config=runtime.config, llm=runtime.llm, data_dir=runtime.data_dir,
        ))
    else:
        runtime.runner.submit(task, lambda p: build_graph_job(
            p, graph_id=graph_id, data=data, filename=uploaded.filename,
            config=runtime.config, llm=runtime.llm, embeddings=runtime.embeddings,
            builder=runtime.builder, data_dir=runtime.data_dir,
        ))

    return jsonify({
        "graph_id": graph_id,
        "task_id": task.id,
        "status": TaskStatus.RUNNING,
        "review_ontology": review,
        "poll": f"/api/graph/status/{task.id}",
    }), 202


@bp.get("/status/<task_id>")
def task_status(task_id: str):
    task = get_runtime().tasks.get(task_id)
    if task is None:
        return _error(f"No task {task_id!r}", 404)
    return jsonify(task.to_dict())


@bp.get("/tasks")
def list_tasks():
    runtime = get_runtime()
    tasks = runtime.tasks.list(
        graph_id=request.args.get("graph_id"), limit=_int_arg("limit", 50)
    )
    return jsonify({"tasks": [t.to_dict() for t in tasks]})


# --------------------------------------------------------------------------
# Ontology review
# --------------------------------------------------------------------------


@bp.get("/<graph_id>/ontology")
def get_ontology(graph_id: str):
    path = ontology_path(graph_id, get_runtime().data_dir)
    if not path.is_file():
        return _error(f"No ontology for graph {graph_id!r}", 404)
    return jsonify(Ontology.load(path).model_dump())


@bp.post("/<graph_id>/ontology")
def approve_ontology(graph_id: str):
    """Accept an edited ontology and start extraction.

    The body is the ontology to use; omit it to accept the proposal unchanged.
    Edits go through the same validation as a generated ontology, so an
    operator cannot introduce a type name the graph could not store.
    """
    runtime = get_runtime()
    path = ontology_path(graph_id, runtime.data_dir)
    if not path.is_file():
        return _error(f"No ontology for graph {graph_id!r}", 404)

    payload = request.get_json(silent=True)
    if payload:
        try:
            ontology = Ontology.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            return _error(f"Invalid ontology: {exc}", 400)
        ontology.save(path)

    task = runtime.tasks.create("graph.extract", graph_id=graph_id)
    runtime.runner.submit(task, lambda p: extract_and_build_job(
        p, graph_id=graph_id, config=runtime.config, llm=runtime.llm,
        embeddings=runtime.embeddings, builder=runtime.builder,
        data_dir=runtime.data_dir,
    ))
    return jsonify({
        "graph_id": graph_id,
        "task_id": task.id,
        "status": TaskStatus.RUNNING,
        "poll": f"/api/graph/status/{task.id}",
    }), 202


# --------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------


@bp.get("")
@bp.get("/")
def list_graphs():
    runtime = get_runtime()
    return jsonify({"graphs": runtime.run(runtime.graphs.list_graphs())})


@bp.get("/<graph_id>")
def get_graph(graph_id: str):
    runtime = get_runtime()
    graph = runtime.run(runtime.graphs.get_graph(graph_id))
    if graph is None:
        return _error(f"No graph {graph_id!r}", 404)
    return jsonify(graph)


@bp.get("/<graph_id>/entities")
def list_entities(graph_id: str):
    runtime = get_runtime()
    if runtime.run(runtime.graphs.get_graph(graph_id)) is None:
        return _error(f"No graph {graph_id!r}", 404)

    types = request.args.getlist("type") or None
    page = runtime.run(runtime.graphs.list_entities(
        graph_id, types=types, search=request.args.get("q"),
        limit=_int_arg("limit", 50), offset=_int_arg("offset", 0),
    ))
    return jsonify({
        "graph_id": graph_id,
        "items": page.items,
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "has_more": page.has_more,
    })


@bp.get("/<graph_id>/entities/<uuid>")
def get_entity(graph_id: str, uuid: str):
    """One entity, with the passages that produced it.

    Provenance is included by default: an entity a caller cannot trace back to
    the source is exactly what this system is supposed not to produce.
    """
    runtime = get_runtime()
    entity = runtime.run(runtime.graphs.get_entity(graph_id, uuid))
    if entity is None:
        return _error(f"No entity {uuid!r} in graph {graph_id!r}", 404)
    entity["provenance"] = runtime.run(runtime.graphs.entity_chunks(graph_id, uuid))
    entity["relationships"] = runtime.run(
        runtime.graphs.list_relationships(graph_id, uuid=uuid, limit=100)
    ).items
    return jsonify(entity)


@bp.get("/<graph_id>/entity-types")
def entity_types(graph_id: str):
    runtime = get_runtime()
    return jsonify({"types": runtime.run(runtime.graphs.entity_types(graph_id))})


@bp.get("/<graph_id>/relationships")
def relationships(graph_id: str):
    runtime = get_runtime()
    page = runtime.run(runtime.graphs.list_relationships(
        graph_id, uuid=request.args.get("entity"),
        limit=_int_arg("limit", 100), offset=_int_arg("offset", 0),
    ))
    return jsonify({
        "graph_id": graph_id, "items": page.items, "total": page.total,
        "limit": page.limit, "offset": page.offset, "has_more": page.has_more,
    })


@bp.get("/<graph_id>/subgraph")
def subgraph(graph_id: str):
    """Nodes and edges for visualisation, capped.

    With ``entity`` it is a neighbourhood to ``depth`` hops; without, the whole
    graph up to ``limit`` nodes. Either way the response says whether it was
    truncated, so a UI can tell the user rather than silently showing part.
    """
    runtime = get_runtime()
    if runtime.run(runtime.graphs.get_graph(graph_id)) is None:
        return _error(f"No graph {graph_id!r}", 404)

    entity = request.args.get("entity")
    limit = _int_arg("limit", 500)
    if entity:
        result = runtime.run(runtime.graphs.neighbours(
            graph_id, entity, depth=_int_arg("depth", 1), limit=limit
        ))
    else:
        result = runtime.run(runtime.graphs.subgraph(graph_id, limit=limit))
    return jsonify({
        "graph_id": graph_id,
        "nodes": result.nodes,
        "edges": result.edges,
        "truncated": result.truncated,
    })


@bp.get("/<graph_id>/search")
def search(graph_id: str):
    runtime = get_runtime()
    query = request.args.get("q", "")
    if not query.strip():
        return _error("Provide a query with ?q=", 400)

    hits = runtime.run(runtime.search.search_entities(
        graph_id, query, limit=_int_arg("limit", 10),
        types=request.args.getlist("type") or None,
    ), timeout=120)
    response: dict[str, Any] = {
        "graph_id": graph_id, "query": query,
        "entities": [h.to_dict() for h in hits],
    }
    if str(request.args.get("passages", "")).lower() in TRUTHY:
        chunks = runtime.run(
            runtime.search.search_chunks(graph_id, query, limit=_int_arg("passages_limit", 5)),
            timeout=120,
        )
        response["passages"] = [c.to_dict() for c in chunks]
    return jsonify(response)


@bp.delete("/<graph_id>")
def delete_graph(graph_id: str):
    runtime = get_runtime()
    if runtime.run(runtime.graphs.get_graph(graph_id)) is None:
        return _error(f"No graph {graph_id!r}", 404)
    deleted = runtime.run(runtime.builder.delete(graph_id))
    return jsonify({"graph_id": graph_id, "deleted_nodes": deleted})
