"""Phase 10 Step 4 — the health endpoint and structured logging.

The two health checks added here answer questions reachability cannot. A stack
whose model was never pulled looks perfectly healthy until the first inference
call fails — and it cannot fix itself, because pulling needs the internet the
seal removes. A disk with no room left loses the round it was writing, and
SQLite's error for that is not obviously about space.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.logging_setup import JsonFormatter, configure, json_logging_requested
from app.main import _disk, _models_present, create_app


@pytest.fixture
def client():
    return create_app().test_client()


# --------------------------------------------------------------------------
# Disk headroom
# --------------------------------------------------------------------------


def test_disk_reports_real_numbers(tmp_path):
    disk = _disk(tmp_path)

    assert disk["checked"] is True
    assert disk["free_gb"] > 0
    assert 0 <= disk["percent_used"] <= 100


def test_a_missing_path_is_reported_not_raised():
    """Health must answer even when something it looks at is wrong."""
    disk = _disk("/no/such/place")

    assert disk["checked"] is False
    assert disk["error"]


def test_ROOM_IS_ONLY_FLAGGED_WHEN_IT_IS_ACTUALLY_LOW(tmp_path):
    disk = _disk(tmp_path)
    # This machine has room; the flag must not be permanently on, or it stops
    # meaning anything.
    assert disk["low"] is (disk["free_gb"] < 5.0)


# --------------------------------------------------------------------------
# Model availability — reachable is not the same as usable
# --------------------------------------------------------------------------


class _Config:
    LLM_BASE_URL = "http://ollama:11434/v1"
    LLM_MODEL_NAME = "qwen2.5:14b"
    EMBEDDING_MODEL = "nomic-embed-text"


def _tags(monkeypatch, models):
    import urllib.request
    from contextlib import contextmanager
    from io import BytesIO

    @contextmanager
    def fake(url, timeout=None):
        assert url.endswith("/api/tags"), url
        yield BytesIO(json.dumps({"models": models}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)


def test_a_present_model_is_not_reported_missing(monkeypatch):
    _tags(monkeypatch, [{"name": "qwen2.5:14b"}, {"name": "nomic-embed-text:latest"}])
    assert _models_present(_Config())["missing"] == []


def test_A_MODEL_THAT_WAS_NEVER_PULLED_IS_NAMED(monkeypatch):
    """The failure this exists for: healthy-looking, and nothing will run."""
    _tags(monkeypatch, [{"name": "nomic-embed-text:latest"}])
    assert _models_present(_Config())["missing"] == ["qwen2.5:14b"]


def test_a_tagged_model_matches_an_untagged_config(monkeypatch):
    """Ollama reports 'nomic-embed-text:latest' for a config saying
    'nomic-embed-text'; that is the same model, not a missing one."""
    _tags(monkeypatch, [{"name": "qwen2.5:14b"}, {"name": "nomic-embed-text:latest"}])
    assert _models_present(_Config())["missing"] == []


def test_an_unreachable_ollama_is_reported_not_raised(monkeypatch):
    import urllib.request

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    result = _models_present(_Config())

    assert result["checked"] is False
    assert result["error"]


# --------------------------------------------------------------------------
# The endpoint itself
# --------------------------------------------------------------------------


def test_health_reports_all_four_things(client):
    body = client.get("/api/health").get_json()

    for key in ("checks", "models", "disk", "config"):
        assert key in body, f"health does not report {key}"
    assert {"ollama", "neo4j"} <= set(body["checks"])


def test_liveness_stays_cheap_and_touches_nothing(client):
    """Docker restarts the container on this; it must not depend on Ollama."""
    response = client.get("/api/health/live")

    assert response.status_code == 200
    assert set(response.get_json()) == {"status"}


# --------------------------------------------------------------------------
# Structured logging
# --------------------------------------------------------------------------


def test_text_is_the_default(monkeypatch):
    monkeypatch.delenv("CROWDSIGHT_LOG_FORMAT", raising=False)
    assert json_logging_requested() is False


def test_json_is_opt_in(monkeypatch):
    monkeypatch.setenv("CROWDSIGHT_LOG_FORMAT", "json")
    assert json_logging_requested() is True


def _formatted(record_kwargs=None, context=None):
    formatter = JsonFormatter(context=context or {})
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="round complete", args=(), exc_info=None)
    for key, value in (record_kwargs or {}).items():
        setattr(record, key, value)
    return json.loads(formatter.format(record))


def test_json_carries_the_standard_fields():
    payload = _formatted()

    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "round complete"
    assert payload["time"]


def test_CONTEXT_BECOMES_A_QUERYABLE_FIELD():
    """A sim_id in a text prefix has to be parsed back out; here it is a field."""
    payload = _formatted(context={"sim_id": "sim-1"})
    assert payload["sim_id"] == "sim-1"


def test_extra_fields_survive():
    payload = _formatted({"round": 3, "acted": 27})

    assert payload["round"] == 3
    assert payload["acted"] == 27


def test_A_LOG_LINE_CANNOT_KILL_A_RUN():
    """An unserialisable value in `extra` must not raise inside logging."""
    payload = _formatted({"thing": object()})
    assert isinstance(payload["thing"], str)


def test_an_exception_is_included():
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="app.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info())
    payload = json.loads(formatter.format(record))

    assert "ValueError: boom" in payload["exception"]


def test_configure_does_not_double_up(monkeypatch):
    """Called twice — as the worker and the app both do — one line stays one."""
    monkeypatch.delenv("CROWDSIGHT_LOG_FORMAT", raising=False)
    configure()
    configure()

    assert len(logging.getLogger().handlers) == 1
