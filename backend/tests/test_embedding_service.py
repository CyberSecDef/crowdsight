"""Phase 2 Step 3 — dimensionality, batching, and the cache.

The HTTP layer is an ``httpx.MockTransport`` that records every request, so
batch shapes are asserted from what actually went over the wire rather than
from an internal counter.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.storage.embedding_service import (
    EmbeddingCache,
    EmbeddingError,
    EmbeddingService,
    cache_key,
)
from app.utils.retry import RetryPolicy

DIM = 768


def vector(seed: int, dim: int = DIM) -> list[float]:
    return [((seed * 7 + i) % 1000) / 1000.0 for i in range(dim)]


class Recorder:
    """MockTransport handler that records requests and shapes replies."""

    def __init__(self, dim: int = DIM, shape: str = "batch") -> None:
        self.requests: list[dict] = []
        self.dim = dim
        self.shape = shape

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        count = len(body["input"]) if isinstance(body.get("input"), list) else 1
        if self.shape == "legacy":
            return httpx.Response(200, json={"embedding": vector(1, self.dim)})
        if self.shape == "short":
            return httpx.Response(200, json={"embeddings": [vector(1, self.dim)]})
        return httpx.Response(
            200, json={"embeddings": [vector(i, self.dim) for i in range(count)]}
        )

    @property
    def batch_sizes(self) -> list[int]:
        return [len(r["input"]) for r in self.requests]

    @property
    def paths(self) -> list[str]:
        return [str(r) for r in self.requests]


@pytest.fixture
def make_service(config):
    created: list[EmbeddingService] = []

    def _make(recorder: Recorder | None = None, **kwargs):
        recorder = recorder or Recorder()
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(recorder), base_url=config.EMBEDDING_BASE_URL
        )
        kwargs.setdefault("cache", None)
        service = EmbeddingService(config, http=http, **kwargs)
        created.append(service)
        return service, recorder

    yield _make


@pytest.fixture
def cache(tmp_path):
    store = EmbeddingCache(tmp_path / "embeddings.db")
    yield store
    store.close()


# --------------------------------------------------------------------------
# Shape and dimensionality
# --------------------------------------------------------------------------


async def test_returns_one_768_dim_vector_per_input(make_service):
    service, _ = make_service()
    vectors = await service.embed_texts(["alpha", "beta"])
    assert len(vectors) == 2
    assert all(len(v) == DIM for v in vectors)


async def test_embed_returns_a_bare_vector(make_service):
    service, _ = make_service()
    assert len(await service.embed("alpha")) == DIM


async def test_request_targets_the_batch_endpoint(make_service):
    """/api/embeddings takes a single prompt and cannot batch at all."""
    service, recorder = make_service()
    await service.embed_texts(["alpha"])
    assert isinstance(recorder.requests[0]["input"], list)
    assert recorder.requests[0]["model"] == "nomic-embed-text"


async def test_wrong_dimensionality_is_rejected(make_service):
    """A silent dimension change poisons every comparison in the graph."""
    service, _ = make_service(Recorder(dim=512))
    with pytest.raises(EmbeddingError, match="512"):
        await service.embed_texts(["alpha"])


async def test_vector_count_mismatch_is_rejected(make_service):
    service, _ = make_service(Recorder(shape="short"))
    with pytest.raises(EmbeddingError):
        await service.embed_texts(["alpha", "beta"])


async def test_legacy_single_vector_shape_is_accepted(make_service):
    service, _ = make_service(Recorder(shape="legacy"))
    vectors = await service.embed_texts(["only"])
    assert len(vectors) == 1 and len(vectors[0]) == DIM


async def test_unrecognised_response_is_rejected(config):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1})),
        base_url=config.EMBEDDING_BASE_URL,
    )
    service = EmbeddingService(config, http=http, cache=None)
    with pytest.raises(EmbeddingError):
        await service.embed_texts(["alpha"])


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------


async def test_batching_splits_correctly(make_service):
    service, recorder = make_service(batch_size=3)
    vectors = await service.embed_texts([f"t{i}" for i in range(7)])
    assert recorder.batch_sizes == [3, 3, 1]
    assert len(vectors) == 7


async def test_single_batch_when_under_the_limit(make_service):
    service, recorder = make_service(batch_size=100)
    await service.embed_texts([f"t{i}" for i in range(7)])
    assert recorder.batch_sizes == [7]


async def test_batch_of_one(make_service):
    service, recorder = make_service(batch_size=1)
    await service.embed_texts(["a", "b", "c"])
    assert recorder.batch_sizes == [1, 1, 1]


async def test_duplicates_within_a_call_are_embedded_once(make_service):
    """Chunk overlap means the same sentence recurs constantly."""
    service, recorder = make_service()
    vectors = await service.embed_texts(["same", "other", "same", "same"])
    assert recorder.requests[0]["input"] == ["same", "other"]
    assert vectors[0] == vectors[2] == vectors[3]
    assert vectors[0] != vectors[1]


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("texts", [[""], ["   "], ["ok", ""]])
async def test_empty_text_is_rejected(make_service, texts):
    service, _ = make_service()
    with pytest.raises(ValueError):
        await service.embed_texts(texts)


async def test_empty_list_is_a_no_op(make_service):
    service, recorder = make_service()
    assert await service.embed_texts([]) == []
    assert recorder.requests == []


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


async def test_cache_returns_without_a_second_http_call(make_service, cache):
    service, recorder = make_service(cache=cache)
    first = await service.embed_texts(["alpha", "beta"])
    calls = len(recorder.requests)
    second = await service.embed_texts(["alpha", "beta"])
    assert len(recorder.requests) == calls
    assert second == first


async def test_cached_and_fresh_vectors_are_identical(make_service, cache):
    """A vector's value must not depend on where it came from.

    Returning float64 fresh and float32 cached shifts similarity thresholds
    between the first run and every later one.
    """
    service, _ = make_service(cache=cache)
    fresh = await service.embed_texts(["alpha"])
    cached = await service.embed_texts(["alpha"])
    assert fresh == cached


async def test_partial_hit_sends_only_the_miss(make_service, cache):
    service, recorder = make_service(cache=cache)
    await service.embed_texts(["alpha"])
    recorder.requests.clear()
    await service.embed_texts(["alpha", "gamma"])
    assert recorder.requests[0]["input"] == ["gamma"]


async def test_cache_persists_across_service_instances(make_service, cache, tmp_path):
    service, _ = make_service(cache=cache)
    original = await service.embed_texts(["alpha"])

    reopened = EmbeddingCache(tmp_path / "embeddings.db")
    service2, recorder2 = make_service(cache=reopened)
    again = await service2.embed_texts(["alpha"])
    reopened.close()

    assert recorder2.requests == []
    assert again == original


async def test_explicit_none_disables_the_cache(config, tmp_path):
    """`cache=None` must mean no cache, not 'fall back to the config default'.

    Collapsing those makes caching impossible to disable and silently shares
    one on-disk file between everything that tries.
    """
    recorder = Recorder()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder), base_url=config.EMBEDDING_BASE_URL
    )
    service = EmbeddingService(config, http=http, cache=None)
    assert service.cache is None
    await service.embed_texts(["alpha"])
    await service.embed_texts(["alpha"])
    assert len(recorder.requests) == 2


def test_cache_key_includes_model_and_dimension():
    base = cache_key("nomic-embed-text", 768, "alpha")
    assert base == cache_key("nomic-embed-text", 768, "alpha")
    assert base != cache_key("other-model", 768, "alpha")
    assert base != cache_key("nomic-embed-text", 1024, "alpha")
    assert base != cache_key("nomic-embed-text", 768, "beta")


def test_blob_codec_round_trips():
    original = vector(3)
    blob = EmbeddingCache.encode(original)
    assert len(blob) == DIM * 4  # float32, not JSON
    decoded = EmbeddingCache.decode(blob)
    assert len(decoded) == DIM
    assert all(abs(a - b) < 1e-6 for a, b in zip(original, decoded))


def test_cache_prune_is_selective(cache):
    cache.put_many([
        (cache_key("m1", DIM, "a"), "m1", DIM, vector(0)),
        (cache_key("m2", DIM, "a"), "m2", DIM, vector(1)),
    ])
    assert cache.count() == 2
    assert cache.prune(model="m1") == 1
    assert cache.count() == 1
    assert cache.prune() == 1
    assert cache.count() == 0


def test_cache_stats_track_hits_and_misses(cache):
    key = cache_key("m", DIM, "a")
    cache.get_many([key])
    cache.put_many([(key, "m", DIM, vector(0))])
    cache.get_many([key])
    assert cache.stats() == {"hits": 1, "misses": 1, "writes": 1}


# --------------------------------------------------------------------------
# Retry integration
# --------------------------------------------------------------------------


async def test_transient_failure_is_retried(config):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, json={"error": "loading model"})
        return httpx.Response(200, json={"embeddings": [vector(0)]})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=config.EMBEDDING_BASE_URL
    )
    service = EmbeddingService(
        config, http=http, cache=None, retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0)
    )
    vectors = await service.embed_texts(["alpha"])
    assert len(vectors[0]) == DIM
    assert state["n"] == 2


async def test_client_error_is_not_retried(config):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(400, json={"error": "bad model"})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=config.EMBEDDING_BASE_URL
    )
    service = EmbeddingService(
        config, http=http, cache=None, retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await service.embed_texts(["alpha"])
    assert state["n"] == 1
