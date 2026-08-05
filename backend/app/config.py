"""Single source of truth for CrowdSight configuration.

Every setting is read from the environment, coerced to its declared type, and
given a local-only default. Nothing else in the codebase should call
``os.environ`` for these values.

The load is deliberately non-fatal: a malformed or missing value is recorded
rather than raised at import time, so that ``python -m app.config`` can still
report *every* problem at once instead of dying on the first one.
:meth:`Config.validate` is what refuses startup.

The egress guard lives here. ``validate()`` parses ``LLM_BASE_URL``,
``EMBEDDING_BASE_URL`` and ``NEO4J_URI`` and rejects any host outside the
allowlist. This is a deliberate inversion of the upstream project, which
rejected *self-hosted* memory URLs; here we reject *non-local* ones. It is the
first of three layers enforcing the sealed-network property — the other two
being the ``internal: true`` container network (Step 3) and the egress
verification suite (Phase 10). A config file cannot by itself guarantee
anything; it can only refuse to be the thing that breaks the seal.
"""

from __future__ import annotations

import os
from ipaddress import ip_address
from typing import Iterable
from urllib.parse import urlparse

try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv
except ImportError:  # python-dotenv absent (e.g. a bare scaffold checkout)
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False

load_dotenv()


class ConfigError(RuntimeError):
    """Raised by :meth:`Config.validate` when the configuration is unusable.

    Carries every problem found, not just the first, so an operator fixes one
    round of errors rather than playing whack-a-mole through restarts.
    """

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = list(errors)
        joined = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(
            f"Invalid CrowdSight configuration ({len(self.errors)} problem(s)):\n{joined}"
        )


# Hosts that are always reachable: loopback plus the two Compose service names.
# Anything else must be opted into explicitly via ALLOWED_HOSTS.
ALWAYS_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "ollama", "neo4j"})

_HTTP_SCHEMES = frozenset({"http", "https"})
_BOLT_SCHEMES = frozenset(
    {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}
)

_MISSING = object()

# Populated during _load(); surfaced by validate(). Coercion failures land here
# so that import never explodes and every problem is reported together.
_LOAD_ERRORS: list[str] = []


