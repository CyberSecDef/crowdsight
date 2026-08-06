"""Phase 2 Step 4 — against a live Neo4j.

Marked ``integration``: run with ``pytest -m integration`` after
``docker compose up -d neo4j``. These do not skip when the database is absent.
They are already opt-in, and skipping an opt-in test turns an explicit request
to verify something into a silent no-op.

Isolation is by ``graph_id`` namespace rather than by database, because
Community Edition serves exactly one. The ``storage`` fixture deletes
everything in its namespace afterwards.

The source-audit tests are the exception: they read files, need no server, and
so run in the default suite.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest

from app.storage import neo4j_schema as schema
from app.storage.neo4j_storage import (
    IdentifierError,
    StorageError,
    audit_cypher_sources,
    escape_identifier,
)

DIM = 768


def vector(seed: int, dim: int = DIM) -> list[float]:
    return [((seed * 37 + i) % 100) / 100.0 for i in range(dim)]


async def make_entity(store, graph_ns, **fields):
    values = {
        "uuid": uuid.uuid4().hex,
        "name": "Ada Lovelace",
        "type": "Person",
        "gid": graph_ns,
        "emb": vector(1),
    }
    values.update(fields)
    await store.write(
        "CREATE (e:Entity {uuid:$uuid, name:$name, type:$type, graph_id:$gid, "
        "embedding:$emb})",
        **values,
    )
    return values["uuid"]


# --------------------------------------------------------------------------
# Connectivity
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_connects_and_reports_server_info(storage):
    info = await storage.server_info()
    assert info.get("version")
    assert info.get("edition")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_schema_creation_is_idempotent(storage):
    """It runs on every startup; a second run must be a no-op, not an error."""
    first = await schema.apply_schema(storage)
    second = await schema.apply_schema(storage)
    assert first.summary() == second.summary()
    assert len(first.constraints) == len(schema.CONSTRAINTS)
    assert len(first.indexes) == len(schema.INDEXES)


@pytest.mark.integration
async def test_constraint_and_indexes_exist_on_the_server(storage):
    await schema.apply_schema(storage)
    constraints = await storage.read("SHOW CONSTRAINTS YIELD name RETURN name")
    assert any(row["name"] == "entity_uuid_unique" for row in constraints)

    indexes = await storage.read("SHOW INDEXES YIELD name, type RETURN name, type")
    names = {row["name"]: row["type"] for row in indexes}
    for expected, _ in schema.INDEXES:
        assert expected in names


@pytest.mark.integration
async def test_vector_index_is_native_when_supported(storage):
    await schema.apply_schema(storage)
    indexes = await storage.read("SHOW INDEXES YIELD name, type RETURN name, type")
    names = {row["name"]: row["type"] for row in indexes}
    if schema.VECTOR_INDEX_NAME in names:
        assert names[schema.VECTOR_INDEX_NAME] == "VECTOR"
        assert await schema.supports_vector_index(storage)


@pytest.mark.integration
async def test_uniqueness_constraint_is_enforced_by_the_server(storage, graph_ns):
    """Deduplication in Phase 3 relies on this, and would fail silently without it."""
    await schema.apply_schema(storage)
    existing = await make_entity(storage, graph_ns)
    with pytest.raises(StorageError):
        await storage.write(
            "CREATE (e:Entity {uuid:$uuid, name:$name, graph_id:$gid})",
            uuid=existing, name="duplicate", gid=graph_ns,
        )


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_read_update_delete_round_trip(storage, graph_ns):
    node = await make_entity(storage, graph_ns)

    rows = await storage.read(
        "MATCH (e:Entity {uuid:$uuid}) RETURN e.name AS name, e.type AS type, "
        "size(e.embedding) AS dim",
        uuid=node,
    )
    assert rows[0]["name"] == "Ada Lovelace"
    assert rows[0]["dim"] == DIM

    await storage.write(
        "MATCH (e:Entity {uuid:$uuid}) SET e.type = $type", uuid=node, type="Engineer"
    )
    rows = await storage.read(
        "MATCH (e:Entity {uuid:$uuid}) RETURN e.type AS type", uuid=node
    )
    assert rows[0]["type"] == "Engineer"

    await storage.write("MATCH (e:Entity {uuid:$uuid}) DETACH DELETE e", uuid=node)
    rows = await storage.read(
        "MATCH (e:Entity {uuid:$uuid}) RETURN count(e) AS n", uuid=node
    )
    assert rows[0]["n"] == 0


@pytest.mark.integration
async def test_batched_write_persists_every_row(storage, graph_ns):
    rows = [
        {"uuid": uuid.uuid4().hex, "name": f"Person {i}", "type": "Person",
         "gid": graph_ns, "emb": vector(i)}
        for i in range(25)
    ]
    cypher = (
        "UNWIND $rows AS row MERGE (e:Entity {uuid: row.uuid}) "
        "SET e.name = row.name, e.type = row.type, e.graph_id = row.gid, "
        "e.embedding = row.emb"
    )
    await storage.run_batch(cypher, rows)
    count = await storage.read(
        "MATCH (e:Entity {graph_id:$gid}) RETURN count(e) AS n", gid=graph_ns
    )
    assert count[0]["n"] == 25

    await storage.run_batch(cypher, rows)
    count = await storage.read(
        "MATCH (e:Entity {graph_id:$gid}) RETURN count(e) AS n", gid=graph_ns
    )
    assert count[0]["n"] == 25, "MERGE batch should be idempotent"


@pytest.mark.integration
async def test_empty_batch_is_a_no_op(storage):
    assert await storage.run_batch("UNWIND $rows AS r RETURN r", []) == []


# --------------------------------------------------------------------------
# Parameterisation
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_injection_payload_is_stored_as_data(storage, graph_ns):
    """A document naming an entity like this is exactly what we ingest."""
    payload = "'); MATCH (n) DETACH DELETE n //"
    before = (await storage.read("MATCH (e:Entity) RETURN count(e) AS n"))[0]["n"]
    node = await make_entity(storage, graph_ns, name=payload)
    after = (await storage.read("MATCH (e:Entity) RETURN count(e) AS n"))[0]["n"]

    assert after == before + 1
    rows = await storage.read(
        "MATCH (e:Entity {uuid:$uuid}) RETURN e.name AS name", uuid=node
    )
    assert rows[0]["name"] == payload


@pytest.mark.integration
async def test_missing_parameter_is_refused(storage):
    with pytest.raises(ValueError, match="uuid"):
        await storage.read("MATCH (e:Entity {uuid:$uuid}) RETURN e")


# --------------------------------------------------------------------------
# Similarity search
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_both_similarity_modes_agree(storage, graph_ns):
    """Callers must never have to branch on which mode ran."""
    await schema.apply_schema(storage)
    rows = [
        {"uuid": uuid.uuid4().hex, "name": f"Person {i}", "type": "Person",
         "gid": graph_ns, "emb": vector(i)}
        for i in range(12)
    ]
    await storage.run_batch(
        "UNWIND $rows AS row MERGE (e:Entity {uuid: row.uuid}) "
        "SET e.name = row.name, e.type = row.type, e.graph_id = row.gid, "
        "e.embedding = row.emb",
        rows,
    )

    probe = vector(3)
    native = await schema.similarity_search(
        storage, probe, graph_id=graph_ns, limit=5, use_index=True
    )
    manual = await schema.similarity_search(
        storage, probe, graph_id=graph_ns, limit=5, use_index=False
    )

    assert len(native) == len(manual) == 5
    assert set(native[0]) == set(manual[0]) == {"uuid", "name", "type", "score"}
    assert native[0]["uuid"] == manual[0]["uuid"]
    assert all(native[i]["score"] >= native[i + 1]["score"] for i in range(4))
    assert abs(manual[0]["score"] - 1.0) < 1e-4


# --------------------------------------------------------------------------
# Unit-level: identifiers and the source audit (no server needed)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Person", "Organisation", "_Private", "A1", "a" * 63])
def test_escape_identifier_accepts_plain_identifiers(name):
    assert escape_identifier(name) == f"`{name}`"


@pytest.mark.parametrize(
    "name",
    ["Person; DROP", "has space", "`backtick`", "", "1Leading", "a" * 64, "naïve", None],
)
def test_escape_identifier_rejects_everything_else(name):
    """A generated ontology proposing this is a defect to surface, not escape."""
    with pytest.raises(IdentifierError):
        escape_identifier(name)


def test_no_interpolated_cypher_in_the_source_tree():
    findings = audit_cypher_sources([Path(__file__).resolve().parents[1] / "app"])
    assert findings == [], "Cypher built by interpolation:\n" + "\n".join(findings)


def test_audit_detects_a_violation():
    with tempfile.TemporaryDirectory() as tmp:
        planted = Path(tmp) / "bad.py"
        planted.write_text('q = f"MATCH (e:Entity {{uuid: {user_id}}}) RETURN e"\n')
        assert len(audit_cypher_sources([planted])) == 1


def test_audit_ignores_parameterised_sql():
    """WHERE and CREATE are shared with SQL; matching them flags the cache."""
    with tempfile.TemporaryDirectory() as tmp:
        planted = Path(tmp) / "sql.py"
        planted.write_text(
            'q = f"SELECT key FROM embeddings WHERE key IN ({placeholders})"\n'
        )
        assert audit_cypher_sources([planted]) == []


def test_audit_honours_a_trailing_exemption_marker():
    """Implicit concatenation ends before the closing paren; the marker follows it."""
    with tempfile.TemporaryDirectory() as tmp:
        planted = Path(tmp) / "exempt.py"
        planted.write_text(
            "q = (\n"
            '    f"MATCH (e:{label}) "\n'
            '    f"RETURN e"\n'
            ")  # cypher-audit: ok\n"
        )
        assert audit_cypher_sources([planted]) == []


def test_exemption_does_not_cover_a_whole_function():
    """A marker on one statement must not silence its neighbours."""
    with tempfile.TemporaryDirectory() as tmp:
        planted = Path(tmp) / "scoped.py"
        planted.write_text(
            "def build(label, user_id):\n"
            '    a = f"MATCH (e:{label}) RETURN e"  # cypher-audit: ok\n'
            '    b = f"MERGE (e:Entity {{uuid: {user_id}}}) RETURN e"\n'
            "    return a, b\n"
        )
        findings = audit_cypher_sources([planted])
        assert len(findings) == 1
        assert "MERGE" in findings[0]


# --------------------------------------------------------------------------
# Cosine fallback
# --------------------------------------------------------------------------


def test_cosine_similarity_bounds():
    assert schema.cosine_similarity([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert schema.cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert schema.cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)


def test_cosine_similarity_of_zero_vector_is_zero_not_nan():
    """An all-zero embedding means the model failed; a NaN hides that."""
    assert schema.cosine_similarity([0, 0], [1, 1]) == 0.0


def test_cosine_similarity_rejects_mismatched_dimensions():
    with pytest.raises(ValueError):
        schema.cosine_similarity([1, 2], [1, 2, 3])
