"""Phase 1 Step 4 — the egress guard at the unit level.

These tests exist because the sealed-network property has three layers, and
this is the only one that can be checked without Docker: configuration that
points off-host must refuse to start the process. The container network
(Step 3) and the egress verification suite (Phase 10) cover the rest.
"""

from __future__ import annotations

import warnings

import pytest

from app.config import (
    Config,
    ConfigError,
    PerimeterWarning,
    classify_host,
    get_config,
    reload_config,
)

# --------------------------------------------------------------------------
# classify_host — the smallest unit the whole guarantee rests on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        # Loopback, in every spelling.
        ("localhost", "loopback"),
        ("LOCALHOST", "loopback"),
        ("127.0.0.1", "loopback"),
        ("127.0.1.5", "loopback"),  # all of 127/8, not just .0.1
        ("::1", "loopback"),
        # Compose service names.
        ("ollama", "service"),
        ("neo4j", "service"),
        # Private, link-local, unique-local.
        ("10.0.0.5", "private"),
        ("172.16.0.1", "private"),
        ("172.31.255.254", "private"),
        ("192.168.1.50", "private"),
        ("169.254.10.1", "private"),
        ("fd00::1", "private"),
        # Public — the ones that must never slip through.
        ("1.1.1.1", "public"),
        ("8.8.8.8", "public"),
        ("93.184.216.34", "public"),
        ("api.openai.com", "public"),
        ("api.anthropic.com", "public"),
        ("abc.databases.neo4j.io", "public"),
        ("2606:4700:4700::1111", "public"),
        ("", "public"),
        # A name that merely *contains* an allowed name is not allowed.
        ("ollama.evil.com", "public"),
        ("notlocalhost", "public"),
    ],
)
def test_classify_host(host: str, expected: str) -> None:
    assert classify_host(host) == expected


def test_classify_host_honours_allowlist() -> None:
    assert classify_host("gpubox") == "public"
    assert classify_host("gpubox", ["gpubox"]) == "allowlisted"
    assert classify_host("GPUBox", ["gpubox"]) == "allowlisted"


def test_classify_host_never_resolves_dns() -> None:
    """A name that certainly resolves must still be judged public.

    If this ever returns anything else, the check has started trusting a
    resolver, and the guarantee is only as good as today's DNS answer.
    """
    assert classify_host("localhost.localdomain") == "public"
    assert classify_host("dns.google") == "public"


# --------------------------------------------------------------------------
# Accepted silently
# --------------------------------------------------------------------------


def test_defaults_are_service_names(make_config) -> None:
    config = make_config()
    assert config.LLM_BASE_URL == "http://ollama:11434/v1"
    assert config.EMBEDDING_BASE_URL == "http://ollama:11434"
    assert config.NEO4J_URI == "bolt://neo4j:7687"
    assert config.perimeter_notes == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"LLM_BASE_URL": "http://localhost:11434/v1"},
        {"EMBEDDING_BASE_URL": "http://127.0.0.1:11434"},
        {"NEO4J_URI": "bolt://localhost:7687"},
        {"NEO4J_URI": "bolt://[::1]:7687"},
        {"NEO4J_URI": "neo4j://neo4j:7687"},
        {
            "LLM_BASE_URL": "http://127.0.0.1:11434/v1",
            "EMBEDDING_BASE_URL": "http://127.0.0.1:11434",
            "NEO4J_URI": "bolt://127.0.0.1:7687",
        },
    ],
)
def test_loopback_and_service_names_accepted_silently(make_config, overrides) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", PerimeterWarning)
        config = make_config(**overrides)
    assert config.perimeter_notes == ()


# --------------------------------------------------------------------------
# Accepted, but noisily
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "host"),
    [
        ({"LLM_BASE_URL": "http://10.0.0.5:11434/v1"}, "10.0.0.5"),
        ({"EMBEDDING_BASE_URL": "http://172.16.4.4:11434"}, "172.16.4.4"),
        ({"NEO4J_URI": "bolt://192.168.1.50:7687"}, "192.168.1.50"),
        ({"LLM_BASE_URL": "http://169.254.10.1:11434/v1"}, "169.254.10.1"),
        ({"NEO4J_URI": "bolt://[fd00::1]:7687"}, "fd00::1"),
    ],
)
def test_private_addresses_accepted_with_warning(make_config, overrides, host) -> None:
    with pytest.warns(PerimeterWarning, match=host.replace("[", "").replace("]", "")):
        config = make_config(**overrides)
    assert len(config.perimeter_notes) == 1
    note = config.perimeter_notes[0]
    assert host in note
    # The warning must say why loopback is better, not merely that it noticed.
    assert "preferred" in note


