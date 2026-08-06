"""Single source of truth for CrowdSight configuration.

Built on ``pydantic-settings``: every setting is a typed field with a
local-only default, environment variables are bound by field name, and
constructing :class:`Config` *is* validating it. Nothing else in the codebase
should call ``os.environ`` for these values.

The egress guard lives here. A model validator parses ``LLM_BASE_URL``,
``EMBEDDING_BASE_URL`` and ``NEO4J_URI`` and refuses any host outside the
sealed perimeter. The perimeter is, in order of preference:

1. **Loopback** — ``localhost``, ``127.0.0.0/8``, ``::1``. Preferred.
2. **Compose service names** — ``ollama``, ``neo4j``. The default deployment.
3. **Private addresses** — RFC 1918 (``10/8``, ``172.16/12``, ``192.168/16``),
   link-local (``169.254/16``, ``fe80::/10``), and unique-local (``fc00::/7``).
   Permitted, but each one emits a warning: another box on the LAN is another
   box that can be compromised, and traffic to it leaves this host.
4. **Operator opt-in** — any hostname listed in ``ALLOWED_HOSTS``.

Anything else — a public IP, a public hostname — is refused. This is a
deliberate inversion of the upstream project, which rejected *self-hosted*
memory URLs; here we reject *non-local* ones. It is the first of three layers
enforcing the sealed-network property, the others being the ``internal: true``
container network (Phase 1 Step 3) and the egress verification suite (Phase
10). A config module cannot by itself guarantee anything; it can only refuse to
be the thing that breaks the seal.
"""

from __future__ import annotations

import warnings
from ipaddress import ip_address
from typing import Annotated, Any, Iterable, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = [
    "Config",
    "ConfigError",
    "PerimeterWarning",
    "classify_host",
    "get_config",
    "reload_config",
]


class ConfigError(RuntimeError):
    """Raised when the configuration is unusable.

    Wraps pydantic's :class:`~pydantic.ValidationError` in something an
    operator can act on, and carries every problem found rather than only the
    first — so one round of fixes is enough instead of whack-a-mole through
    restarts.
    """

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = list(errors)
        joined = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(
            f"Invalid CrowdSight configuration ({len(self.errors)} problem(s)):\n{joined}"
        )

    @classmethod
    def from_validation_error(cls, exc: ValidationError) -> "ConfigError":
        messages = []
        for err in exc.errors():
            location = ".".join(str(part) for part in err["loc"]) or "(config)"
            messages.append(f"{location}: {err['msg']}")
        return cls(messages)


class PerimeterWarning(UserWarning):
    """Emitted when an endpoint is reachable but weaker than loopback."""


# Compose service names, resolvable only on the internal network.
SERVICE_HOSTS = frozenset({"ollama", "neo4j"})
LOOPBACK_HOSTS = frozenset({"localhost"})

HostKind = Literal["loopback", "service", "private", "allowlisted", "public"]

_HTTP_SCHEMES = frozenset({"http", "https"})
_BOLT_SCHEMES = frozenset(
    {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}
)

_API_KEY_SENTINEL = "ollama"


def classify_host(host: str, allowed_hosts: Iterable[str] = ()) -> HostKind:
    """Classify a hostname or IP literal against the sealed perimeter.

    Hostnames are never resolved. DNS can point anywhere, and a check that
    depends on what a resolver says today is not a guarantee — so a name is
    trusted only if it is a known service name or an explicit operator opt-in.
    IP literals are classified by the address itself.
    """
    host = host.strip().lower().strip("[]")
    if not host:
        return "public"

    if host in LOOPBACK_HOSTS:
        return "loopback"

    try:
        ip = ip_address(host)
    except ValueError:
        if host in SERVICE_HOSTS:
            return "service"
        if host in {h.strip().lower() for h in allowed_hosts}:
            return "allowlisted"
        return "public"

    if ip.is_loopback:
        return "loopback"
    # is_private covers RFC 1918, link-local, unique-local and friends.
    if ip.is_private or ip.is_link_local:
        return "private"
    if host in {h.strip().lower() for h in allowed_hosts}:
        return "allowlisted"
    return "public"


