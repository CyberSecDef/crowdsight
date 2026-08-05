"""Text embeddings from local Ollama, batched and cached on disk.

Embeddings are used twice in this system and both uses are high-volume:
deduplicating entities across chunks during extraction (Phase 3), and semantic
search over the graph (Phase 3 Step 6). A 50 MB document at 500-character
chunks is on the order of 100,000 chunks, so the two economies here are not
micro-optimisations — they are the difference between minutes and hours.

**Batching.** Ollama's ``/api/embed`` accepts an array and returns one vector
per input, so a batch is one round trip rather than N. The older
``/api/embeddings`` takes a single ``prompt`` and cannot batch at all; this
service targets ``/api/embed`` and falls back to reading the legacy response
shape if the server returns it.

**Caching.** Re-embedding unchanged text across runs is pure waste — rebuild a
graph from the same document and every chunk is identical. The cache is a
SQLite file keyed by ``sha256(model|dim|text)``. The model and dimension are
part of the key deliberately: swapping ``nomic-embed-text`` for another model
must miss rather than silently return vectors from a different vector space,
where cosine similarity is meaningless.

Vectors are stored as raw float32 blobs, roughly 3 KB each against ~15 KB as
JSON. At 100,000 chunks that is 300 MB versus 1.5 GB.

SQLite calls run in a worker thread. They are fast but not instant, and
blocking the event loop while 4 in-flight embed requests wait on the GPU would
be a poor trade.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import time
from array import array
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

from app.config import Config, get_config
from app.utils.retry import RetryPolicy, get_llm_gate, retry_async

logger = logging.getLogger(__name__)

__all__ = ["EmbeddingCache", "EmbeddingError", "EmbeddingService", "cache_key"]

Vector = list[float]

# Distinguishes "caller passed cache=None to disable caching" from "caller said
# nothing". Without it, None means both, and disabling the cache is impossible.
_UNSET: Any = object()


class EmbeddingError(RuntimeError):
    """The embedding backend returned something unusable."""


def cache_key(model: str, dim: int, text: str) -> str:
    """Stable key for one text under one model.

    Model and dimension are in the key, not merely stored alongside it, so a
    configuration change cannot produce a hit against a foreign vector space.
    """
    digest = hashlib.sha256()
    digest.update(f"{model}|{dim}|".encode("utf-8"))
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


class EmbeddingCache:
    """SQLite-backed vector cache.

    One file rather than one file per vector: tens of thousands of tiny files
    strain the filesystem and turn pruning into a directory walk.
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS embeddings (
            key        TEXT PRIMARY KEY,
            model      TEXT NOT NULL,
            dim        INTEGER NOT NULL,
            vector     BLOB NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        # WAL lets a simulation process read while the API process writes.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(self.SCHEMA)

    # -- blob codec ---------------------------------------------------------

    @staticmethod
    def encode(vector: Sequence[float]) -> bytes:
        return array("f", vector).tobytes()

    @staticmethod
    def decode(blob: bytes) -> Vector:
        values = array("f")
        values.frombytes(blob)
        return values.tolist()

    # -- operations ---------------------------------------------------------

    def get_many(self, keys: Sequence[str]) -> dict[str, Vector]:
        if not keys:
            return {}
        found: dict[str, Vector] = {}
        # Chunked to stay under SQLite's variable limit on large batches.
        for start in range(0, len(keys), 500):
            window = keys[start : start + 500]
            placeholders = ",".join("?" * len(window))
            rows = self._connection.execute(
                f"SELECT key, vector FROM embeddings WHERE key IN ({placeholders})",
                window,
            ).fetchall()
            for key, blob in rows:
                found[key] = self.decode(blob)
        self.hits += len(found)
        self.misses += len(keys) - len(found)
        return found

    def put_many(self, items: Iterable[tuple[str, str, int, Sequence[float]]]) -> None:
        rows = [
            (key, model, dim, self.encode(vector), time.time())
            for key, model, dim, vector in items
        ]
        if not rows:
            return
        self._connection.executemany(
            "INSERT OR REPLACE INTO embeddings (key, model, dim, vector, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self.writes += len(rows)

    def count(self) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    def prune(self, model: str | None = None) -> int:
        if model is None:
            cursor = self._connection.execute("DELETE FROM embeddings")
        else:
            cursor = self._connection.execute(
                "DELETE FROM embeddings WHERE model = ?", (model,)
            )
        return cursor.rowcount

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}

    def close(self) -> None:
        self._connection.close()


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


class EmbeddingService:
    """Embeds text via local Ollama, with batching and an on-disk cache."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        cache: EmbeddingCache | None = _UNSET,
        gate: Any | None = None,
        retry_policy: RetryPolicy | None = None,
        batch_size: int | None = None,
    ) -> None:
        self.config = config or get_config()
        self.model = self.config.EMBEDDING_MODEL
        self.dim = self.config.EMBEDDING_DIM
        self.batch_size = batch_size or self.config.EMBEDDING_BATCH_SIZE
        self.retry_policy = retry_policy or self.config.llm_retry_policy()
        # Shared with the LLM client: both contend for the same GPU.
        self._gate = gate or get_llm_gate(self.config.LLM_CONCURRENCY)
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=self.config.EMBEDDING_BASE_URL.rstrip("/"),
            timeout=httpx.Timeout(
                self.config.LLM_TIMEOUT, connect=self.config.LLM_CONNECT_TIMEOUT
            ),
        )
        # An explicit cache= wins, including an explicit None meaning "no
        # cache". Only when the caller says nothing do we consult config.
        if cache is not _UNSET:
            self.cache: EmbeddingCache | None = cache
            self._owns_cache = False
        elif self.config.EMBEDDING_CACHE_ENABLED:
            self.cache = EmbeddingCache(self.config.EMBEDDING_CACHE_PATH)
            self._owns_cache = True
        else:
            self.cache = None
            self._owns_cache = False

    # -- public -------------------------------------------------------------

    async def embed(self, text: str) -> Vector:
        """Embed a single text."""
        return (await self.embed_texts([text]))[0]

    async def embed_texts(self, texts: Sequence[str]) -> list[Vector]:
        """Embed many texts, returning vectors in the order given.

        Duplicates within one call are embedded once. Chunk overlap means the
        same sentence recurs constantly, and paying for it twice in a single
        request is avoidable.
        """
        if not texts:
            return []
        for index, text in enumerate(texts):
            if not text or not text.strip():
                raise ValueError(f"cannot embed empty text at position {index}")

        keys = [cache_key(self.model, self.dim, text) for text in texts]

        cached: dict[str, Vector] = {}
        if self.cache is not None:
            cached = await asyncio.to_thread(self.cache.get_many, keys)

        # Preserve first-seen order so batches stay deterministic and testable.
        pending: dict[str, str] = {}
        for key, text in zip(keys, texts):
            if key not in cached and key not in pending:
                pending[key] = text

        if pending:
            pending_keys = list(pending)
            pending_texts = [pending[key] for key in pending_keys]
            vectors = await self._embed_uncached(pending_texts)
            fresh = dict(zip(pending_keys, vectors))
            cached.update(fresh)
            if self.cache is not None:
                await asyncio.to_thread(
                    self.cache.put_many,
                    [(key, self.model, self.dim, vec) for key, vec in fresh.items()],
                )

        return [cached[key] for key in keys]

    # -- internals ----------------------------------------------------------

    def _batches(self, texts: Sequence[str]) -> list[Sequence[str]]:
        return [
            texts[start : start + self.batch_size]
            for start in range(0, len(texts), self.batch_size)
        ]

    async def _embed_uncached(self, texts: Sequence[str]) -> list[Vector]:
        batches = self._batches(texts)
        logger.debug(
            "Embedding %d text(s) in %d batch(es) of up to %d",
            len(texts), len(batches), self.batch_size,
        )
        # The gate bounds how many of these are actually in flight.
        results = await asyncio.gather(*(self._embed_batch(b) for b in batches))
        vectors: list[Vector] = []
        for batch in results:
            vectors.extend(batch)
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"Requested {len(texts)} embeddings but received {len(vectors)}"
            )
        return vectors

    async def _embed_batch(self, texts: Sequence[str]) -> list[Vector]:
        payload = {"model": self.model, "input": list(texts)}

        async def attempt() -> httpx.Response:
            async with self._gate():
                response = await self._http.post("/api/embed", json=payload)
                response.raise_for_status()
                return response

        response = await retry_async(
            attempt,
            policy=self.retry_policy,
            description=f"Ollama embeddings ({self.model})",
        )
        return self._vectors_from(response.json(), expected=len(texts))

    def _vectors_from(self, body: Any, expected: int) -> list[Vector]:
        """Read either the batch or the legacy single-vector response shape."""
        if not isinstance(body, dict):
            raise EmbeddingError(f"Unexpected embeddings response: {body!r}")

        if "embeddings" in body:
            vectors = body["embeddings"]
        elif "embedding" in body:  # legacy /api/embeddings shape
            vectors = [body["embedding"]]
        else:
            raise EmbeddingError(
                f"Embeddings response contained neither 'embeddings' nor "
                f"'embedding': {sorted(body)}"
            )

        if not isinstance(vectors, list) or len(vectors) != expected:
            raise EmbeddingError(
                f"Expected {expected} vector(s), got "
                f"{len(vectors) if isinstance(vectors, list) else type(vectors).__name__}"
            )

        for position, vector in enumerate(vectors):
            if not isinstance(vector, list):
                raise EmbeddingError(f"Vector {position} is not a list")
            if len(vector) != self.dim:
                raise EmbeddingError(
                    f"Vector {position} has {len(vector)} dimensions, expected "
                    f"{self.dim}. EMBEDDING_MODEL={self.model!r} may not match "
                    f"EMBEDDING_DIM; mixing dimensionalities makes cosine "
                    f"similarity meaningless."
                )
        # Quantise to float32, the precision the cache stores. Embedding
        # models emit float32 anyway, so nothing is lost — but without this a
        # vector's value would depend on whether it came from the cache or the
        # server, and similarity thresholds would shift under you on a rerun.
        return [array("f", vector).tolist() for vector in vectors]

    # -- lifecycle ----------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "dim": self.dim,
            "batch_size": self.batch_size,
            "cache": self.cache.stats() if self.cache else None,
        }

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
        if self._owns_cache and self.cache is not None:
            self.cache.close()
