"""Phase 3 Step 7 — the HTTP surface.

Two layers. Route shapes, status codes and error handling run against a stub
runtime through the Flask test client: fast, no services, and they cover the
cases that matter most — an unknown `graph_id` must be a 404 and not a 500,
because a client cannot tell "that does not exist" from "we broke" when both
come back the same.

A smaller `integration` set drives a real upload through to a built graph,
because "upload returns a task id and the build actually completes" is exactly
what a stub cannot prove.
"""

from __future__ import annotations

import io
import time
from typing import Any

import pytest

from app.services.tasks import TaskStore
from app.storage.graph_storage import Page, Subgraph
from app.storage.search_service import ChunkHit, SearchHit

# --------------------------------------------------------------------------
# Stub runtime
# --------------------------------------------------------------------------

ENTITY = {
    "uuid": "e-1", "name": "Councillor Jane Doe", "normalised": "jane doe",
    "type": "Person", "aliases": ["Cllr Jane Doe"], "mention_count": 2,
    "inferred": False, "attributes": {"role": "chair"},
}
GRAPH = {
    "graph_id": "g-1", "filename": "council.txt", "domain": "Housing",
    "built_at": "2026-01-01T00:00:00Z", "char_count": 1792, "page_count": None,
    "entity_types": ["Person", "Organisation"], "relationship_types": ["WORKS_FOR"],
    "chunk_count": 2, "entity_count": 1,
}


class StubGraphs:
    def __init__(self, data_dir):
        self.data_dir = data_dir

    async def list_graphs(self):
        return [GRAPH]

    async def get_graph(self, graph_id):
        return GRAPH if graph_id == "g-1" else None

    async def get_entity(self, graph_id, uuid):
        return dict(ENTITY) if graph_id == "g-1" and uuid == "e-1" else None

    async def list_entities(self, graph_id, *, types=None, search=None, limit=50, offset=0):
        items = [] if types and "Person" not in types else [dict(ENTITY)]
        return Page(items=items, total=len(items), limit=limit, offset=offset)

    async def entity_types(self, graph_id):
        return [{"type": "Person", "count": 1}]

    async def list_relationships(self, graph_id, *, uuid=None, limit=100, offset=0):
        return Page(items=[], total=0, limit=limit, offset=offset)

    async def entity_chunks(self, graph_id, uuid):
        return [{"chunk_index": 0, "start": 0, "end": 20, "surface": "Cllr Jane Doe",
                 "text": "Councillor Jane Doe"}]

    async def neighbours(self, graph_id, uuid, *, depth=1, limit=500):
        return Subgraph(nodes=[dict(ENTITY)], edges=[], truncated=False)

    async def subgraph(self, graph_id, *, limit=500):
        return Subgraph(nodes=[dict(ENTITY)], edges=[], truncated=limit < 1000)


class StubSearch:
    async def search_entities(self, graph_id, query, *, limit=10, types=None):
        return [SearchHit(uuid="e-1", name="Councillor Jane Doe", type="Person",
                          score=1.0, matched_by="exact", mention_count=2)]

    async def search_chunks(self, graph_id, query, *, limit=5):
        return [ChunkHit(chunk_index=0, start=0, end=20, score=0.8, text="a passage")]


class StubBuilder:
    def __init__(self):
        self.deleted: list[str] = []

    async def delete(self, graph_id):
        self.deleted.append(graph_id)
        return 7


class StubRuntime:
    """Everything api/graph.py touches, minus the services."""

    def __init__(self, tmp_path, config):
        self.config = config
        self.data_dir = tmp_path / "graphs"
        self.tasks = TaskStore(tmp_path / "tasks.db")
        self.graphs = StubGraphs(self.data_dir)
        self.search = StubSearch()
        self.builder = StubBuilder()
        self.llm = None
        self.embeddings = None
        self.submitted: list[Any] = []
        self.runner = self

    def submit(self, task, job):
        # Record rather than run: these tests are about the HTTP contract.
        self.submitted.append((task, job))
        return task

    def run(self, coro, timeout=60.0):
        import asyncio

        return asyncio.run(coro)


