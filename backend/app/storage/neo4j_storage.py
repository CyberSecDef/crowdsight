"""Async Neo4j access: connection pooling, sessions, parameterised Cypher.

Everything that reaches the graph goes through here. The driver owns the
connection pool, so a single :class:`Neo4jStorage` per process is correct —
constructing one per request would defeat pooling entirely.

**Parameterised Cypher only.** Values are always passed as parameters, never
interpolated into the query text. This is not merely about injection from
untrusted input, though a document that names an entity ``"'); MATCH (n)
DETACH DELETE n //"`` is exactly the kind of thing this system ingests. It is
also about the query cache: Neo4j plans by query text, so interpolating values
produces a distinct plan per value and evicts everything useful.

Cypher cannot parameterise labels or relationship types — that is a genuine
limitation of the language, not an oversight. :func:`escape_identifier` is the
only sanctioned way to build those, and it validates against a strict pattern
rather than escaping, because an identifier that needs escaping is a bug
upstream.

:func:`audit_cypher_sources` scans the tree for interpolation next to Cypher
keywords, so "all queries are parameterised" can be asserted as a repo-wide
invariant instead of hoped for.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import Neo4jError as DriverError

from app.config import Config, get_config
from app.utils.retry import RetryPolicy, retry_async

logger = logging.getLogger(__name__)

__all__ = [
    "IdentifierError",
    "Neo4jStorage",
    "StorageError",
    "audit_cypher_sources",
    "escape_identifier",
]


class StorageError(RuntimeError):
    """A graph operation failed in a way the caller should handle."""


class IdentifierError(ValueError):
    """A label or relationship type was not a safe bare identifier."""


# Deliberately narrow. Labels and relationship types in this system come from
# a generated ontology, and an ontology proposing `My Label; DROP` is a defect
# to surface, not a string to quietly escape.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def escape_identifier(name: str) -> str:
    """Validate a label or relationship type for safe inclusion in Cypher.

    Returns the name backtick-quoted. Raises :class:`IdentifierError` for
    anything that is not a plain identifier — including names containing
    backticks, which is how quoting would otherwise be escaped out of.
    """
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise IdentifierError(
            f"{name!r} is not a valid Cypher identifier. Labels and relationship "
            f"types must match {_IDENTIFIER_RE.pattern}; values belong in "
            f"parameters, not in the query text."
        )
    return f"`{name}`"


# --------------------------------------------------------------------------
# Source audit
# --------------------------------------------------------------------------

# Keywords that are Cypher and not also SQL. `WHERE`, `CREATE` and `SET` are
# shared, and matching on those flags every parameterised SQL string in the
# project — the embedding cache's own queries included.
_CYPHER_ONLY = (
    "MATCH", "MERGE", "UNWIND", "YIELD", "DETACH", "RETURN",
    "CREATE INDEX", "DROP INDEX", "CREATE CONSTRAINT", "DROP CONSTRAINT",
    "CREATE VECTOR INDEX", "SHOW INDEXES", "SHOW CONSTRAINTS",
)
_CYPHER_ONLY_RE = re.compile("|".join(_CYPHER_ONLY))

AUDIT_EXEMPTION = "cypher-audit: ok"


def _static_text(node: ast.AST) -> str:
    """The literal portions of an interpolated string, interpolations dropped."""
    parts: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            parts.append(child.value)
    return " ".join(parts)


def audit_cypher_sources(paths: Iterable[str | Path]) -> list[str]:
    """Return Cypher that appears to be built by string interpolation.

    Works on the AST rather than line by line, for two reasons learned the
    hard way. A statement spanning several lines is one node, so an exemption
    comment anywhere inside it applies to the whole thing — a line scanner
    only sees the line it is on. And the AST distinguishes an f-string from a
    comment or a docstring that merely mentions ``MERGE``.

    Only Cypher-specific keywords count, so the SQL in the embedding cache —
    which is parameterised with ``?`` placeholders — is not flagged.

    Statements marked ``# cypher-audit: ok`` on any of their lines are exempt.
    That is for labels and relationship types, which Cypher genuinely cannot
    parameterise and which must go through :func:`escape_identifier`.
    """
    findings: list[str] = []
    for path in paths:
        path = Path(path)
        candidates = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in candidates:
            try:
                source = file.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (OSError, UnicodeDecodeError, SyntaxError):  # pragma: no cover
                continue
            lines = source.splitlines()
            marker_lines = [
                number
                for number, line in enumerate(lines, start=1)
                if AUDIT_EXEMPTION in line
            ]
            exempt_ranges = _exempt_ranges(tree, marker_lines)
            for node in ast.walk(tree):
                if not _is_interpolation(node):
                    continue
                if not _CYPHER_ONLY_RE.search(_static_text(node)):
                    continue
                start = getattr(node, "lineno", 0)
                if any(low <= start <= high for low, high in exempt_ranges):
                    continue
                snippet = lines[start - 1].strip() if 0 < start <= len(lines) else ""
                findings.append(f"{file}:{start}: {snippet}")
    return findings


def _exempt_ranges(tree: ast.AST, marker_lines: Sequence[int]) -> list[tuple[int, int]]:
    """Line ranges covered by an exemption marker.

    Each marker exempts the *smallest* statement enclosing it. Implicit string
    concatenation makes a multi-line query one expression whose range stops
    before the closing parenthesis, so a trailing ``# cypher-audit: ok`` would
    otherwise fall outside it. Taking the smallest enclosing statement covers
    that without letting a marker buried in a function body exempt the entire
    function.
    """
    if not marker_lines:
        return []
    statements = [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
    ]
    ranges: list[tuple[int, int]] = []
    for line in marker_lines:
        enclosing = [r for r in statements if r[0] <= line <= r[1]]
        if enclosing:
            ranges.append(min(enclosing, key=lambda r: r[1] - r[0]))
    return ranges


def _is_interpolation(node: ast.AST) -> bool:
    """True for f-strings, ``.format(...)`` calls, and ``%`` formatting."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        return isinstance(func, ast.Attribute) and func.attr == "format"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
    return False


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