def test_every_off_loopback_endpoint_is_reported(make_config) -> None:
    with pytest.warns(PerimeterWarning):
        config = make_config(
            LLM_BASE_URL="http://192.168.1.9:11434/v1",
            EMBEDDING_BASE_URL="http://192.168.1.9:11434",
            NEO4J_URI="bolt://192.168.1.9:7687",
        )
    assert len(config.perimeter_notes) == 3


def test_allowlisted_host_warns_about_dns(make_config) -> None:
    with pytest.warns(PerimeterWarning, match="DNS"):
        config = make_config(
            LLM_BASE_URL="http://gpubox:11434/v1", ALLOWED_HOSTS="gpubox"
        )
    assert len(config.perimeter_notes) == 1


# --------------------------------------------------------------------------
# Refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"LLM_BASE_URL": "http://api.openai.com/v1"}, "api.openai.com"),
        ({"LLM_BASE_URL": "https://api.anthropic.com/v1"}, "api.anthropic.com"),
        ({"LLM_BASE_URL": "https://generativelanguage.googleapis.com"}, "googleapis"),
        ({"EMBEDDING_BASE_URL": "http://1.1.1.1:11434"}, "1.1.1.1"),
        ({"NEO4J_URI": "bolt://8.8.8.8:7687"}, "8.8.8.8"),
        ({"NEO4J_URI": "neo4j+s://abc.databases.neo4j.io"}, "databases.neo4j.io"),
        ({"NEO4J_URI": "bolt://ollama.evil.com:7687"}, "ollama.evil.com"),
    ],
)
def test_public_endpoints_refused(make_config, overrides, needle) -> None:
    with pytest.raises(Exception) as excinfo:
        make_config(**overrides)
    assert needle in str(excinfo.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"NEO4J_URI": "http://neo4j:7687"},
        {"NEO4J_URI": "ftp://neo4j:7687"},
        {"LLM_BASE_URL": "bolt://ollama:11434"},
        {"LLM_BASE_URL": "ollama:11434/v1"},  # no scheme at all
        {"EMBEDDING_BASE_URL": "file:///etc/passwd"},
    ],
)
def test_wrong_scheme_refused(make_config, overrides) -> None:
    with pytest.raises(Exception, match="scheme"):
        make_config(**overrides)


@pytest.mark.parametrize("field", ["LLM_BASE_URL", "EMBEDDING_BASE_URL", "NEO4J_URI"])
def test_empty_endpoint_refused(make_config, field) -> None:
    with pytest.raises(Exception, match="not set"):
        make_config(**{field: "   "})


def test_allowlist_does_not_permit_public_hosts(make_config) -> None:
    """ALLOWED_HOSTS is an escape hatch for LAN names, not for the internet.

    Listing a public name is still honoured — an operator can defeat any
    guard — but it must warn rather than pass silently.
    """
    with pytest.warns(PerimeterWarning):
        config = make_config(
            LLM_BASE_URL="http://api.openai.com/v1", ALLOWED_HOSTS="api.openai.com"
        )
    assert config.perimeter_notes


# --------------------------------------------------------------------------
# Required settings and field constraints
# --------------------------------------------------------------------------


def test_missing_password_fails_loudly(make_config) -> None:
    with pytest.raises(Exception, match="NEO4J_PASSWORD"):
        make_config(NEO4J_PASSWORD="")


def test_whitespace_password_is_not_a_password(make_config) -> None:
    with pytest.raises(Exception, match="NEO4J_PASSWORD"):
        make_config(NEO4J_PASSWORD="   ")


def test_missing_password_from_environment_fails(clean_env) -> None:
    """The real entry point, not just the constructor."""
    with pytest.raises(ConfigError, match="NEO4J_PASSWORD"):
        reload_config(_env_file=None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"MAX_ROUNDS": "ten"},
        {"MAX_ROUNDS": 0},
        {"MAX_ROUNDS": -1},
        {"MAX_AGENTS": 0},
        {"LLM_CONCURRENCY": 0},
        {"REPORT_TEMPERATURE": 3.5},
        {"REPORT_TEMPERATURE": -0.1},
        {"CHUNK_SIZE": 0},
        {"CHUNK_OVERLAP": -1},
        {"MAX_CONTENT_LENGTH": 0},
        {"ALLOWED_EXTENSIONS": ""},
    ],
)
def test_out_of_range_values_refused(make_config, overrides) -> None:
    with pytest.raises(Exception):
        make_config(**overrides)