@pytest.fixture
def client(tmp_path, config, monkeypatch):
    from app.main import create_app

    runtime = StubRuntime(tmp_path, config)
    monkeypatch.setattr("app.api.graph.get_runtime", lambda **_: runtime)
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        test_client.runtime = runtime  # type: ignore[attr-defined]
        yield test_client


DOC = b"Councillor Jane Doe spoke in favour of the housing policy on Tuesday."


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------


def test_upload_returns_identifiers_without_blocking(client):
    started = time.monotonic()
    response = client.post("/api/graph/upload",
                           data={"file": (__import__("io").BytesIO(DOC), "council.txt")},
                           content_type="multipart/form-data")
    elapsed = time.monotonic() - started

    assert response.status_code == 202
    body = response.get_json()
    assert body["graph_id"] and body["task_id"]
    assert body["poll"].endswith(body["task_id"])
    assert elapsed < 2.0
    assert len(client.runtime.submitted) == 1


def test_upload_with_review_flag_is_reported(client):
    import io

    response = client.post(
        "/api/graph/upload",
        data={"file": (io.BytesIO(DOC), "council.txt"), "review_ontology": "true"},
        content_type="multipart/form-data",
    )
    assert response.get_json()["review_ontology"] is True


def test_upload_without_a_file_is_400(client):
    response = client.post("/api/graph/upload", data={},
                           content_type="multipart/form-data")
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_the_refusal_says_no_parts_arrived_at_all(client):
    response = client.post("/api/graph/upload", data={},
                           content_type="multipart/form-data")
    assert "No file parts were present" in response.get_json()["error"]


