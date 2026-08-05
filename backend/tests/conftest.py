"""Shared fixtures.

The central concern is environment isolation. ``Config`` reads both the process
environment and a ``.env`` file, and under Compose the backend container has
every CrowdSight variable injected via ``env_file``. A test that forgot to
clear those would silently assert against the operator's real configuration —
passing or failing for reasons having nothing to do with the code.

Integration fixtures need the opposite: the *real* credentials, so they can
reach the live Neo4j. Both work from ``REAL_ENV``, a snapshot taken at import
before any test starts monkeypatching.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402

# Snapshot before any test mutates os.environ.
REAL_ENV = dict(os.environ)

# Every variable Config knows about, plus the ones Compose injects.
MANAGED_VARS = tuple(Config.model_fields) + ("CROWDSIGHT_BIND", "BACKEND_TARGET")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all CrowdSight variables from the environment for one test."""
    for name in MANAGED_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


@pytest.fixture
def make_config(clean_env: None) -> Callable[..., Config]:
    """Build a Config from explicit values only.

    ``_env_file=None`` stops pydantic-settings reading a stray ``.env`` from
    the working directory, so a test asserts on what it passed and nothing
    else. NEO4J_PASSWORD is supplied by default because it is required and
    almost never the subject of the test; pass ``NEO4J_PASSWORD=""`` to
    exercise its absence.
    """

    def _make(**overrides: Any) -> Config:
        values: dict[str, Any] = {"NEO4J_PASSWORD": "test-password"}
        values.update(overrides)
        return Config(_env_file=None, **values)

    return _make


@pytest.fixture
def config(make_config: Callable[..., Config]) -> Config:
    """A default, valid configuration for service-level tests."""
    return make_config()


@pytest.fixture(autouse=True)
def _reset_config_singleton() -> Any:
    """Drop the cached process-wide config around every test."""
    import app.config as config_module

    config_module._config = None
    yield
    config_module._config = None


@pytest.fixture(autouse=True)
def _reset_gate() -> Any:
    """Drop the process-wide concurrency gate around every test.

    It is a singleton sized from configuration, so a gate built under one
    test's config would otherwise bound the next test's concurrency.
    """
    from app.utils.retry import reset_llm_gate

    reset_llm_gate()
    yield
    reset_llm_gate()


# --------------------------------------------------------------------------
# Integration: live Neo4j
# --------------------------------------------------------------------------


@pytest.fixture
def integration_config() -> Config:
    """Configuration pointing at the live Neo4j, from the real environment."""
    password = REAL_ENV.get("NEO4J_PASSWORD")
    if not password:
        pytest.fail(
            "NEO4J_PASSWORD is not set in the environment, so the live Neo4j "
            "cannot be reached. Run integration tests inside the backend "
            "container: docker compose exec backend pytest -m integration"
        )
    return Config(
        _env_file=None,
        NEO4J_PASSWORD=password,
        NEO4J_URI=REAL_ENV.get("NEO4J_URI", "bolt://neo4j:7687"),
        NEO4J_USER=REAL_ENV.get("NEO4J_USER", "neo4j"),
    )


@pytest.fixture
def graph_ns() -> str:
    """A unique graph_id namespace, so concurrent runs cannot collide."""
    return "pytest-" + uuid.uuid4().hex[:12]


@pytest.fixture
async def storage(integration_config: Config, graph_ns: str) -> Any:
    """A connected Neo4jStorage, with every node it created removed after.

    Fails rather than skips when Neo4j is unreachable. These tests are already
    opt-in via the `integration` marker; skipping an opt-in test turns an
    explicit request to verify something into a silent no-op.
    """
    from app.storage.neo4j_storage import Neo4jStorage

    store = Neo4jStorage(integration_config)
    try:
        await store.verify_connectivity()
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        await store.aclose()
        pytest.fail(
            f"Cannot reach Neo4j at {integration_config.NEO4J_URI}: {exc}\n"
            f"Start it with: docker compose up -d neo4j"
        )
    try:
        yield store
    finally:
        try:
            await store.write(
                "MATCH (e:Entity {graph_id: $ns}) DETACH DELETE e", ns=graph_ns
            )
        finally:
            await store.aclose()


def in_container() -> bool:
    """True when running inside a container, by the marker Docker leaves."""
    return Path("/.dockerenv").exists() or os.environ.get("CROWDSIGHT_IN_CONTAINER") == "1"