def _raw(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _str(name: str, default: str | object = _MISSING) -> str | None:
    value = _raw(name)
    if value is not None:
        return value
    if default is _MISSING:
        return None  # required-but-absent; validate() reports it
    return default  # type: ignore[return-value]


def _int(name: str, default: int) -> int:
    value = _raw(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        _LOAD_ERRORS.append(
            f"{name} must be an integer, got {value!r} (falling back to {default})"
        )
        return default


def _float(name: str, default: float) -> float:
    value = _raw(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        _LOAD_ERRORS.append(
            f"{name} must be a number, got {value!r} (falling back to {default})"
        )
        return default


def _csv(name: str, default: frozenset[str] = frozenset()) -> frozenset[str]:
    value = _raw(name)
    if value is None:
        return default
    items = (item.strip().lstrip(".").lower() for item in value.split(","))
    return frozenset(item for item in items if item)


class Config:
    """Resolved configuration. Read the class attributes; never ``os.environ``."""

    # --- Inference (local Ollama) -------------------------------------------
    LLM_BASE_URL: str
    LLM_MODEL_NAME: str
    LLM_API_KEY: str
    LLM_CONCURRENCY: int

    # --- Embeddings (local Ollama) ------------------------------------------
    EMBEDDING_BASE_URL: str
    EMBEDDING_MODEL: str

    # --- Knowledge graph (local Neo4j) --------------------------------------
    NEO4J_URI: str
    NEO4J_USER: str
    NEO4J_PASSWORD: str | None

    # --- Simulation limits ---------------------------------------------------
    MAX_ROUNDS: int
    MAX_AGENTS: int

    # --- Report generation ---------------------------------------------------
    REPORT_TEMPERATURE: float

    # --- Document ingestion --------------------------------------------------
    CHUNK_SIZE: int
    CHUNK_OVERLAP: int
    MAX_CONTENT_LENGTH: int
    ALLOWED_EXTENSIONS: frozenset[str]

    # --- Egress allowlist ----------------------------------------------------
    ALLOWED_HOSTS: frozenset[str]

    @classmethod
    def reload(cls) -> None:
        """Re-read every setting from the current environment.

        Exists so tests can manipulate ``os.environ`` and re-resolve without
        reimporting the module.
        """
        _LOAD_ERRORS.clear()

        cls.LLM_BASE_URL = _str("LLM_BASE_URL", "http://ollama:11434/v1")
        cls.LLM_MODEL_NAME = _str("LLM_MODEL_NAME", "qwen2.5:14b")
        # Not a credential. The OpenAI SDK rejects an empty string, and Ollama
        # ignores the value entirely; there is nothing to authenticate against.
        cls.LLM_API_KEY = _str("LLM_API_KEY", "ollama")
        cls.LLM_CONCURRENCY = _int("LLM_CONCURRENCY", 4)

        cls.EMBEDDING_BASE_URL = _str("EMBEDDING_BASE_URL", "http://ollama:11434")
        cls.EMBEDDING_MODEL = _str("EMBEDDING_MODEL", "nomic-embed-text")

        cls.NEO4J_URI = _str("NEO4J_URI", "bolt://neo4j:7687")
        cls.NEO4J_USER = _str("NEO4J_USER", "neo4j")
        # No default: an operator must choose one. See validate().
        cls.NEO4J_PASSWORD = _str("NEO4J_PASSWORD")

        cls.MAX_ROUNDS = _int("MAX_ROUNDS", 10)
        cls.MAX_AGENTS = _int("MAX_AGENTS", 100)

        cls.REPORT_TEMPERATURE = _float("REPORT_TEMPERATURE", 0.5)

        cls.CHUNK_SIZE = _int("CHUNK_SIZE", 500)
        cls.CHUNK_OVERLAP = _int("CHUNK_OVERLAP", 50)
        cls.MAX_CONTENT_LENGTH = _int("MAX_CONTENT_LENGTH", 50 * 1024 * 1024)
        cls.ALLOWED_EXTENSIONS = _csv(
            "ALLOWED_EXTENSIONS", frozenset({"pdf", "md", "txt", "markdown"})
        )

        cls.ALLOWED_HOSTS = _csv("ALLOWED_HOSTS")

    @classmethod
    def allowed_hosts(cls) -> frozenset[str]:
        """Every hostname this deployment may talk to."""
        return ALWAYS_ALLOWED_HOSTS | cls.ALLOWED_HOSTS

    @classmethod
    def validate(cls) -> None:
        """Raise :class:`ConfigError` unless the configuration is safe to run.

        Checks, in order: coercion failures recorded during load, required
        settings, numeric ranges, and — the load-bearing one — that no endpoint
        points off-host.
        """
        errors: list[str] = list(_LOAD_ERRORS)

        if not cls.NEO4J_PASSWORD:
            errors.append(
                "NEO4J_PASSWORD is not set. There is no default; choose one and "
                "set it in .env (it must match NEO4J_AUTH in docker-compose.yml)."
            )

        if cls.LLM_CONCURRENCY < 1:
            errors.append("LLM_CONCURRENCY must be at least 1")
        if cls.MAX_ROUNDS < 1:
            errors.append("MAX_ROUNDS must be at least 1")
        if cls.MAX_AGENTS < 1:
            errors.append("MAX_AGENTS must be at least 1")
        if not 0.0 <= cls.REPORT_TEMPERATURE <= 2.0:
            errors.append("REPORT_TEMPERATURE must be between 0.0 and 2.0")
        if cls.CHUNK_SIZE < 1:
            errors.append("CHUNK_SIZE must be at least 1")
        if cls.CHUNK_OVERLAP < 0:
            errors.append("CHUNK_OVERLAP must not be negative")
        if cls.CHUNK_OVERLAP >= cls.CHUNK_SIZE:
            # Otherwise the chunker makes no forward progress and loops forever.
            errors.append(
                f"CHUNK_OVERLAP ({cls.CHUNK_OVERLAP}) must be smaller than "
                f"CHUNK_SIZE ({cls.CHUNK_SIZE})"
            )
        if cls.MAX_CONTENT_LENGTH < 1:
            errors.append("MAX_CONTENT_LENGTH must be at least 1")
        if not cls.ALLOWED_EXTENSIONS:
            errors.append("ALLOWED_EXTENSIONS must list at least one extension")

        allowed = cls.allowed_hosts()
        cls._check_endpoint(errors, "LLM_BASE_URL", cls.LLM_BASE_URL, _HTTP_SCHEMES, allowed)
        cls._check_endpoint(
            errors, "EMBEDDING_BASE_URL", cls.EMBEDDING_BASE_URL, _HTTP_SCHEMES, allowed
        )
        cls._check_endpoint(errors, "NEO4J_URI", cls.NEO4J_URI, _BOLT_SCHEMES, allowed)

        if errors:
            raise ConfigError(errors)

    @staticmethod
    def _check_endpoint(
        errors: list[str],
        name: str,
        raw: str | None,
        allowed_schemes: frozenset[str],
        allowed_hosts: frozenset[str],
    ) -> None:
        """Reject an endpoint whose scheme is wrong or whose host is off-box."""
        if not raw:
            errors.append(f"{name} is not set")
            return

        parsed = urlparse(raw)
        if parsed.scheme not in allowed_schemes:
            errors.append(
                f"{name} has scheme {parsed.scheme or '(none)'!r}; expected one of "
                f"{sorted(allowed_schemes)}"
            )
            return

        try:
            host = parsed.hostname
        except ValueError:  # malformed IPv6 literal, bad port, etc.
            host = None
        if not host:
            errors.append(f"{name} ({raw!r}) has no hostname")
            return

        if host in allowed_hosts:
            return

        # A raw IP is allowed only if it is loopback. Everything else — public
        # addresses and other hosts on the LAN alike — is off-box.
        try:
            if ip_address(host).is_loopback:
                return
        except ValueError:
            pass

        errors.append(
            f"{name} points at {host!r}, which is outside the sealed perimeter. "
            f"Allowed: {sorted(allowed_hosts)}. CrowdSight runs entirely on local "
            f"infrastructure; if this host really is on your LAN and you accept "
            f"widening the perimeter, add it to ALLOWED_HOSTS."
        )

    @classmethod
    def as_dict(cls, redact: bool = True) -> dict[str, object]:
        """Resolved settings, for the health endpoint and startup logging."""
        return {
            "LLM_BASE_URL": cls.LLM_BASE_URL,
            "LLM_MODEL_NAME": cls.LLM_MODEL_NAME,
            "LLM_API_KEY": cls.LLM_API_KEY,  # the literal "ollama"; not secret
            "LLM_CONCURRENCY": cls.LLM_CONCURRENCY,
            "EMBEDDING_BASE_URL": cls.EMBEDDING_BASE_URL,
            "EMBEDDING_MODEL": cls.EMBEDDING_MODEL,
            "NEO4J_URI": cls.NEO4J_URI,
            "NEO4J_USER": cls.NEO4J_USER,
            "NEO4J_PASSWORD": (
                ("***" if cls.NEO4J_PASSWORD else None) if redact else cls.NEO4J_PASSWORD
            ),
            "MAX_ROUNDS": cls.MAX_ROUNDS,
            "MAX_AGENTS": cls.MAX_AGENTS,
            "REPORT_TEMPERATURE": cls.REPORT_TEMPERATURE,
            "CHUNK_SIZE": cls.CHUNK_SIZE,
            "CHUNK_OVERLAP": cls.CHUNK_OVERLAP,
            "MAX_CONTENT_LENGTH": cls.MAX_CONTENT_LENGTH,
            "ALLOWED_EXTENSIONS": sorted(cls.ALLOWED_EXTENSIONS),
            "ALLOWED_HOSTS": sorted(cls.ALLOWED_HOSTS),
        }


Config.reload()


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    print(json.dumps(Config.as_dict(), indent=2, sort_keys=True))
    try:
        Config.validate()
    except ConfigError as exc:
        print(f"\n{exc}", file=sys.stderr)
        raise SystemExit(1)
    print("\nConfiguration valid; all endpoints are inside the sealed perimeter.")