def test_A_CALLER_SENDING_files_IS_TOLD_WHAT_IT_SENT(client):
    """`files` is the obvious guess and a real client made it.

    It is not accepted as an alias: a graph is built from exactly one document,
    so `files` would promise a multi-upload that silently discards everything
    after the first. But being told only what to send, and not what arrived,
    means guessing twice.
    """
    response = client.post(
        "/api/graph/upload",
        data={"files": (io.BytesIO(b"Council notes."), "notes.txt")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert "'file'" in error, "it must still say what to send"
    assert "files" in error, "and name what actually arrived"


def test_sending_several_files_is_refused_rather_than_silently_truncated(client):
    response = client.post(
        "/api/graph/upload",
        data={"files": [(io.BytesIO(b"one"), "a.txt"), (io.BytesIO(b"two"), "b.txt")]},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


@pytest.mark.parametrize("filename", ["notes.exe", "archive.zip"])
def test_disallowed_extension_is_400_not_a_failed_task(client, filename):
    """A rejected file should not become a task the client polls to discover."""
    import io

    response = client.post("/api/graph/upload",
                           data={"file": (io.BytesIO(DOC), filename)},
                           content_type="multipart/form-data")
    assert response.status_code == 400
    assert client.runtime.submitted == []


def test_empty_file_is_400(client):
    import io

    response = client.post("/api/graph/upload",
                           data={"file": (io.BytesIO(b""), "empty.txt")},
                           content_type="multipart/form-data")
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Task status
# --------------------------------------------------------------------------


def test_status_returns_the_documented_shape(client):
    task = client.runtime.tasks.create("graph.build", graph_id="g-1")
    body = client.get(f"/api/graph/status/{task.id}").get_json()
    assert set(body) >= {
        "id", "kind", "status", "stage", "progress", "message", "graph_id",
        "result", "error", "created_at", "updated_at",
    }


def test_unknown_task_is_404(client):
    assert client.get("/api/graph/status/t-nope").status_code == 404


def test_tasks_are_listable(client):
    client.runtime.tasks.create("graph.build", graph_id="g-1")
    body = client.get("/api/graph/tasks", query_string={"graph_id": "g-1"}).get_json()
    assert len(body["tasks"]) == 1


# --------------------------------------------------------------------------
# 404s, not 500s
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/graph/no-such-graph",
        "/api/graph/no-such-graph/entities",
        "/api/graph/no-such-graph/subgraph",
        "/api/graph/no-such-graph/ontology",
        "/api/graph/g-1/entities/no-such-entity",
    ],
)
def test_unknown_identifiers_are_404_with_json(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert "error" in response.get_json()


def test_delete_of_unknown_graph_is_404(client):
    assert client.delete("/api/graph/no-such-graph").status_code == 404


# --------------------------------------------------------------------------
# Query routes
# --------------------------------------------------------------------------


def test_list_graphs(client):
    assert client.get("/api/graph").get_json()["graphs"][0]["graph_id"] == "g-1"


def test_graph_metadata(client):
    body = client.get("/api/graph/g-1").get_json()
    assert body["graph_id"] == "g-1" and body["entity_count"] == 1


def test_entities_page_shape(client):
    body = client.get("/api/graph/g-1/entities").get_json()
    assert set(body) >= {"graph_id", "items", "total", "limit", "offset", "has_more"}
    assert body["items"][0]["name"] == "Councillor Jane Doe"


def test_entities_filter_by_type(client):
    empty = client.get("/api/graph/g-1/entities", query_string={"type": "Organisation"})
    assert empty.get_json()["items"] == []


def test_entity_detail_includes_provenance_and_relationships(client):
    """An entity a caller cannot trace to source is what this must not produce."""
    body = client.get("/api/graph/g-1/entities/e-1").get_json()
    assert body["uuid"] == "e-1"
    assert body["provenance"][0]["text"]
    assert isinstance(body["relationships"], list)


def test_entity_types_route(client):
    assert client.get("/api/graph/g-1/entity-types").get_json()["types"][0]["count"] == 1


def test_subgraph_shape(client):
    body = client.get("/api/graph/g-1/subgraph").get_json()
    assert set(body) >= {"graph_id", "nodes", "edges", "truncated"}


def test_neighbourhood_subgraph(client):
    body = client.get("/api/graph/g-1/subgraph",
                      query_string={"entity": "e-1", "depth": 2}).get_json()
    assert body["nodes"]


def test_search_shape_and_matched_by(client):
    body = client.get("/api/graph/g-1/search", query_string={"q": "Jane"}).get_json()
    assert body["entities"][0]["matched_by"] == "exact"
    assert "passages" not in body


def test_search_can_include_passages(client):
    body = client.get("/api/graph/g-1/search",
                      query_string={"q": "Jane", "passages": "1"}).get_json()
    assert body["passages"][0]["text"] == "a passage"


def test_empty_search_query_is_400(client):
    assert client.get("/api/graph/g-1/search", query_string={"q": " "}).status_code == 400


def test_delete_reports_what_it_removed(client):
    body = client.delete("/api/graph/g-1").get_json()
    assert body["deleted_nodes"] == 7
    assert client.runtime.builder.deleted == ["g-1"]


# --------------------------------------------------------------------------
# Live pipeline
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_real_upload_builds_a_graph_and_can_be_deleted(council_text):
    """The claim a stub cannot make: the build actually completes."""
    import httpx

    base = "http://localhost:5000/api/graph"
    with httpx.Client(timeout=180.0) as http:
        response = http.post(base + "/upload",
                             files={"file": ("council.txt", council_text.encode())})
        assert response.status_code == 202
        body = response.json()
        graph_id, task_id = body["graph_id"], body["task_id"]

        deadline = time.monotonic() + 600
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = http.get(f"{base}/status/{task_id}").json()
            if status["status"] in {"succeeded", "failed"}:
                break
            time.sleep(2)

        assert status["status"] == "succeeded", status.get("error")
        assert status["result"]["entities"] > 0
        assert status["progress"] == 1.0

        entities = http.get(f"{base}/{graph_id}/entities").json()
        assert entities["total"] > 0

        detail = http.get(
            f"{base}/{graph_id}/entities/{entities['items'][0]['uuid']}"
        ).json()
        assert detail["provenance"], "every entity must trace back to source text"

        assert http.delete(f"{base}/{graph_id}").status_code == 200
        assert http.get(f"{base}/{graph_id}").status_code == 404