def _split_csv(value: Any) -> Any:
    """Accept ``a,b,c`` from the environment for set-typed fields."""
    if isinstance(value, str):
        items = (item.strip().lstrip(".").lower() for item in value.split(","))
        return {item for item in items if item}
    return value


class Config(BaseSettings):
    """Resolved configuration. Construct via :func:`get_config`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    # --- Inference (local Ollama, or any OpenAI-compatible local gateway) ----
    LLM_BASE_URL: str = "http://ollama:11434/v1"
    LLM_MODEL_NAME: str = "qwen2.5:14b"
    # Usually not a credential: the OpenAI SDK rejects an empty string and
    # Ollama ignores the value. Settable because a local gateway in front of
    # Ollama (LiteLLM, vLLM) may want a real token. It never leaves the
    # perimeter either way, since the perimeter is enforced above.
    LLM_API_KEY: SecretStr = SecretStr(_API_KEY_SENTINEL)
    # Per process. Phase 6 spawns a process per simulation and must divide
    # this across its workers; the bound exists to stop a GPU OOM, and three
    # concurrent runs at 4 each would put 12 requests in flight.
    LLM_CONCURRENCY: int = Field(default=4, ge=1)
    # Generous by necessity: a 14b model producing a long completion routinely
    # takes 30-90s, and a report agent longer. Connecting, by contrast, either
    # works immediately or is not going to.
    LLM_TIMEOUT: float = Field(default=300.0, gt=0)
    LLM_CONNECT_TIMEOUT: float = Field(default=10.0, gt=0)
    LLM_MAX_ATTEMPTS: int = Field(default=3, ge=1)
    LLM_RETRY_BASE_DELAY: float = Field(default=1.0, ge=0)
    LLM_RETRY_MAX_DELAY: float = Field(default=30.0, ge=0)

    # --- Embeddings (local Ollama) ------------------------------------------
    EMBEDDING_BASE_URL: str = "http://ollama:11434"
    EMBEDDING_MODEL: str = "nomic-embed-text"
    # nomic-embed-text is 768-dimensional. Declared rather than inferred so a
    # model swap that silently changes dimensionality fails loudly instead of
    # poisoning the graph with vectors that cannot be compared to the old ones.
    EMBEDDING_DIM: int = Field(default=768, ge=1)
    EMBEDDING_BATCH_SIZE: int = Field(default=32, ge=1)
    EMBEDDING_CACHE_PATH: str = "data/cache/embeddings.db"
    EMBEDDING_CACHE_ENABLED: bool = True
    # Cosine threshold for folding near-duplicate entities together. High on
    # purpose: measured against nomic-embed-text, "Mayor Alan Reyes"/"Alan
    # Reyes" scores 0.839 while "Jane Doe"/"John Doe" scores 0.813, so a
    # permissive threshold fuses distinct people. Name normalisation does the
    # real work; this is a guarded safety net.
    ENTITY_SIMILARITY_THRESHOLD: float = Field(default=0.90, ge=0.0, le=1.0)

    # --- Knowledge graph (local Neo4j) --------------------------------------
    NEO4J_URI: str = "bolt://neo4j:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: SecretStr = SecretStr("")
    # Community Edition serves a single database; the name is fixed but kept
    # configurable so an Enterprise deployment can isolate runs per database.
    NEO4J_DATABASE: str = "neo4j"
    NEO4J_MAX_POOL_SIZE: int = Field(default=50, ge=1)
    NEO4J_CONNECTION_TIMEOUT: float = Field(default=30.0, gt=0)

    # --- Simulation limits ---------------------------------------------------
    MAX_ROUNDS: int = Field(default=10, ge=1)
    #: Sampling temperature for simulation agents. Higher than the extraction
    #: paths on purpose: a population that all reasons identically is not a
    #: population. The spec's figure.
    SIMULATION_TEMPERATURE: float = Field(default=0.7, ge=0.0, le=2.0)
    MAX_AGENTS: int = Field(default=100, ge=1)
    # The share of the population drawn from people the document names. A crowd
    # that is one-third councillors is not the crowd; documents name
    # office-holders, and a simulation of office-holders answers a different
    # question. If the document names fewer people than this allows, all are used.
    POPULATION_NAMED_RATIO: float = Field(default=0.25, ge=0.0, le=1.0)

    # --- Report generation ---------------------------------------------------
    REPORT_TEMPERATURE: float = Field(default=0.5, ge=0.0, le=2.0)

    # --- Document ingestion --------------------------------------------------
    # 1500 characters is roughly a paragraph or two, so a relationship stated
    # across sentences survives inside one chunk. Each chunk is one LLM
    # extraction call in Phase 3 Step 4, so this also sets ingestion cost:
    # at 500 a 40-page report is ~220 calls, at 1500 it is ~73.
    CHUNK_SIZE: int = Field(default=1500, ge=1)
    CHUNK_OVERLAP: int = Field(default=150, ge=0)
    MAX_CONTENT_LENGTH: int = Field(default=50 * 1024 * 1024, ge=1)
    ALLOWED_EXTENSIONS: Annotated[frozenset[str], NoDecode] = frozenset(
        {"pdf", "md", "txt", "markdown"}
    )

    # --- Egress allowlist ----------------------------------------------------
    ALLOWED_HOSTS: Annotated[frozenset[str], NoDecode] = frozenset()

    # Populated by the perimeter validator; surfaced at startup.
    perimeter_notes: tuple[str, ...] = Field(default=(), exclude=True, repr=False)

    _split = field_validator("ALLOWED_EXTENSIONS", "ALLOWED_HOSTS", mode="before")(
        _split_csv
    )

    @field_validator("ALLOWED_EXTENSIONS")
    @classmethod
    def _require_an_extension(cls, value: frozenset[str]) -> frozenset[str]:
        if not value:
            raise ValueError("must list at least one extension")
        return value

    @model_validator(mode="after")
    def _validate_deployment(self) -> "Config":
        """Cross-field checks, accumulated.

        Deliberately a single validator rather than several. Pydantic runs
        ``mode="after"`` validators in sequence and the first to raise aborts
        the rest, so splitting these would report one problem per restart —
        exactly the whack-a-mole this module promises to avoid. Errors are
        gathered here and raised together as a :class:`ConfigError`, which
        pydantic passes through untouched (it only converts ``ValueError`` and
        ``AssertionError``).

        Type and range failures still surface first, as a pydantic
        ``ValidationError``: a field that is not even the right type cannot be
        meaningfully checked for anything else.
        """
        errors: list[str] = []

        if not self.NEO4J_PASSWORD.get_secret_value().strip():
            errors.append(
                "NEO4J_PASSWORD is not set. There is no default; choose one and set "
                "it in .env (it must match NEO4J_AUTH in docker-compose.yml)."
            )

        if self.CHUNK_OVERLAP >= self.CHUNK_SIZE:
            # Otherwise the chunker never advances and loops forever.
            errors.append(
                f"CHUNK_OVERLAP ({self.CHUNK_OVERLAP}) must be smaller than "
                f"CHUNK_SIZE ({self.CHUNK_SIZE})"
            )

        notes: list[str] = []
        for name, schemes in (
            ("LLM_BASE_URL", _HTTP_SCHEMES),
            ("EMBEDDING_BASE_URL", _HTTP_SCHEMES),
            ("NEO4J_URI", _BOLT_SCHEMES),
        ):
            try:
                note = self._check_endpoint(name, getattr(self, name), schemes)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if note:
                notes.append(note)

        if errors:
            raise ConfigError(errors)

        object.__setattr__(self, "perimeter_notes", tuple(notes))
        for note in notes:
            warnings.warn(note, PerimeterWarning, stacklevel=2)
        return self

    def _check_endpoint(self, name: str, raw: str, allowed_schemes: frozenset[str]) -> str | None:
        """Validate one endpoint. Returns a warning note, or raises to reject."""
        from urllib.parse import urlparse

        if not raw or not raw.strip():
            raise ValueError(f"{name} is not set")

        parsed = urlparse(raw.strip())
        if parsed.scheme not in allowed_schemes:
            raise ValueError(
                f"{name} has scheme {parsed.scheme or '(none)'!r}; expected one of "
                f"{sorted(allowed_schemes)}"
            )

        try:
            host = parsed.hostname
        except ValueError:  # malformed IPv6 literal or bad port
            host = None
        if not host:
            raise ValueError(f"{name} ({raw!r}) has no hostname")

        kind = classify_host(host, self.ALLOWED_HOSTS)
        if kind == "public":
            raise ValueError(
                f"{name} points at {host!r}, which is outside the sealed perimeter. "
                f"Permitted: loopback, the service names {sorted(SERVICE_HOSTS)}, "
                f"private/link-local addresses, or a host listed in ALLOWED_HOSTS. "
                f"CrowdSight runs entirely on local infrastructure."
            )
        if kind == "private":
            return (
                f"{name} points at the private address {host!r}. This is permitted, "
                f"but loopback or a Compose service name is preferred — traffic to "
                f"another box on the LAN still leaves this host, and the "
                f"container-level egress seal cannot cover it."
            )
        if kind == "allowlisted":
            return (
                f"{name} points at {host!r}, permitted only because it appears in "
                f"ALLOWED_HOSTS. Names are never resolved, so this trusts your DNS."
            )
        return None

    # --- Convenience ---------------------------------------------------------

    def llm_retry_policy(self) -> Any:
        """Retry policy for calls to Ollama, built from configuration."""
        from app.utils.retry import RetryPolicy

        return RetryPolicy(
            max_attempts=self.LLM_MAX_ATTEMPTS,
            base_delay=self.LLM_RETRY_BASE_DELAY,
            max_delay=self.LLM_RETRY_MAX_DELAY,
        )

    @property
    def neo4j_password(self) -> str:
        return self.NEO4J_PASSWORD.get_secret_value()

    @property
    def llm_api_key(self) -> str:
        return self.LLM_API_KEY.get_secret_value()

    def as_dict(self, redact: bool = True) -> dict[str, Any]:
        """Resolved settings, for the health endpoint and startup logging."""
        data = self.model_dump(mode="json")
        data.pop("perimeter_notes", None)
        data["ALLOWED_EXTENSIONS"] = sorted(self.ALLOWED_EXTENSIONS)
        data["ALLOWED_HOSTS"] = sorted(self.ALLOWED_HOSTS)
        if redact:
            data["NEO4J_PASSWORD"] = "***" if self.neo4j_password else None
            # Revealed only while it is the inert default; a value an operator
            # actually chose is treated as a credential.
            data["LLM_API_KEY"] = (
                _API_KEY_SENTINEL if self.llm_api_key == _API_KEY_SENTINEL else "***"
            )
        else:
            data["NEO4J_PASSWORD"] = self.neo4j_password
            data["LLM_API_KEY"] = self.llm_api_key
        return data


_config: Config | None = None


def get_config(**overrides: Any) -> Config:
    """Return the process-wide config, constructing it on first use.

    Raises :class:`ConfigError` — not pydantic's ``ValidationError`` — so
    callers depend on this module's contract rather than on pydantic's.
    """
    global _config
    if _config is None or overrides:
        try:
            config = Config(**overrides)
        except ValidationError as exc:
            raise ConfigError.from_validation_error(exc) from exc
        if not overrides:
            _config = config
        return config
    return _config


def reload_config(**overrides: Any) -> Config:
    """Discard the cached config and re-read the environment."""
    global _config
    _config = None
    config = get_config(**overrides)
    _config = config
    return config


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", PerimeterWarning)
        try:
            config = get_config()
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            raise SystemExit(1)

    print(json.dumps(config.as_dict(), indent=2, sort_keys=True))
    sys.stdout.flush()

    for note in config.perimeter_notes:
        print(f"\nWARNING: {note}", file=sys.stderr)
    sys.stderr.flush()

    if config.perimeter_notes:
        print(
            f"\nConfiguration valid, with {len(config.perimeter_notes)} endpoint(s) "
            f"reaching beyond loopback. See the warnings above."
        )
    else:
        print("\nConfiguration valid; every endpoint is on loopback or a service name.")
