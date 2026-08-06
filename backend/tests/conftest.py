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


# --------------------------------------------------------------------------
# Ingestion fixtures
# --------------------------------------------------------------------------


LEFT_COLUMN = (
    "The council approved the housing policy on Tuesday. "
    "Councillor Jane Doe spoke in favour of the measure. "
    "Opposition members requested a longer consultation period."
)
RIGHT_COLUMN = (
    "Meanwhile in Ward Four residents organised a petition. "
    "Local businesses expressed concern about parking provision. "
    "The chamber of commerce will publish its response next month."
)

COUNCIL_TEXT = (
    "Riverbend City Council draft housing policy\n\n"
    + LEFT_COLUMN
    + "\n\n"
    + RIGHT_COLUMN
    + "\n\nMayor Alan Reyes defended the timetable. "
    "Planning officer Sarah Kim confirmed the corridor boundaries."
)


@pytest.fixture
def council_text() -> str:
    return COUNCIL_TEXT


@pytest.fixture
def make_pdf():
    """Build a PDF in memory, optionally two-column or image-only."""
    import pymupdf

    def _make(pages: int = 1, *, columns: int = 1, scanned: bool = False,
              title: str | None = None, encrypt: bool = False) -> bytes:
        document = pymupdf.open()
        for index in range(pages):
            page = document.new_page(width=595, height=842)
            if scanned:
                pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200))
                pixmap.clear_with(200)
                page.insert_image(pymupdf.Rect(50, 50, 250, 250), pixmap=pixmap)
                continue
            if title:
                page.insert_textbox(pymupdf.Rect(50, 50, 545, 90), title, fontsize=18)
            if columns == 2:
                page.insert_textbox(pymupdf.Rect(50, 100, 285, 700), LEFT_COLUMN, fontsize=11)
                page.insert_textbox(pymupdf.Rect(310, 100, 545, 700), RIGHT_COLUMN, fontsize=11)
            else:
                page.insert_textbox(
                    pymupdf.Rect(50, 100, 545, 750),
                    f"Page {index + 1}. " + LEFT_COLUMN, fontsize=11,
                )
        if encrypt:
            data = document.tobytes(
                encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user"
            )
        else:
            data = document.tobytes()
        document.close()
        return data

    return _make


@pytest.fixture
def sample_ontology():
    """A small, fixed ontology so extraction tests do not depend on a model."""
    from app.services.ontology_generator import Ontology

    return Ontology.model_validate({
        "domain": "Municipal housing policy",
        "entity_types": [
            {"name": "Person", "description": "An individual"},
            {"name": "Organisation", "description": "A body"},
            {"name": "Location", "description": "A place"},
        ],
        "relationship_types": [
            {"name": "WORKS_FOR", "description": "Employment",
             "source_types": ["Person"], "target_types": ["Organisation"]},
            {"name": "OPPOSES", "description": "Opposition",
             "source_types": ["Organisation"], "target_types": ["Organisation"]},
        ],
    })


def chat_completion(content: str):
    """An OpenAI-shaped chat completion carrying ``content``."""
    import httpx

    return httpx.Response(200, json={
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
        "model": "qwen2.5:14b",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
    })


def route_by_chunk(per_chunk: dict, default=None):
    """Reply based on which chunk's text a request carries.

    Extraction runs chunks concurrently, so a ``side_effect`` list would be
    consumed in completion order rather than chunk order — the test would then
    assert against whichever request happened to finish first.
    """
    def handler(request):
        blob = request.content.decode()
        for marker, response in per_chunk.items():
            if marker in blob:
                return response() if callable(response) else response
        if default is not None:
            return default() if callable(default) else default
        return chat_completion('{"entities": [], "relationships": []}')

    return handler


# --------------------------------------------------------------------------
# Population fixtures
# --------------------------------------------------------------------------


VALID_PERSONA = {
    "name": "Dawn Mercer",
    "age": 41,
    "occupation": "carpenter",
    "background": "Grew up in the ward and runs a two-person joinery firm.",
    "personality": {"openness": 0.7, "conscientiousness": 0.8, "extraversion": 0.3,
                    "agreeableness": 0.4, "neuroticism": 0.5},
    "traits": ["blunt", "practical"],
    "interests": ["five-a-side", "local history"],
    "leanings": "sceptical of developers",
    "activity_level": "moderate",
    "writing_style": "short sentences, no punctuation fuss",
}


@pytest.fixture
def persona_data() -> dict[str, Any]:
    """A model response that should validate cleanly."""
    return dict(VALID_PERSONA)


@pytest.fixture
def make_persona():
    """Build a PersonaProfile directly, bypassing the model."""
    from app.services.profile_generator import PersonaProfile

    def _make(**overrides: Any):
        provenance = overrides.pop("provenance", "synthetic")
        data = {**VALID_PERSONA, **overrides}
        profile = PersonaProfile.model_validate(data)
        profile.provenance = provenance
        return profile

    return _make


@pytest.fixture
def sample_sketch():
    from app.services.population import PopulationSketch, Stance

    return PopulationSketch(
        setting="a UK city ward facing four-storey development",
        affected_groups=["renters", "homeowners", "commuters", "small traders"],
        stances=[
            Stance(label="opposed", description="Against the density increase", weight=0.4),
            Stance(label="supportive", description="In favour of more homes", weight=0.3),
            Stance(label="conditional", description="Supportive with conditions", weight=0.2),
            Stance(label="indifferent", description="Not engaged", weight=0.1),
        ],
        min_age=20, max_age=75,
    )


@pytest.fixture
def named_contexts():
    """Entities the document names, as the graph would supply them."""
    from app.services.profile_generator import EntityContext

    return [
        EntityContext(uuid=f"u{index}", name=name, type="Councillor",
                      passages=["The planning committee met on Tuesday."])
        for index, name in enumerate(
            ["Councillor Jane Doe", "Mayor Alan Reyes", "Sarah Kim", "Tom Whitfield"]
        )
    ]


@pytest.fixture
def profile_generator(config):
    """A generator whose LLM is mocked at the HTTP layer, with a fixed seed."""
    import random

    from app.services.profile_generator import ProfileGenerator
    from app.utils.llm_client import LLMClient
    from app.utils.retry import RetryPolicy

    def _make(seed: int = 7, **kwargs: Any):
        return ProfileGenerator(
            config,
            llm=LLMClient(config, retry_policy=RetryPolicy(max_attempts=1), **kwargs),
            rng=random.Random(seed),
        )

    return _make


def in_container() -> bool:
    """True when running inside a container, by the marker Docker leaves."""
    return Path("/.dockerenv").exists() or os.environ.get("CROWDSIGHT_IN_CONTAINER") == "1"
