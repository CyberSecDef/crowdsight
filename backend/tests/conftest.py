"""Shared fixtures.

The central concern is environment isolation. ``Config`` reads both the process
environment and a ``.env`` file, and under Compose the backend container has
every CrowdSight variable injected via ``env_file``. A test that forgot to
clear those would silently assert against the operator's real configuration —
passing or failing for reasons having nothing to do with the code.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402

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


@pytest.fixture(autouse=True)
def _reset_config_singleton() -> Any:
    """Drop the cached process-wide config around every test."""
    import app.config as config_module

    config_module._config = None
    yield
    config_module._config = None


def in_container() -> bool:
    """True when running inside a container, by the marker Docker leaves."""
    return Path("/.dockerenv").exists() or os.environ.get("CROWDSIGHT_IN_CONTAINER") == "1"