class Neo4jStorage:
    """Async Neo4j client. One per process; the driver pools connections."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        driver: Any | None = None,
        database: str | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.config = config or get_config()
        self.database = database or self.config.NEO4J_DATABASE
        self.retry_policy = retry_policy or self.config.llm_retry_policy()
        self._owns_driver = driver is None
        self._driver = driver or AsyncGraphDatabase.driver(
            self.config.NEO4J_URI,
            auth=(self.config.NEO4J_USER, self.config.neo4j_password),
            max_connection_pool_size=self.config.NEO4J_MAX_POOL_SIZE,
            connection_acquisition_timeout=self.config.NEO4J_CONNECTION_TIMEOUT,
        )

    # -- lifecycle ----------------------------------------------------------

    async def verify_connectivity(self) -> None:
        """Raise unless the server is reachable and the credentials work."""
        await self._driver.verify_connectivity()

    async def server_info(self) -> dict[str, Any]:
        rows = await self.read(
            "CALL dbms.components() YIELD name, versions, edition "
            "RETURN name, versions[0] AS version, edition"
        )
        if not rows:
            return {}
        row = rows[0]
        return {
            "name": row.get("name"),
            "version": row.get("version"),
            "edition": row.get("edition"),
        }

    async def aclose(self) -> None:
        if self._owns_driver:
            await self._driver.close()

    # -- queries ------------------------------------------------------------

    async def read(self, cypher: str, /, **params: Any) -> list[dict[str, Any]]:
        """Run a read query and return rows as plain dicts.

        Records are materialised before the session closes — a lazily consumed
        result would raise after the session is gone, and callers should not
        have to know that.
        """
        return await self._execute(cypher, params, write=False)

    async def write(self, cypher: str, /, **params: Any) -> list[dict[str, Any]]:
        """Run a write query in a write transaction."""
        return await self._execute(cypher, params, write=True)

    async def run_batch(
        self, cypher: str, rows: Sequence[Mapping[str, Any]], /, **params: Any
    ) -> list[dict[str, Any]]:
        """Apply one query across many rows via ``UNWIND $rows``.

        Phase 3 writes entities in the thousands. One round trip per node
        would dominate ingestion time; this is the shape that avoids it.
        """
        if not rows:
            return []
        return await self.write(cypher, rows=list(rows), **params)

    async def _execute(
        self, cypher: str, params: Mapping[str, Any], *, write: bool
    ) -> list[dict[str, Any]]:
        self._reject_unparameterised(cypher, params)

        async def attempt() -> list[dict[str, Any]]:
            async with self._driver.session(database=self.database) as session:
                runner = session.execute_write if write else session.execute_read
                return await runner(_collect, cypher, dict(params))

        try:
            return await retry_async(
                attempt,
                policy=self.retry_policy,
                description=f"Neo4j {'write' if write else 'read'}",
            )
        except DriverError as exc:
            raise StorageError(f"{exc.__class__.__name__}: {exc}") from exc

    @staticmethod
    def _reject_unparameterised(cypher: str, params: Mapping[str, Any]) -> None:
        """Catch the obvious mistake of passing a pre-formatted query.

        Not a parser and not a security boundary — the real guarantee is that
        the API only accepts values as keyword parameters. This just refuses
        the one shape that silently looks fine: a query carrying a `$name`
        placeholder that nobody supplied.
        """
        referenced = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", cypher))
        missing = referenced - set(params)
        if missing:
            raise ValueError(
                f"Cypher references parameter(s) {sorted(missing)} that were not "
                f"supplied. Pass values as keyword arguments; never interpolate "
                f"them into the query."
            )


async def _collect(tx: Any, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    result = await tx.run(cypher, params)
    records = [record.data() async for record in result]
    return records