def test_chunk_overlap_must_leave_forward_progress(make_config) -> None:
    """Overlap >= size means the chunker never advances. Catch it at startup."""
    with pytest.raises(Exception, match="CHUNK_OVERLAP"):
        make_config(CHUNK_SIZE=500, CHUNK_OVERLAP=500)
    with pytest.raises(Exception, match="CHUNK_OVERLAP"):
        make_config(CHUNK_SIZE=500, CHUNK_OVERLAP=800)
    assert make_config(CHUNK_SIZE=500, CHUNK_OVERLAP=499).CHUNK_OVERLAP == 499


def test_defaults_are_not_silently_substituted_for_bad_input(make_config) -> None:
    """A typo must fail, not fall back to the default."""
    with pytest.raises(Exception):
        make_config(MAX_ROUNDS="ten")


# --------------------------------------------------------------------------
# Parsing and presentation
# --------------------------------------------------------------------------


def test_csv_fields_are_normalised(make_config) -> None:
    config = make_config(ALLOWED_EXTENSIONS="PDF, .MD ,txt,", ALLOWED_HOSTS="A, b ,")
    assert config.ALLOWED_EXTENSIONS == frozenset({"pdf", "md", "txt"})
    assert config.ALLOWED_HOSTS == frozenset({"a", "b"})


def test_secrets_are_redacted(make_config) -> None:
    config = make_config(NEO4J_PASSWORD="hunter2")
    assert config.as_dict()["NEO4J_PASSWORD"] == "***"
    assert config.as_dict(redact=False)["NEO4J_PASSWORD"] == "hunter2"
    assert "hunter2" not in repr(config)


def test_default_api_key_is_not_treated_as_a_secret(make_config) -> None:
    """Redacting the inert default would imply a secret exists where none does."""
    assert make_config().as_dict()["LLM_API_KEY"] == "ollama"


def test_operator_supplied_api_key_is_redacted(make_config) -> None:
    config = make_config(LLM_API_KEY="sk-local-gateway-token")
    assert config.llm_api_key == "sk-local-gateway-token"
    assert config.as_dict()["LLM_API_KEY"] == "***"


def test_as_dict_omits_internal_fields(make_config) -> None:
    assert "perimeter_notes" not in make_config().as_dict()


# --------------------------------------------------------------------------
# Error reporting
# --------------------------------------------------------------------------


def test_config_error_reports_every_problem_at_once(clean_env, monkeypatch) -> None:
    """One round of fixes, not whack-a-mole through restarts."""
    monkeypatch.setenv("LLM_BASE_URL", "http://api.openai.com/v1")
    monkeypatch.setenv("NEO4J_URI", "bolt://8.8.8.8:7687")
    with pytest.raises(ConfigError) as excinfo:
        reload_config(_env_file=None)
    assert len(excinfo.value.errors) >= 2


def test_entry_points_raise_config_error_not_validation_error(clean_env, monkeypatch) -> None:
    """Callers depend on this module's contract, not on pydantic's."""
    monkeypatch.setenv("LLM_BASE_URL", "http://api.openai.com/v1")
    monkeypatch.setenv("NEO4J_PASSWORD", "x")
    with pytest.raises(ConfigError):
        reload_config(_env_file=None)


def test_get_config_caches(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "x")
    first = reload_config(_env_file=None)
    assert get_config() is first
    monkeypatch.setenv("MAX_ROUNDS", "42")
    assert get_config().MAX_ROUNDS == first.MAX_ROUNDS
    assert reload_config(_env_file=None).MAX_ROUNDS == 42


def test_environment_variables_are_read(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "from-env")
    monkeypatch.setenv("MAX_AGENTS", "250")
    monkeypatch.setenv("ALLOWED_EXTENSIONS", "pdf,txt")
    config = reload_config(_env_file=None)
    assert config.neo4j_password == "from-env"
    assert config.MAX_AGENTS == 250
    assert config.ALLOWED_EXTENSIONS == frozenset({"pdf", "txt"})
