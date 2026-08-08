# Requirement resolution

A step-by-step trace from [`REQ_SPEC.md`](REQ_SPEC.md) to the code that satisfies it.

For every one of the **53 steps** across **10 phases**: what the specification asked
for, how it was satisfied, where in the tree that lives, and what proves it. The
specification records *what was found* while building; this document records *what
exists now*, so a reader can check the claim rather than trust the narrative.

## How to read this

| Field | Meaning |
|---|---|
| **Required** | What the specification asks for, in its own terms |
| **Satisfied by** | How the requirement is met, and any deviation from the letter of the spec |
| **Where** | The files that carry it |
| **Verified by** | The tests or checks that would fail if it stopped being true |

**Status** is one of:

| | |
|---|---|
| ✅ | Satisfied as specified |
| ⚠️ | Satisfied, with a deviation or limitation recorded in the entry |
| ❌ | Not satisfied |

A deviation is not a failure — several were deliberate decisions taken during the
build, and each says why. What matters is that nothing is claimed to be covered when
it is not.

## Progress

Entries are filled in phase by phase. This table is the index; each phase links to its
section below.

| Phase | Title | Steps | Resolved |
|---|---|---|---|
| [1](#phase-1) | Foundation, sealed networking, and the configuration contract | 4 | **4 / 4** — 3 ✅, 1 ⚠️ |
| [2](#phase-2) | Local service layer — Ollama and Neo4j clients | 5 | **5 / 5** — 5 ✅ |
| [3](#phase-3) | Document ingestion and knowledge graph construction | 8 | **8 / 8** — 6 ✅, 2 ⚠️ |
| [4](#phase-4) | Agent profile generation | 5 | **5 / 5** — 5 ✅ |
| [5](#phase-5) | Simulation configuration generation | 4 | **4 / 4** — 3 ✅, 1 ⚠️ |
| [6](#phase-6) | Simulation execution engine | 6 | **6 / 6** — 5 ✅, 1 ⚠️ |
| [7](#phase-7) | Monitoring, data access, and agent interviews | 5 | _pending_ |
| [8](#phase-8) | Report generation | 4 | _pending_ |
| [9](#phase-9) | Frontend | 7 | _pending_ |
| [10](#phase-10) | Integration testing, egress verification, and operations | 5 | _pending_ |
| | **Total** | **53** | **32 / 53** |

---

## Phase 1

**Foundation, sealed networking, and the configuration contract**

> Specification: [`REQ_SPEC.md` line 78](REQ_SPEC.md#L78)

### 1.1 — Repository scaffold

> Specification: [line 80](REQ_SPEC.md#L80)

**Status:** ✅ Satisfied as specified

**Required:** The project skeleton — `backend/app/{api,services,storage,utils,models}`, `backend/tests`,
`frontend/src`, `data/{uploads,graphs,simulations,reports}`, plus `docker-compose.yml`,
`Dockerfile`, `backend/requirements.txt`, `backend/requirements-dev.txt`, `.env.example`,
`README.md`; git initialised; `.gitignore` covering `.env`, `data/`, `__pycache__`,
`node_modules` and `*.db`.

**Satisfied by:** All eleven directories and all seven files exist, checked individually rather than by
eye. All five `.gitignore` patterns are present. Git is initialised with 69 commits.

**Where:** Repository root. `backend/app/models/` exists as specified but is unused — the project
puts pydantic models beside the services that own them (`app/services/profile_generator.py`,
`app/services/report_agent.py`) rather than in a shared package. The directory was not
removed, so the scaffold matches the spec literally.

**Verified by:** Directory and file existence checked one by one; `.gitignore` patterns matched by regex;
`git rev-parse` and `git log`. Nothing here is covered by an automated test — it is
structure, and its absence would fail every other test in the project.

### 1.2 — The configuration module

> Specification: [line 83](REQ_SPEC.md#L83)

**Status:** ⚠️ Satisfied, with two recorded default changes

**Required:** `backend/app/config.py` as the single source of truth on `pydantic-settings`; every named
setting with the specified default and type; `get_config()`/`reload_config()` raising a typed
`ConfigError` rather than leaking pydantic's `ValidationError`; and a model validator that
classifies every endpoint host and **refuses to start** on anything public, without ever
resolving DNS.

**Satisfied by:** Verified by constructing the configuration and reading each field back.

**Fifteen of seventeen named settings match the specification exactly**, including both
`SecretStr` fields and `MAX_CONTENT_LENGTH` at 52,428,800.

**Two differ, and the change is recorded in the specification itself** (`REQ_SPEC.md`
line 246): `CHUNK_SIZE` is 1500 rather than 500 and `CHUNK_OVERLAP` is 150 rather than 50.
The reasoning is there — 500 characters is about three sentences, so a relationship stated
across a sentence boundary was routinely severed, and chunk size sets ingestion cost because
each chunk is one extraction call. Both remain configurable. This is a deliberate revision
made during Phase 3, not drift.

**The perimeter guarantee holds in full.** All fourteen classification cases from the
specification's table return the documented class. Loopback and service names are accepted
silently; a private address is accepted with exactly one `PerimeterWarning` and one entry in
`perimeter_notes`; public hostnames, raw public IPs, a cloud Neo4j Aura URI and wrong schemes
on both the LLM and Neo4j endpoints are all refused. A missing `NEO4J_PASSWORD` is refused
rather than defaulted.

**DNS is never consulted** — proven by replacing `socket.getaddrinfo` with a function that
raises and constructing a configuration successfully.

One nuance worth stating precisely: the `ConfigError` contract belongs to
`get_config()`/`reload_config()`, which is what the specification says. Through those,
*every* failure including a type error arrives as `ConfigError`. Constructing `Config()`
directly surfaces pydantic's `ValidationError` for field-constraint violations, though
perimeter refusals raise `ConfigError` even there.

**Where:** `backend/app/config.py` — `Config`, `classify_host`, `ConfigError`, `PerimeterWarning`, `get_config`, `reload_config`

**Verified by:** `backend/tests/test_config_validation.py` — 27 tests, including `classify_host` exercised
directly as the specification asks, `test_classify_host_never_resolves_dns`,
`test_entry_points_raise_config_error_not_validation_error`, and
`test_config_error_reports_every_problem_at_once`. All pass.

Independently re-verified for this document by driving `Config()` and `reload_config()`
against every case in the specification's table.

### 1.3 — Sealed container networking

> Specification: [line 102](REQ_SPEC.md#L102)

**Status:** ✅ Satisfied as specified, including the spec's own correction

**Required:** Five services on a bridge network declared `internal: true`; GPU passthrough for Ollama;
a named volume, `NEO4J_AUTH` and heap settings for Neo4j; a stateless **gateway** on both
networks as the single acknowledged boundary; masquerade disabled on `edge`; published ports
bound to `127.0.0.1`; nginx resolving upstreams at request time with a placeholder on 502;
and a `docker-compose.provision.yml` overlay for the one-time model pull.

**Satisfied by:** The deployed topology is exactly the diagram in the specification.

`sealed` is `internal: true`. `ollama`, `neo4j`, `backend` and `frontend` are on it and
**publish no ports at all**. `gateway` alone spans `edge` and `sealed` and publishes
`:8080` and `:5000`, both bound to `${CROWDSIGHT_BIND:-127.0.0.1}`. `edge` carries
`com.docker.network.bridge.enable_ip_masquerade: "false"`.

Ollama has `deploy.resources.reservations.devices` with `driver: nvidia`, `count: all`,
`capabilities: [gpu]`. Neo4j has the `neo4j_data` named volume, `NEO4J_AUTH` bound to a
required `NEO4J_PASSWORD`, and configurable heap and page-cache settings.

The gateway config sets `resolver 127.0.0.11 valid=10s ipv6=off` and serves
`docker/gateway/placeholder.html` through `error_page 502 503 504 = @placeholder`.
`docker-compose.provision.yml` attaches Ollama to a separate routable `provisioning`
network and exists as a distinct file so using it is a deliberate act.

**The frontend is no longer profile-gated**, which satisfies rather than deviates from the
requirement: the gate was specified to last "until Phase 9 builds it", and Phase 9 Step 1
built it.

**Where:** `docker-compose.yml`, `docker-compose.provision.yml`, `docker/gateway/conf.d/default.conf`, `docker/gateway/placeholder.html`

**Verified by:** `backend/tests/test_network_isolation.py` — `test_sealed_network_is_internal`,
`test_backend_publishes_no_ports`, `test_gateway_has_no_outbound_tcp`, and
`test_backend_has_no_default_route`. These inspect the Docker daemon, so they run host-side:
**11 passed from the host.**

Re-read from `docker compose config` for this document rather than from the file, so what is
recorded is the resolved topology rather than the intent.

### 1.4 — Egress guard test unit

> Specification: [line 127](REQ_SPEC.md#L127)

**Status:** ✅ Satisfied as specified

**Required:** `tests/test_config_validation.py` covering acceptance, warnings, refusals, a missing
required variable and field constraints, with `classify_host` tested directly;
`tests/test_network_isolation.py` proving no outbound TCP, no external DNS, no default route
and agreement with `app.egress_check`, detecting its context automatically. **It must not
skip** — with no verifiable context it must fail with instructions. Topology assertions are a
separate category that may skip in-container. Plus a `dev` build target and a `pytest.ini`
with `--strict-markers`, `pythonpath = .` and the `integration` and `egress` markers.

**Satisfied by:** Both files exist — 27 and 7 tests. **88 passed** in-container with 3 skipped, and those
three are the topology assertions skipping with a message pointing at the host, which is what
the specification asks for. From the host, all 11 pass for real.

**The non-skip property was proven rather than assumed.** Running the suite with `docker`
genuinely absent from `PATH` — the unverifiable context the specification describes — produced
**11 failed, exit code 1**. It goes red, not green. The file contains a single `pytest.skip`,
at the topology guard the specification sanctions; every other unverifiable path is
`pytest.fail` with instructions.

`egress` is deliberately *not* in the `addopts` deselection list, so the seal proof runs in the
default suite and cannot become something anyone has to remember to ask for. The `dev` target
exists in the `Dockerfile` and Compose builds `target: ${BACKEND_TARGET:-dev}`. `pytest.ini`
carries `--strict-markers`, `pythonpath = .`, and all three markers registered with reasons.

**Where:** `backend/tests/test_config_validation.py`, `backend/tests/test_network_isolation.py`, `backend/pytest.ini`, `Dockerfile` (`FROM base AS dev`), `docker-compose.yml`

**Verified by:** The tests are themselves the verification. Run for this document in both contexts, plus the
deliberate negative test of the non-skip property described above.

---

## Phase 2

**Local service layer — Ollama and Neo4j clients**

> Specification: [`REQ_SPEC.md` line 141](REQ_SPEC.md#L141)

### 2.1 — LLM client

> Specification: [line 143](REQ_SPEC.md#L143)

**Status:** ✅ Satisfied as specified

**Required:** `backend/app/utils/llm_client.py` wrapping the OpenAI SDK at `LLM_BASE_URL`, exposing
`complete()` and `complete_json()`; async-first on `AsyncOpenAI` with a `SyncLLMClient`
facade owning a long-lived loop on a background thread; `max_retries=0` to the SDK; three
layers of JSON defence in cost order; `LLMJSONError` carrying every raw response and parser
error; `schema` accepting either a pydantic model or a JSON Schema mapping, injected as a
system message **appended** to any existing one.

**Satisfied by:** Probed directly rather than read.

**Local salvage works without a round trip** — fenced, prose-wrapped, nested and
top-level-array inputs all yield the embedded JSON, and a brace inside a string literal does
*not* end the scan (`{"a": "} not the end", "b": 2}` survives intact). Genuinely broken input
returns `None`, so the repair loop is entered only when it is actually needed.

**`LLMJSONError(attempts, errors)`** carries both lists in order.

**Both schema forms work**: a pydantic class returns a validated instance, a mapping returns
a validated dict, and a mapping violation reports every missing key at once rather than one
per round trip.

**The schema is appended, not substituted** — with an existing system prompt present, the
result has one system message that still contains the original persona.

`AsyncOpenAI` is constructed with `max_retries=0` and the comment naming `retry.py` as the
owner of retry policy. `_LoopThread` provides the long-lived background loop for
`SyncLLMClient`, and `test_sync_facade_survives_repeated_calls` guards the "Event loop is
closed" failure the specification describes.

**Where:** `backend/app/utils/llm_client.py` — `LLMClient`, `SyncLLMClient`, `_LoopThread`, `LLMJSONError`, `strip_code_fences`, `extract_json_candidate`, `_validate`, `_with_schema_instruction`

**Verified by:** `backend/tests/test_llm_client.py` — 25 tests using `respx` to intercept real HTTP rather
than substituting the client, so the SDK's own path is exercised. Named coverage includes
`test_fenced_json_is_stripped_without_a_retry`,
`test_prose_wrapped_json_is_salvaged_without_a_retry`,
`test_server_rejecting_response_format_downgrades_once`,
`test_existing_system_prompt_is_preserved` and `test_raises_after_exhausting_repairs`.
All pass.

### 2.2 — Retry, timeout, and concurrency control

> Specification: [line 158](REQ_SPEC.md#L158)

**Status:** ✅ Satisfied as specified

**Required:** `backend/app/utils/retry.py` with exponential backoff and **full jitter**; `is_transient`
true for connection resets, read timeouts, 429 and 5xx and false for other 4xx, re-raising
the original exception; SDK imports local and guarded; a concurrency gate with one semaphore
**per event loop**, acquired **inside** the retry loop, shared between chat and embeddings,
exposing `in_flight`, `peak_in_flight`, `total_acquired` and `total_waited`; five new
settings; and `retry_sync` alongside `retry_async`.

**Satisfied by:** **The classifier was tested against constructed exceptions**, not read: connection reset,
read timeout, connect error, 429, 500 and 503 all return `True`; 400, 404 and 422 all return
`False`.

**Backoff is genuinely jittered** — twelve draws at the same attempt number produced twelve
distinct delays. An undithered implementation would have produced one.

**The bound is observed, not assumed.** Twenty concurrent workers against a limit of 4 gave
`peak_in_flight=4`, `total_acquired=20`, `total_waited=16`.

**Semaphores are per loop**, keyed by `id(asyncio.get_running_loop())` — running the gate
from two different loops created two semaphores, which is what makes `SyncLLMClient`'s
background loop safe.

**The gate is acquired inside the retry loop.** It is not referenced in `retry_async` at
all; acquisition happens in `LLMClient._send`'s inner `attempt()` coroutine, which
`retry_async` calls. Backoff therefore happens outside the gate, so a coroutine sleeping
through a retry holds no slot. This is the correct shape for the requirement even though it
places the acquisition at the call site rather than in `retry.py`.

**SDK imports are guarded** via `_maybe_import`, with `openai`, `httpx` and `neo4j.exceptions`
resolved by name at classification time; none is imported at module top.

All five settings match their specified defaults exactly. `retry_sync` and `retry_async` are
both present.

**Where:** `backend/app/utils/retry.py` — `retry_async`, `retry_sync`, `is_transient`, `ConcurrencyGate`, `get_llm_gate`, `RetryPolicy`, `_maybe_import`; gate acquisition in `backend/app/utils/llm_client.py::LLMClient._send`

**Verified by:** `backend/tests/test_retry.py` — 24 tests covering backoff timing, the retry ceiling and the semaphore bound. All pass. Re-verified for this document by driving the gate with 20 concurrent workers and inspecting the counters.

### 2.3 — Embedding service

> Specification: [line 173](REQ_SPEC.md#L173)

**Status:** ✅ Satisfied as specified

**Required:** `backend/app/storage/embedding_service.py` calling `/api/embed` (not `/api/embeddings`)
with `nomic-embed-text`, returning 768-dim vectors; batching; an on-disk SQLite cache at
`data/cache/embeddings.db` storing raw float32 blobs with WAL, called from a worker thread;
keyed on `sha256(model|dim|text)`; fresh vectors quantised to float32; dimensionality
validated; duplicates collapsed within a call; four new settings; and an explicit
`cache=None` meaning "no cache", distinct from the argument being omitted.

**Satisfied by:** `/api/embed` is the endpoint, with the legacy `{"embedding": [...]}` shape read defensively
as a fallback.

**The cache key genuinely includes the model and the dimension** — verified by computing
keys for the same text under a different model and a different dimension and confirming all
three differ. A model swap therefore misses rather than returning a vector from another
vector space.

WAL, `asyncio.to_thread`, float32 quantisation, dimension validation and within-call
deduplication are all present.

**The `cache=None` distinction is implemented with a sentinel** — the default is a module-level
`object()`, so an explicitly passed `None` is distinguishable from omission and caching can
actually be disabled. All four settings match: `EMBEDDING_DIM` 768, `EMBEDDING_BATCH_SIZE` 32,
`EMBEDDING_CACHE_PATH` `data/cache/embeddings.db`, `EMBEDDING_CACHE_ENABLED` true.

**Where:** `backend/app/storage/embedding_service.py` — `EmbeddingService`, `EmbeddingCache`

**Verified by:** `backend/tests/test_embedding_service.py` — 24 tests covering dimensionality, batch splitting and the cache returning without a second HTTP call, using `respx`. All pass.

### 2.4 — Neo4j storage layer

> Specification: [line 190](REQ_SPEC.md#L190)

**Status:** ✅ Satisfied as specified

**Required:** `neo4j_storage.py` (pooling, session management, parameterised Cypher only) and
`neo4j_schema.py` (constraints, indexes, a vector index if supported, otherwise in-process
cosine); an async driver, one storage per process, records materialised before the session
closes; `escape_identifier` **validating** against `^[A-Za-z_][A-Za-z0-9_]{0,62}$`;
`audit_cypher_sources` working on the AST and matching Cypher-only keywords; vector-index
support established by trying and reading `SHOW INDEXES` back; `cosine_similarity` returning
`0.0` for a zero-magnitude vector; three new settings.

**Satisfied by:** **`escape_identifier` was tested against eight cases**: `Person`, `_x9` and a 63-character
name accepted; a 64-character name, `My Label; DROP`, `9lives`, an empty string and
`has-dash` all rejected. It validates rather than sanitises, as specified.

**`cosine_similarity` returns exactly `0.0`** for a zero-magnitude vector — no NaN to
propagate into a ranking — with 1.0 for identical and 0.0 for orthogonal vectors. It lives in
`neo4j_schema.py` rather than `neo4j_storage.py`, which matches the specification's own
sentence placing in-process cosine with the index concern.

**`audit_cypher_sources` walks the AST**, honours the `# cypher-audit: ok` marker, and keys
on the six Cypher-only keywords (`MATCH`, `MERGE`, `UNWIND`, `YIELD`, `DETACH`, `RETURN`)
without triggering on SQL-shared `WHERE` — confirmed by inspecting the function body. **Run
over the whole shipped tree it reports zero interpolated Cypher.**

Vector-index support is established by attempting creation and reading `SHOW INDEXES` back to
confirm the type is `VECTOR`, not by parsing a version string. All three settings match:
`NEO4J_DATABASE` `neo4j`, `NEO4J_MAX_POOL_SIZE` 50, `NEO4J_CONNECTION_TIMEOUT` 30.

**Where:** `backend/app/storage/neo4j_storage.py` — `Neo4jStorage`, `escape_identifier`, `audit_cypher_sources`; `backend/app/storage/neo4j_schema.py` — schema DDL, vector-index probe, `cosine_similarity`

**Verified by:** `backend/tests/test_neo4j_storage.py` — 21 tests, of which 11 are `integration`-marked and run
against the live Compose Neo4j. **10 unit + 11 integration, all pass.** The audit, identifier
and cosine tests are deliberately *not* integration-marked, as the specification requires.

### 2.5 — Service client test units

> Specification: [line 210](REQ_SPEC.md#L210)

**Status:** ✅ Satisfied as specified

**Required:** Four test files — `test_llm_client.py` (respx, not object substitution),
`test_embedding_service.py`, `test_neo4j_storage.py` (live Neo4j, namespaced by `graph_id`,
not testcontainers) and `test_retry.py`. Only server-dependent tests marked `integration`;
neither marker ever skips to pass.

**Satisfied by:** All four exist: 25, 24, 21 and 24 tests. **125 pass in the default run with 11 deselected;
those 11 are exactly the `integration`-marked Neo4j tests, and they pass against the live
server.**

`respx` is used in the LLM and embedding suites, so the SDK's real HTTP path is exercised
rather than a stub proving the code calls the stub.

**The marker policy holds where it matters**: `test_neo4j_storage.py` carries all 11
integration marks and the other three files carry none — the identifier, source-audit and
cosine tests run in the default loop, which is what the specification asks for.

**Where:** `backend/tests/test_llm_client.py`, `test_embedding_service.py`, `test_neo4j_storage.py`, `test_retry.py`

**Verified by:** Run for this document in both modes: 125 passed / 11 deselected unit, then 11 passed integration against the live Neo4j.

---

## Phase 3

**Document ingestion and knowledge graph construction**

> Specification: [`REQ_SPEC.md` line 224](REQ_SPEC.md#L224)

### 3.1 — File parsing

> Specification: [line 226](REQ_SPEC.md#L226)

**Status:** ⚠️ Satisfied, with one observed limitation in encoding detection

**Required:** `backend/app/utils/file_parser.py` for PDF, Markdown and plain text; encoding detection; the
50 MB cap and extension allowlist; normalised text plus metadata. Two-column PDFs ordered by
layout; scanned and password-protected PDFs rejected **by name**; Markdown reduced to prose;
NFKC normalisation with zero-width and soft-hyphen stripping; line-break de-hyphenation
restricted to lowercase-to-lowercase; and encoding detection gated on BOM, then strict UTF-8,
then statistical detection accepted only with coherence ≥ 0.1 or ~128 bytes.

**Satisfied by:** Driven against PDFs generated in memory rather than committed fixtures.

**All four rejections fire with the cause named**: a scanned page raises `UnparseableDocument`
saying "1 page(s) but yielded only 0 characters"; an encrypted PDF raises `EncryptedDocument`;
a `.exe` raises `UnsupportedFileType` listing what is permitted; an oversized file raises
`FileTooLarge` with both byte counts.

**Two-column ordering is correct** — a generated two-column page returns the left column
before the right, not spliced across the gutter.

**NFKC and invisible characters**: `The oﬃce​ of the ma­yor` (ligature, zero-width space, soft
hyphen) normalises to `The office of the mayor`.

**De-hyphenation is PDF-only** (`normalise_text(..., dehyphenate=False)` by default), which is
right — line-break hyphens are a typesetting artefact and a plain text file has none. On a
generated PDF, `govern-\nment` joins to `government`, `mayor-\nelect` collapses to
`mayorelect` (the limitation the specification explicitly accepts), and `Smith-\nJones` is
spared by the lowercase-to-lowercase rule.

**The limitation found.** Encoding gates are implemented exactly as specified, and all five
inputs tried decoded without error — including Japanese Shift-JIS, which scores zero coherence
and would be rejected by a coherence-only rule. But **a 156-byte genuine cp1252 input decoded
via another Latin codepage**: `\x93…\x94` became `ì…î` rather than smart quotes. The words are
intact and only punctuation is affected. Notably a *short* cp1252 input decoded correctly,
because it falls below the evidence floor and reaches the cp1252 fallback — so passing the
128-byte gate can produce a worse result than failing it. This is a property of statistical
detection rather than a departure from the specification, which anticipates imperfect
recovery, but it is a real observed defect and is recorded here rather than left implicit.

**Where:** `backend/app/utils/file_parser.py` — `parse_bytes`, `normalise_text`, `_LINE_BREAK_HYPHEN`, the encoding gates, `UnparseableDocument`/`EncryptedDocument`/`UnsupportedFileType`/`FileTooLarge`

**Verified by:** `backend/tests/test_file_parser.py` — 26 tests, with PDFs built in memory by `make_pdf` (single-column, two-column, scanned, encrypted). All pass.

### 3.2 — Chunking

> Specification: [line 243](REQ_SPEC.md#L243)

**Status:** ✅ Satisfied as specified

**Required:** Overlapping chunks preferring semantic boundaries; defaults 1500/150; `CHUNK_SIZE` a **hard
ceiling including the overlap**; every chunk an **exact slice** of the source; overlap backed
up over whole sentences; regex sentence splitting with an abbreviation list.

**Satisfied by:** **The two structural invariants were tested rather than inspected**, across three
size/overlap settings on a repeated multi-paragraph document:

| size | overlap | chunks | longest | ceiling held | exact slices |
|---|---|---|---|---|---|
| 1500 | 150 | 2 | 1420 | yes | yes |
| 400 | 80 | 8 | 398 | yes | yes |
| 200 | 40 | 12 | 184 | yes | yes |

`text == source[start:end]` held for **every chunk at every setting**, so Step 5's provenance
is exact rather than approximate. The ceiling was never exceeded, so the overlap is genuinely
inside it.

A document shorter than one chunk yields exactly one chunk.

**Abbreviation handling works on all five cases tried** — `Cllr. Jane Doe`, `J. R. Smith`,
`3.5`, `fig. 4` and `Dr. Patel and Mr. Woods` each split into two sentences, never severing
the title from the name.

At size 200 / overlap 40 only 4 of 11 chunk pairs overlap, which is the specification's own
rule working: a trailing sentence longer than the overlap budget is skipped rather than
truncated.

**Where:** `backend/app/utils/chunker.py` — `chunk_text`, `Chunk`, `iter_sentence_spans`, `_SENTENCE_END`, `_is_sentence_end`

**Verified by:** `backend/tests/test_chunking.py` — 20 tests. All pass. Re-verified here by asserting the exact-slice and ceiling properties directly across three configurations.

### 3.3 — Ontology generation

> Specification: [line 258](REQ_SPEC.md#L258)

**Status:** ⚠️ Satisfied, with a sampling limitation at small budgets

**Required:** `ontology_generator.py` proposing entity and relationship types from a document sample;
names normalised to PascalCase / UPPER_SNAKE_CASE / snake_case with the original kept as
`label`; relationships with unknown endpoints dropped; duplicates collapsed; the document
sampled from beginning, middle and end with elisions marked; temperature 0.2.

**Satisfied by:** **Normalisation and pruning verified on a constructed ontology**: `Public Figure` →
`PublicFigure` and `Local Government Body` → `LocalGovernmentBody`, both keeping their
original text as `label`; the attribute `Party Name` → `party_name`; `works for` →
`WORKS_FOR`; a duplicate `public figure` collapsed to one type; and `GOVERNS`, whose target
`Nonexistent` is not in the ontology, **dropped**.

Temperature defaults to 0.2 in `generate()`.

**The sampling limitation.** `build_sample` marks elisions with `[...]` and stays inside its
budget (1182 ≤ 1200, 2977 ≤ 3000), so the overrun the specification warns about does not
happen. But **at a 1200-character budget over a ~7000-character document, the closing section
was not represented** — the middle section consumed the remaining budget. At 3000 and above
all three sections appear. The default budget is **12000**, so this does not affect normal
operation; it is recorded because the specification's stated intent is beginning, middle *and*
end, and at small budgets that is not guaranteed.

**Where:** `backend/app/services/ontology_generator.py` — `Ontology`, `EntityType`, `RelationshipType`, `to_identifier`, `build_sample`, `OntologyGenerator.generate`

**Verified by:** `backend/tests/test_ontology_generator.py` — 24 tests, including the shared identifier-contract fixture also asserted by the frontend. All pass.

### 3.4 — Entity and relationship extraction

> Specification: [line 271](REQ_SPEC.md#L271)

**Status:** ✅ Satisfied as specified

**Required:** `ner_extractor.py` extracting per chunk against the ontology, deduplicating across chunks
and merging attributes. Deduplication **lexical**, not by embedding similarity: normalised
name plus multi-token suffix alias, with an embedding pass at 0.90 as a guarded net, merging
only within a type. Missing relationship endpoints materialised with a token guard and marked
`inferred`. Attribute conflicts resolved to first occurrence in document order with losers
kept as `attribute_conflicts`; fan-out via `asyncio.gather`.

**Satisfied by:** **The deduplication rules were run against the specification's own measured examples.**

`normalise_name` merges `Mayor Alan Reyes` / `Alan Reyes` (both → `alan reyes`) and
`The Riverbend Council` / `Riverbend Council`, while keeping `Jane Doe` / `John Doe` and
`Eastgate` / `Eastgate corridor` apart — the two pairs whose cosine scores made an embedding
threshold impossible.

`is_alias_of` behaves as specified on all four cases: `opposition councillor tom whitfield` ~
`tom whitfield` and `Riverbend Residents Association` ~ `Residents Association` are aliases;
`Mill Street conservation area` ~ `Mill Street` is **not** (the prefix case the specification
warns about); `Alan Reyes` ~ `Reyes` is **not** (the two-token minimum).

`ENTITY_SIMILARITY_THRESHOLD` is **0.9**, applied only when an embedding service is available
and guarded lexically. Merging is scoped to an ontology type. `inferred`,
`attribute_conflicts` and `asyncio.gather` are all present.

**Where:** `backend/app/storage/ner_extractor.py` — `normalise_name`, `is_alias_of`, `SUFFIXES`, `ChunkExtraction`

**Verified by:** `backend/tests/test_ner_extractor.py` — 19 tests, routing mocked extraction **by chunk content rather than call order**, which the specification records as a trap that silently affected seven cases. All pass.

### 3.5 — Graph construction

> Specification: [line 300](REQ_SPEC.md#L300)

**Status:** ✅ Satisfied as specified

**Required:** `graph_builder.py` persisting entities and relationships with `graph_id`, chunk references
and embeddings; provenance as a traversal `(:Entity)-[:MENTIONED_IN]->(:Chunk)-[:PART_OF]->(:Document)`;
chunk nodes storing offsets rather than text; idempotent rebuilds via UUID5 identifiers from a
fixed namespace; the ontology type as a second label through `escape_identifier`; attributes
under an `attr_` prefix; `attribute_conflicts` as JSON; `replace=True`; and schema constraints
on `Chunk.uuid`, `Document.graph_id` and `Chunk.graph_id`.

**Satisfied by:** Every element is present and located: `uuid5` with a fixed `NAMESPACE` constant, the
`MENTIONED_IN` / `PART_OF` traversal shape, `escape_identifier` for the second label and the
relationship type, the `attr_` prefix, `attribute_conflicts`, `replace`, and `SET e += $props`
keeping the dynamic attribute map parameterised.

Chunk nodes carry `start`/`end` offsets and the document is written once to
`data/graphs/<graph_id>/document.txt`, so the graph store never holds the text — which, given
chunks overlap, would otherwise be more than the whole document.

The three schema constraints idempotency depends on are declared in `neo4j_schema.py`.

**Where:** `backend/app/services/graph_builder.py`; `backend/app/storage/neo4j_schema.py`

**Verified by:** `backend/tests/test_graph_builder.py` — 16 tests including provenance traversal and rebuild idempotency, the integration set running against the live Neo4j. All pass.

### 3.6 — Graph query and search

> Specification: [line 324](REQ_SPEC.md#L324)

**Status:** ✅ Satisfied as specified

**Required:** `search_service.py` and `graph_storage.py` for fetch by graph/type/UUID, search, and
neighbourhood traversal. Search **hybrid** — a lexical arm ranked exact → prefix → alias →
substring, ordered before a vector arm, with `matched_by` on every hit; passages searchable
via chunk vectors; every query scoped to `graph_id`; traversal depth clamped 1–5 with a
`truncated` flag and paths constrained to `:Entity`; depth interpolated from a clamped integer
with an audit marker.

**Satisfied by:** All nine specified behaviours are present in the source: both arms with lexical first, the
four-way lexical ranking, `matched_by`, chunk/passage search, `graph_id` scoping throughout
`graph_storage.py`, the 1–5 depth clamp, the `truncated` flag, `:Entity`-constrained traversal,
and the `# cypher-audit: ok` marker on the one place depth must be interpolated because Cypher
cannot parameterise `*1..n`.

**Where:** `backend/app/storage/search_service.py` — `SearchHit`, `ChunkHit`; `backend/app/storage/graph_storage.py` — `Page`, `Subgraph`, `neighbours`, `subgraph`

**Verified by:** `backend/tests/test_graph_query.py` — 26 tests, added beyond the specification's list precisely so these behaviours cannot regress unnoticed: pagination and clamping, graph scoping, depth and node caps, the refusal to traverse through `:Chunk`, and the lexical-before-vector order. All pass.

### 3.7 — Graph API

> Specification: [line 342](REQ_SPEC.md#L342)

**Status:** ✅ Satisfied as specified

**Required:** `backend/app/api/graph.py` with upload, status, entities, entity detail, subgraph and delete,
plus list, metadata, entity-types, relationships, search, tasks and the ontology review pair.
Task state in SQLite with running tasks reaped on startup; uploads validated **inside the
request** before a task is created; ontology review opt-in via `review_ontology=true`; the two
phases communicating through the filesystem; typed errors (404 vs 400, never 500); provenance
on entity detail by default; shared process-wide clients.

**Satisfied by:** **All fifteen routes the specification names are registered**, confirmed by enumerating the
Flask URL map rather than reading the file:
`/api/graph` and `/api/graph/` (list), `/<graph_id>` (GET and DELETE), `/entities`,
`/entities/<uuid>`, `/entity-types`, `/relationships`, `/search`, `/subgraph`,
`/ontology` (GET and POST), `/status/<task_id>`, `/tasks`, `/upload`.

Task state is SQLite-backed with reaping on startup. `parse_bytes` is called **before**
`tasks.create` in the upload handler — verified by source position, so a rejected file is a
400 rather than a failed task to discover by polling. `review_ontology` parks the task and the
handoff is through `ontology_path` and `document.txt`. `GraphNotFound` maps to 404 and
provenance is included in entity detail by default.

**Where:** `backend/app/api/graph.py`; `backend/app/services/tasks.py`; `backend/app/services/runtime.py`

**Verified by:** `backend/tests/test_graph_api.py` — 26 tests across two layers: route shapes and error codes against a stub runtime, plus an `integration` test driving a real upload through to a built graph. All pass.

### 3.8 — Ingestion test units

> Specification: [line 359](REQ_SPEC.md#L359)

**Status:** ✅ Satisfied as specified

**Required:** Six named test files plus `test_graph_query.py`; API tests in two layers; PDF fixtures
generated in memory rather than committed; mocked extraction routed by chunk content, never by
call order.

**Satisfied by:** All seven files exist — 26, 20, 24, 19, 16, 26 and 26 tests. **216 pass with 45 deselected**,
the deselected set being the `integration`-marked tests that need Neo4j and Ollama.

`make_pdf` generates single-column, two-column, scanned and encrypted PDFs in memory, so a
reader can see and amend what a fixture contains. Extraction mocks are keyed on chunk content,
which the specification records as having silently affected seven cases when they were keyed on
call order — `asyncio.gather` consumes a `side_effect` list in completion order.

**Where:** `backend/tests/test_file_parser.py`, `test_chunking.py`, `test_ontology_generator.py`, `test_ner_extractor.py`, `test_graph_builder.py`, `test_graph_api.py`, `test_graph_query.py`

**Verified by:** Run together for this document: 216 passed, 45 deselected.

---

## Phase 4

**Agent profile generation**

> Specification: [`REQ_SPEC.md` line 377](REQ_SPEC.md#L377)

### 4.1 — Entity-to-persona mapping

> Specification: [line 379](REQ_SPEC.md#L379)

**Status:** ✅ Satisfied as specified

**Required:** `profile_generator.py` synthesising a persona per eligible entity — name, age, occupation,
background, Big-Five scores plus descriptive traits, interests, leanings, activity level and a
writing-style hint. Eligibility **classified from the ontology**, not a hard-coded `Person`
filter, with `Person` always kept as a fallback. An occupation taxonomy spanning the whole
spectrum, weighted so professionals are a minority, **assigned** round-robin rather than
suggested, with the sector normalised. Field and type drift **coerced rather than
re-prompted**. Temperature 0.8.

**Satisfied by:** **Every coercion rule was run against the drift the specification names.**

`age` accepted as `34`, `"34"`, `"thirty-four"`, `"34 years old"`, `"mid-thirties"` (→ 35) and
`"  41  "`; `"not a number at all"`, `None` and `""` rejected. Personality disambiguated on
shape exactly as specified: `0.7` kept, `1.4` clamped to 1.0, `8` read as 1–10 → 0.8, `80` read
as a percentage → 0.8, and the word `"high"` mapped to 0.75. Renamed fields `job`, `bio`,
`big_five`, `political_leaning` and `hobbies` all map onto the canonical names.

**The taxonomy is real and weighted as described**: 92 occupations across 9 sectors —
`skilled trades`, `manual and industrial`, `care and health`, `transport and logistics`,
`retail and hospitality`, `not in paid work` among them. Ordinary and trade occupations
(mechanic, carpenter, plumber, electrician, bus driver, shop assistant…) outnumber
professional ones **18 to 4**, so professionals are 4% of the pool. `_sector_for('carpenter')`
returns `skilled trades` and `_sector_for('bus driver')` returns `transport and logistics`, so
the sector is derived rather than left to the model.

Eligibility is classified into individuals and institutions with `Person` retained as a
fallback. Persona generation runs at `temperature=0.8` while the classification step runs at
0.0 — the split the specification asks for.

**Where:** `backend/app/services/profile_generator.py` — `PersonaProfile`, `BigFive`, `EntityRoles`, `ALL_OCCUPATIONS`, `OCCUPATION_SECTORS`, `_sector_for`, `ProfileGenerator`

**Verified by:** `backend/tests/test_profile_generator.py` (23) and `test_profile_normalization.py` (13). All pass.

### 4.2 — Population expansion

> Specification: [line 396](REQ_SPEC.md#L396)

**Status:** ✅ Satisfied as specified

**Required:** Synthetic expansion with a configurable named-to-synthetic ratio and provenance on every
profile. Synthetic names **collision-checked** against the graph using the same normalisation
that deduplicates entities, numbered on pool exhaustion rather than reused. The allocated name
**enforced, not suggested** — the generator overwrites whatever the model returns. A negative
statement in the prompt. A sketch call grounding the crowd with sampling done locally.
`POPULATION_NAMED_RATIO` 0.25, and a ratio of 0 still keeping one named actor.

**Satisfied by:** `POPULATION_NAMED_RATIO` is **0.25**, matching the specification exactly.

Name collision checking reuses Phase 3's `normalise_name`, so `Cllr. Jane Doe` in the graph
reserves `Jane Doe` for no synthetic agent. Pool exhaustion numbers rather than reuses.

**The name is enforced rather than suggested**, and the code says why at the point it happens:
"The allocated name is the safety property, not a suggestion". The specification records this
as a silent failure that only surfaces by asserting the model's own name is *absent* from the
output — which is how the tests check it.

The negative statement is present verbatim: personas are described as "NOT named in the source
document and are not a public figure". The sketch-then-sample structure is in place, and the
stance vocabulary includes indifference, so the crowd is not uniformly engaged.

**Where:** `backend/app/services/population.py` — `plan_population`, `sketch_population`; `backend/app/services/profile_generator.py` — the synthetic prompt and name enforcement

**Verified by:** `backend/tests/test_synthetic_expansion.py` — 24 tests covering the N-from-M count, provenance on every profile, the ratio, and the model's name being absent from the output. All pass.

### 4.3 — OASIS profile schema conformance

> Specification: [line 413](REQ_SPEC.md#L413)

**Status:** ✅ Satisfied as specified

**Required:** Emit the exact shapes OASIS reads: `twitter.csv` (CSV, not JSON) and `reddit.json`, plus a
richer `profiles.json` nothing in OASIS reads. Derive `gender`, `country` and `mbti`, which the
persona schema lacks but the Reddit agent's own system prompt interpolates — MBTI from the Big
Five by a documented projection, gender and country invented for synthetic agents but left
unstated for real named people. Phrase the placeholder to read correctly inside that sentence.
Validate what was written against the loaders' actual accesses. Pin `mcp>=1.9,<2`.

**Satisfied by:** **Emission verified by writing a real bundle and reading it back.** Three files appear:
`twitter.csv`, `reddit.json`, `profiles.json`.

`twitter.csv` is genuine CSV and carries every field the two Twitter loaders index —
`username`, `description`, `user_char`, `name`, `following_agentid_list`, `following_count`,
`followers_count`, `user_id`. `reddit.json` is a JSON list carrying `username`, `bio`,
`persona`, `mbti`, `gender`, `age`, `country`, with `age` an `int`.

**The placeholder phrasing is right.** `UNSTATED_GENDER = "person of unstated gender"` and the
country default `"the local area"` read correctly inside OASIS's own sentence — "You are a
person of unstated gender, 47 years old … from the local area" — rather than the
`"You are a unspecified"` the specification warns about. The named/synthetic distinction is
made at generation time (`gender_stated` is set only when the source states it), not in the
emitter, which writes what it is given.

MBTI is derived from the Big Five: the two test personas projected to `ENFJ` and `ISFJ`.

`mcp>=1.9,<2` is pinned in `requirements.txt` with the `FastMCP` import failure explained
above it.

**Where:** `backend/app/services/oasis_profiles.py` — `write_profiles`, `to_twitter_row`, `to_reddit_entry`, `derive_mbti`, `validate_twitter_csv`, `validate_reddit_json`, `UNSTATED_GENDER`; `backend/requirements.txt`

**Verified by:** `backend/tests/test_oasis_profile_contract.py` — 24 tests importing OASIS's real loaders and
`UserInfo.to_system_message()`, and running in the **default** suite (32 collected without a
marker filter).

**Independently mutation-tested for this document**, by corrupting the emitted files rather
than the code: dropping `mbti`, emitting `age` as a string, and blanking a `username` were all
caught with a `SchemaViolation` naming the exact field and why it matters. The conformance
check has teeth.

### 4.4 — Parallel generation with progress

> Specification: [line 434](REQ_SPEC.md#L434)

**Status:** ✅ Satisfied as specified

**Required:** Bounded parallel generation respecting the global Ollama semaphore, with per-profile
progress, partial-result persistence and resumability. Completed profiles appended to **JSONL,
flushed per record**. The plan persisted before generating and resumed against, fingerprinted
by what it will generate, refusing a resume whose fingerprint differs. The record written
before the profile is counted done. Failures simply not recorded, so a resume retries them. A
worker pool sized from `LLM_CONCURRENCY`. A named agent's name from the graph, never the
model.

**Satisfied by:** All seven mechanisms are present in `profile_job.py`: JSONL append with per-record flush, the
plan written before generation, a fingerprint over the names and assigned occupations, refusal
on fingerprint divergence, the write-then-count ordering, a worker pool sized from
`LLM_CONCURRENCY`, and a torn final line discarded on resume.

The guarantee that a named agent's name comes from the graph is the same enforcement path as
Step 2's — the allocated name overwrites the model's response in both directions.

**Where:** `backend/app/services/profile_job.py` — `generate_population`, the plan fingerprint and JSONL writer

**Verified by:** `backend/tests/test_profile_job.py` — 18 tests, added beyond the specification's list because
these guarantees only fail after a crash, when nobody is watching. Covers the plan
round-tripping with its assignments, fingerprint divergence, interruption and resume, a
**deliberately torn final line**, plan-mismatch refusal, bounded parallelism, and a failed
agent retried on resume. All pass.

### 4.5 — Profile test units

> Specification: [line 449](REQ_SPEC.md#L449)

**Status:** ✅ Satisfied as specified

**Required:** Four named test files plus `test_profile_job.py`; the OASIS contract test asserting against
the **real loaders** rather than a remembered schema, running in the default suite; and
evidence that the conformance test has teeth.

**Satisfied by:** All five files exist — 23, 13, 24, 24 and 18 tests. **167 pass together in 5.2 s**, none of
them `integration`-marked, so the whole phase is covered by the default loop.

The contract test imports `oasis.social_platform.config.user.UserInfo` and reads the files
"exactly how `generate_twitter_agent_graph` reads it" and "exactly how
`generate_reddit_agent_graph` reads it" — the real accesses, not a schema written from memory.
It collects without any marker filter, so it runs by default as the specification requires,
for the same reason the egress tests do.

**Where:** `backend/tests/test_profile_generator.py`, `test_profile_normalization.py`, `test_synthetic_expansion.py`, `test_oasis_profile_contract.py`, `test_profile_job.py`

**Verified by:** Run together for this document: 167 passed. The contract test's teeth re-confirmed by three independent output mutations, all caught.

---

## Phase 5

**Simulation configuration generation**

> Specification: [`REQ_SPEC.md` line 465](REQ_SPEC.md#L465)

### 5.1 — Scenario derivation

> Specification: [line 467](REQ_SPEC.md#L467)

**Status:** ✅ Satisfied as specified

**Required:** Derive from the graph and the document: the triggering event, a simulated time window and
round cadence, seed posts, and scheduled mid-run events. Exactly two attributions —
**broadcaster** (synthetic, name-checked against every graph entity) and **named_quote** (a
line the document actually contains). Verify the quote by searching the text rather than
trusting the label, with a minimum length; demote an unlocatable quote to the broadcaster
and record the reason. Scheduled events `counterfactual: true`, `enabled: false`. Drop events
past the final round, clamp rounds to `MAX_ROUNDS`, and normalise the broadcaster's name and
handle rather than merely filling them.

**Satisfied by:** Every clause holds, each probed by running it rather than reading it.

`find_verbatim()` flattens whitespace on both sides, lowercases, and returns offsets into the
**original** document — a quote given as `WE  WERE\n NOT   consulted about the riverside
DEVELOPMENT` located at `(38, 91)`, the same span as the exact text, and `DOC[38:91]` reads back as the real
sentence. `"the plan"` (8 characters) and `"the council"` (11) are refused by the 12-character
floor; a paraphrase of a sentence the document does contain returns `None`.

Attribution is a two-member `Literal`, so a third value cannot be constructed. `verify_scenario()`
recomputes the span for every `named_quote` — the recorded offsets are never read — and
`demote()` reassigns the post to the broadcaster, clears the speaker and offsets, and writes
`demoted_reason`. Content is kept, not dropped. A post that is not a `named_quote` has any
speaker and offsets stripped, so they cannot sit in the file looking like evidence.

Scheduled events default `counterfactual=True, enabled=False`; `enabled_events()` on a freshly
generated config returns `[]`, so a baseline run reflects the document alone. Events past the
last round are dropped by the model validator (rounds 3 and 9 with `rounds=5` → `[3]` kept) and
again by `set_rounds()`, which drops what it orphans — a bare `rounds` assignment leaves the
round-3 event stranded, which is the defect `set_rounds()` exists to prevent.

Rounds clamp twice: `min(rounds or MAX_ROUNDS, MAX_ROUNDS)` on the request and
`set_rounds(min(config.rounds, rounds))` on the model's answer. `MAX_ROUNDS` is 10 here; a
config asking for 999 lands on 10.

The broadcaster normalises rather than fills: `{"name": "@RB Echo"}` → name `RB Echo`, handle
`rb_echo`; `{"handle": "@RB_Echo"}` → `rb_echo` (no `@@`); punctuation collapses, and a
40-character name is truncated to a 24-character handle.

**Where:** `backend/app/services/simulation_config_generator.py` — `find_verbatim` (95), `Broadcaster` (142), `SeedPost` (173), `ScheduledEvent` (198), `SimulationConfig._events_fall_inside_the_run` (270), `set_rounds` (294), `demote` (398), `verify_scenario` (415), `SimulationConfigGenerator.generate` (530)

**Verified by:** Probed live against the running backend: whitespace/case matching, offset fidelity, the 12-character floor, the paraphrase refusal, event dropping at both sites, the counterfactual defaults, the `MAX_ROUNDS` clamp and handle normalisation each confirmed by execution. Covered by `tests/test_simulation_config.py`. Mutating `find_verbatim` to trust the label — the exact defect the design exists to prevent — fails **21 tests** across generation, the operator edit and the fork path.

### 5.2 — Action space configuration

> Specification: [line 485](REQ_SPEC.md#L485)

**Status:** ✅ Satisfied as specified

**Required:** The permitted action set per platform, matching OASIS's supported actions. Twitter:
`CREATE_POST, LIKE_POST, REPOST, FOLLOW, QUOTE_POST, DO_NOTHING`. Reddit: `LIKE_POST,
DISLIKE_POST, CREATE_POST, CREATE_COMMENT, LIKE_COMMENT, DISLIKE_COMMENT, SEARCH_POSTS,
SEARCH_USER, TREND, REFRESH, FOLLOW, MUTE, DO_NOTHING`. Include `DO_NOTHING`. Validate up
front, because OASIS warns and drops rather than raising.

**Satisfied by:** `TWITTER_ACTIONS` is the spec's Twitter list **element for element, in order**.
`REDDIT_ACTIONS` holds the same 13 members set-for-set, reordered so related actions sit
together (post/comment, then votes, then search) — a presentation choice with no behavioural
effect.

Checked against the installed camel-oasis, not against a remembered schema: `ActionType` has
**32** members, `AGENT_INVOKABLE` mirrors **29**, `ENGINE_ONLY` names the other three
(`EXIT`, `SIGNUP`, `UPDATE_REC_TABLE`), and `AGENT_INVOKABLE | ENGINE_ONLY == {a.name for a in
ActionType}` is exactly true. Every configured action is a real `ActionType`, and `to_oasis()`
returns real enum members.

Validation refuses, with a message that says why: `REPOST` on Reddit ("a valid OASIS action but
not part of the reddit action set"), `EXIT` ("driven by the OASIS engine and has no agent
tool"), `PURCHASE_PRODUCT` ("belongs to another OASIS scenario"), and `creat_post` ("is not an
OASIS action; did you mean `'CREATE_POST'`?"). `DO_NOTHING` cannot be removed, and cannot stand
alone. Case, whitespace and duplicates are normalised away first.

Inactivity is modelled twice as described. `ACTIVITY_PARTICIPATION` is `{low: 0.20, moderate:
0.55, high: 0.90}`; on a 300-agent crowd of 100 each, `select_active()` invoked **172** and
saved **128** inferences in that round, against an expected 165. An unknown activity level
falls to the middle (540/1000 at 0.55).

`SimulationConfig.set_platform()` moves platform and action space together — a bare
`platform = "reddit"` assignment leaves `action_space.platform == "twitter"`, which is the
hazard, and a config carrying a mismatched pair is refused outright. A model-supplied
`action_space` is discarded: `["CREATE_POST"]` comes back as the full Twitter set rather than
failing validation for a missing `DO_NOTHING`, while the same value supplied with
`context={"trusted": True}` is honoured.

The sealed-network fix is in place: the `Dockerfile` bakes four BPE encodings into
`TIKTOKEN_CACHE_DIR=/opt/tiktoken` at build time, and the running container holds all four.

**Where:** `backend/app/services/action_space.py`; `SimulationConfig.set_platform` and `_the_model_does_not_choose_the_action_space` in `simulation_config_generator.py`; `Dockerfile:41-44`

**Verified by:** `tests/test_action_space.py` — **45 tests**, four of which import the real `oasis` enum. Teeth confirmed by mutation: adding a phantom member to the mirror fails `test_our_mirror_of_the_enum_is_current`; removing `DO_NOTHING` from the Twitter set fails **14** tests.

### 5.3 — Config persistence and override

> Specification: [line 502](REQ_SPEC.md#L502)

**Status:** ⚠️ Satisfied, with one limitation recorded below

**Required:** Write the config to `data/simulations/<sim_id>/config.json` and expose it for operator review
and editing before the run. Re-verify an operator edit exactly as generated output is
verified; refuse a `named_quote` edit when the document is unavailable; freeze a started run's
config and fork an edit to a new `sim_id` recording `forked_from`; keep run state in `meta.json`
outside the file the operator edits; write both atomically; rebuild a missing `meta.json`
rather than 404; guard `sim_id` against path traversal; and serve the five HTTP routes, with a
forked edit answering `201`.

**Satisfied by:** The layout, the paths and the split are as specified: `config.json` holds exactly the eleven
scenario fields and no lifecycle at all, `meta.json` holds `state`, `created_at`, `started_at`,
`finished_at`, `updated_at`, `edits`, `last_edit_changes` and `forked_from`. Both go through
`_atomic_write` (temp file, then rename).

The edit path shares one implementation with generation. A fabricated quote carrying
hand-written `source_start`/`source_end` was demoted to the broadcaster with the reason "the
quoted text is not in the source document", its offsets cleared and the correction returned in
`changes`; a genuine quote submitted with deliberately wrong offsets kept its attribution and
had its span **recomputed** to `(38, 91)`. An edit carrying a `named_quote` with no document is
refused ("the source document is not available to check it against"), while a broadcaster-only
edit without one is accepted. Repointing at another graph is refused by name.

`SimulationState.LOCKED` is `{running, complete, failed}`. Editing a running simulation forked
into a new `sim_id`, recorded `forked_from` on disk, and left the original's `event` byte-identical;
editing the **fork** re-ran the same verification and demoted a fabricated quote there too.
`describe()` reports `editable: true` while draft and `false` while running and when complete.

`sim_id` is `sim-YYYYmmdd-HHMMSS-xxxxxx`, sorts chronologically, and two minted in the same
second differ. `SIM_ID_PATTERN` refuses `../../etc`, a non-hex tail and a malformed id before
any path is built. Over HTTP a well-formed-looking but invalid id answers
`404 {"error": "Not a simulation id: ..."}`; a percent-encoded traversal never reaches Flask at
all, because the gateway normalises the path first and the request lands on the SPA.

Live over the gateway: `GET /api/simulations` 200; `GET /api/simulations/<id>` returns
`config`, `meta`, `editable`, `prepared`, `summary`, `warnings`; `GET .../config` returns the
scenario; `PUT .../config` on a completed run answered **201** with `forked: true` and the new
`sim_id`. `TaskProgress.await_review()` takes `stage`, and the scenario job passes
`stage="scenario_review"` against the ontology flow's default.

**The limitation.** `load_meta()` rebuilds a missing `meta.json` from the config, as the
specification asks — but the rebuilt record takes `SimulationMeta`'s defaults, so `state`
returns to `draft`. Deleting the `meta.json` of a *running* simulation and reloading it yields
`draft`, and with it `editable: true`: the freeze is enforced through a file that, if lost,
un-freezes the run instead of failing closed. Both behaviours are specified individually; the
interaction is not, and the rebuild is the weaker of the two. Not fixed here — this document
does not change code — and it needs an outside deletion or disk loss to reach, since both files
are written atomically by the same call.

**Where:** `backend/app/services/simulation_store.py` — `SIM_ID_PATTERN` (69), `SimulationState.LOCKED` (82), `_atomic_write` (149), `load_meta` (276), `update_config` (316); `backend/app/api/simulation.py` — `derive_scenario_job` (90), `create_simulation` (130), `list_simulations` (172), `get_simulation` (179), `get_config` (184), `update_config` (189); `TaskProgress.await_review` in `services/tasks.py:266`

**Verified by:** `tests/test_simulation_store.py` — 46 tests. Probed live for this document: fork-on-locked, `forked_from` on disk, the original left untouched, fork re-verification, the graph-repoint refusal, the withheld-document refusal, the traversal guard and the `201` over HTTP. The rebuild limitation was found by probing, not by reading. Mutating `find_verbatim` to trust the label fails `test_AN_OPERATOR_CANNOT_ATTRIBUTE_AN_INVENTED_QUOTE_TO_A_REAL_PERSON`, `test_hand_written_offsets_are_recomputed_not_believed`, `test_a_correction_is_reported_never_silent` and `test_a_fork_still_re_verifies`.

### 5.4 — Config test units

> Specification: [line 519](REQ_SPEC.md#L519)

**Status:** ✅ Satisfied as specified

**Required:** `tests/test_simulation_config.py` — generated config validates against the schema; round count
respects `MAX_ROUNDS`; scheduled event rounds fall within the window. `tests/test_action_space.py`
— only OASIS-supported actions appear; per-platform sets are correct; an unknown action is
rejected at validation rather than at runtime.

**Satisfied by:** Both files exist and all six named assertions have a test of their own.

Schema: `test_a_valid_scenario_parses`, plus a parametrised `test_an_unusable_scenario_is_refused`.
`MAX_ROUNDS`: `test_the_model_cannot_exceed_max_rounds`, `test_a_caller_can_ask_for_fewer_rounds`,
`test_a_caller_cannot_ask_for_more_than_max_rounds` (the ceiling binds without inflating a modest
request) and `test_capping_rounds_also_drops_the_events_it_orphans`. Window:
`test_events_past_the_last_round_are_dropped`, `test_every_surviving_event_falls_inside_the_window`,
`test_set_rounds_drops_the_events_it_orphans`.

Action space: `test_the_spec_lists_are_what_we_ship` compares against the specification's own
lists; `test_our_mirror_of_the_enum_is_current` and `test_oasis_drops_nothing_from_our_action_space`
check the real installed enum; `test_an_unknown_action_is_rejected` and `test_a_near_miss_is_diagnosed`
prove refusal happens at validation, with a suggestion.

`test_simulation_config.py` holds **80** tests, two of them `integration`-marked, matching the
specification's account. The two live-model tests pass against `qwen2.5:14b` — the second is the
end-to-end safety property, that whatever the model claims, every surviving `named_quote` re-locates
at exactly the recorded offsets with a speaker the document names.

**Where:** `backend/tests/test_simulation_config.py` (80 tests), `backend/tests/test_action_space.py` (45 tests)

**Verified by:** Run for this document: `test_simulation_config.py`, `test_action_space.py` and `test_simulation_store.py` together — **169 passed, 2 deselected in 4.05 s**. The two deselected integration tests then run against live `qwen2.5:14b`: **2 passed in 19.3 s**. Three independent mutations (phantom enum member, `DO_NOTHING` removed, quote label trusted) each turn the suite red.

---

## Phase 6

**Simulation execution engine**

> Specification: [`REQ_SPEC.md` line 537](REQ_SPEC.md#L537)

### 6.1 — OASIS integration with local inference

> Specification: [line 539](REQ_SPEC.md#L539)

**Status:** ✅ Satisfied as specified

**Required:** Build `simulation_runner.py`. Instantiate the OASIS environment with CAMEL's `ModelFactory`
bound to local Ollama (`ModelPlatformType.OLLAMA`, the configured URL, temperature 0.7), and
**verify it before building anything on top** — a smoke test of three agents for two rounds
confirming requests arrive locally.

**Satisfied by:** `build_model()` is the only way to get a model backend, and the guarantee is structural: the
signature is `(config=None, *, temperature=None, model_name=None)` — **there is no
`model_platform` parameter**, so a caller cannot ask for a cloud vendor. The module names
`ModelPlatformType.OLLAMA` and no other platform at all. It re-checks the URL through the same
`classify_host` the configuration uses, so a `Config` mutated after validation is still caught,
and refuses an empty URL by name because camel would otherwise fall back to `OLLAMA_BASE_URL`
and shell out to an `ollama` binary the image does not contain.

Probed: `build_model()` returns a real `OllamaModel` bound to `http://ollama:11434/v1`;
`LLM_BASE_URL=""` raises `ModelBindingError` naming the fallback; `https://api.openai.com/v1`
raises "Every agent turn would leave this machine." `SIMULATION_TEMPERATURE` is 0.7, the spec's
figure.

**All four OASIS defects re-confirmed against the installed camel-oasis**, not taken from the
narrative. `get_db_path()` with `OASIS_DB_PATH` unset raises `PermissionError` on
`/usr/local/lib/python3.11/site-packages/oasis/data` — the package-internal fallback, exactly as
described — and returns the run's own path once the runner sets it; `agent_environment` calls it
three times. `OasisEnv` declares `semaphore: int = 128`; the runner passes its own concurrency.
`generate_twitter_agent_graph` never mentions `user_name`, building
`UserInfo(name=agent_info["username"][...], description=...)`. `OasisEnv.step` gathers with a
bare `await asyncio.gather(*tasks)` — no `return_exceptions`. And `SocialAgent.__init__` contains
the "is not supported" warning with **no `raise` anywhere**.

`harden_agent()` closes the gather hole per instance: with one agent raising `RuntimeError`, the
other still returned `"acted"`, the failure was counted and named (`alice: RuntimeError:
timeout`), and re-hardening is a no-op. `KeyboardInterrupt` propagated — a stop request is never
swallowed. `seed()` has no `try` around its step, so a seed that cannot be published is still a
broken run.

The username backfill is visible in real data: a completed 50-agent run has **51 users, none with
an empty `user_name`**. The broadcaster is separate and distinguishable — agent 50,
`rbnewsnow` / "Riverbend News Now", where every population agent's `name` equals its
`user_name` because that is all OASIS sets.

The second sealed-network fix is in place: `HF_HOME=/opt/huggingface` holds
`models--Twitter--twhin-bert-base` (1.1 GB) and `HF_HUB_OFFLINE=1` is set in the running
container.

**Where:** `backend/app/services/simulation_runner.py` — `build_model` (92), `harden_agent` (173), `attach_graph_memory` (202), `trim_agent_memory` (228), `SimulationRunner.setup` (432), `add_broadcaster` (397), `_name_the_agents` (344); `Dockerfile` (`HF_HOME`, `HF_HUB_OFFLINE`)

**Verified by:** `tests/test_ollama_model_binding.py` (17) and `tests/test_simulation_runner.py` (21). The privacy guard mutation-tested: making `classify_host` answer "loopback" for every host fails 4 tests, including all three public-endpoint cases. Making `harden_agent` a no-op fails 4 more. The smoke test moved to `test_simulation_smoke.py` as Step 6 records, rather than existing twice. The three `integration` files ran together against live Ollama, Neo4j and real spawned processes for this document: **4 passed, 41 deselected in 22 m 46 s**.

### 6.2 — Process isolation and IPC

> Specification: [line 576](REQ_SPEC.md#L576)

**Status:** ✅ Satisfied as specified

**Required:** One OS process per run, not a thread, independently killable. `simulation_ipc.py` for
control-plane messaging over a queue or Unix socket; `simulation_manager.py` tracking PIDs,
lifecycle state and cleanup of orphans on restart. The manager must **divide the
`LLM_CONCURRENCY` budget across the workers it spawns**, passing each its share through the
environment, and reserve a share for the API.

**Satisfied by:** The arithmetic is `(LLM_CONCURRENCY - API_LLM_RESERVE) // MAX_CONCURRENT_SIMULATIONS`, floored at
1. On this configuration (4, 1, 2) each worker gets **1**. The specification's own failure case
was re-run: with `LLM_CONCURRENCY=4` and three runs the worst case is `share × max + reserve` =
**3 requests in flight, never 12**. A degenerate budget of `(1-1)//3` returns 1 rather than 0, so
a worker is never given nothing. The share is passed through the environment
(`CROWDSIGHT_WORKER_CONCURRENCY`) at spawn, and the worker builds its own `Config`, so it never
guesses.

**`spawn`, never `fork`** — confirmed live: `multiprocessing.get_start_method()` on this platform
is `fork`, and the manager's context reports `spawn`.

**Unix socket, newline-delimited JSON**, driven here by a raw socket the way `socat` would:

    -> {"command": "status"}   <- {"ok": true, "result": {"round": 2, "rounds": 4}}
    -> {"command": "nonsense"} <- {"ok": false, "error": "Unknown command 'nonsense'"}
    -> not json at all         <- {"ok": false, "error": "Malformed request: ..."}
    -> {"command": "boom"}     <- {"ok": false, "error": "RuntimeError: handler exploded"}

Two commands travel down one connection, a handler that raises does not kill the server, and the
socket is mode `0600`. The documented asymmetry is real and was hit while probing: calling the
blocking client from inside the server's own loop hangs, which is why the probe runs the server
in its own thread.

The three defects hold as fixed, verified against real processes. A terminated-but-unreaped
child reports state `Z` and `WorkerRecord.alive()` returns **False**; once reaped, `/proc` is
gone. PID reuse is caught: the same PID with a wrong start time is not ours; with the right one
it is. `MAX_SOCKET_PATH` is 107 and a too-deep directory raises `IPCError` naming the length
rather than failing at `bind()`. A vanished worker is marked failed, not complete: the worker
records its own outcome on the way out (`_record_outcome(..., failed=True)` in its `except`
path), and `_reconcile_finished` only ever moves a run still recorded as `running`, so a killed
process cannot read as a finished one.

Orphans are reconciled at startup rather than left: `reap_orphans()` walks everything recorded as
`running` and pings each control socket — which outlives the parent — adopting a worker that
answers, escalating one that is alive but wedged, and marking a vanished one failed.

**Where:** `backend/app/services/simulation_manager.py` — `worker_share` (93), `process_status` (105), `WorkerRecord.alive` (147), `start` (324); `simulation_ipc.py` — `MAX_SOCKET_PATH` (70), `ControlServer` (131), `ControlClient`; `simulation_worker.py` (`spawn` target)

**Verified by:** `tests/test_process_isolation.py` — **33 tests**, against real spawned processes, including `test_THE_SPECS_OVERSUBSCRIPTION_CANNOT_HAPPEN`, `test_A_REUSED_PID_IS_NEVER_MISTAKEN_FOR_OURS`, `test_a_zombie_counts_as_dead`, `test_KILLING_A_SIMULATION_DOES_NOT_AFFECT_THE_API`, `test_A_LIVE_ORPHAN_IS_ADOPTED_NOT_KILLED` and `test_a_vanished_worker_is_marked_failed_for_resume`. `tests/test_ipc.py` adds 26.

### 6.3 — Round loop and persistence

> Specification: [line 607](REQ_SPEC.md#L607)

**Status:** ✅ Satisfied as specified

**Required:** Drive OASIS round by round. After each round persist agent actions, posts and comments to the
run's SQLite database and write a checkpoint enabling resume. Emit structured progress: current
round, total rounds, per-action counts, agents active.

**Satisfied by:** Verified against a **real completed run** — 11 rounds, 50 agents plus the broadcaster, 279 posts,
536 trace rows — rather than against fixtures.

OASIS's schema carries no round column anywhere; the only table that has one is
`crowdsight_round`, and there are no `WITHOUT ROWID` tables to break attribution. The ledger
records the high-water rowid of 13 tracked tables at each round's end. Replaying those marks
attributes **every one of the 279 posts to exactly one round** (1, 35, 28, 30, 33, 26, 22, 24,
28, 26, 26), with the seed as round 0 — a single post, one invocation, the broadcaster.

Per-action counts come from OASIS's own `trace` table over each round's range. Round 4 of that run
reads `{create_post: 27, do_nothing: 1, follow: 2, quote_post: 5, refresh: 6, repost: 1}`. Each
record also carries `invoked`, `acted`, `failed` and `skipped` — round 4 invoked 37 and skipped
13, which is Step 5's participation roll doing its work.

Rollback was exercised on a copy of that database: rolling back to round 5 removed exactly
`{post: 126, trace: 252, follow: 7, like: 6}`, leaving 153 posts — round 5's recorded mark to the
row. The 51 users were untouched, later checkpoints were discarded, **no post's `num_likes`
disagreed with the rows that remained**, and re-running the rollback removed nothing.

Resume keys off the database, not the checkpoint: `resuming = database_path.exists()`, and a
database with no checkpoint is rolled back to empty so the seed cannot be published twice. The
clock is advanced past completed rounds before the loop resumes, and a checkpoint is written
only after a round completes. Agent memory is bounded to `SIMULATION_MEMORY_ROUNDS` (3), sliced
at user-message boundaries so a tool result never loses its assistant tool-call. The run database
is in **WAL**.

**Where:** `backend/app/services/simulation_persistence.py` — `ROLLBACK_TABLES` (66), `marks` (161), `action_counts` (176), `record_round` (191), `rollback_to` (268), `_recount` (301), `rows_by_round` (332); round loop in `simulation_worker.py` (86-285); `trim_agent_memory` and `advance_clock` in `simulation_runner.py`

**Verified by:** `tests/test_simulation_persistence.py` (41) and `tests/test_simulation_resume.py` (15 + 1 integration). Teeth confirmed by mutation: stopping `rollback_to` from rebuilding denormalised counters fails `test_ROLLBACK_REBUILDS_DENORMALISED_COUNTERS`. The three `integration` files ran together against live Ollama, Neo4j and real spawned processes for this document: **4 passed, 41 deselected in 22 m 46 s**.

### 6.4 — Graph memory feedback (optional, flagged)

> Specification: [line 624](REQ_SPEC.md#L624)

**Status:** ✅ Satisfied as specified

**Required:** Optionally feed significant simulation outcomes back into Neo4j as new nodes and edges so agent
memory evolves across rounds. Build `graph_memory_updater.py`. Keep it behind a config flag,
because it roughly doubles graph writes and materially increases run time.

**Satisfied by:** The flag is off: `GRAPH_MEMORY_FEEDBACK=False`, `GRAPH_MEMORY_MIN_ENGAGEMENT=1`,
`GRAPH_MEMORY_TOP_N=5` — the spec's figures. Nothing is built when it is off, so a normal run
pays nothing.

The loop is closed in both directions: `_write_graph_memory` after each round, `_refresh_graph_memory`
before the next. The recollection is fetched **once per round into a single shared dict** and
`attach_graph_memory` wraps `agent.env.to_text_prompt` — so it arrives as part of what the agent
observes, alongside its feed, and there is no Neo4j round trip per agent.

The simulated/documented line was re-derived from the Cypher rather than taken on trust. Of the
nine create-or-merge clauses in the module, **none names `:Entity`**. The labels it creates are
exactly `SimRun`, `SimAgent`, `SimPost`. Three clauses touch `:Entity` and all three are
`MATCH`/`OPTIONAL MATCH`; the string `SET e.` does not appear anywhere in the module. The single
edge that crosses the line is `(p:SimPost)-[:ABOUT]->(e:Entity)` — named for exactly what it
means, and pointing at the entity, never writing to it.

Significance is engagement counted from the run's own database, not judged by a model, so the
same round gives the same answer twice and nothing is added to the critical path of a saturated
GPU. Entity linking ignores names of three characters or fewer: on "AB and Kim discussed the
Riverside Development" it returns only `riverside development`. Both the read and write paths
swallow their exceptions and log, so an optional enrichment cannot take down a run that is hours
old.

**Where:** `backend/app/services/graph_memory_updater.py` — `entity_names` (125), `collect` (148), `write_round` (220), `context_for` (289), `delete_run` (324), `_mentioned_entities` (334); `attach_graph_memory` in `simulation_runner.py:202`; `_refresh_graph_memory` / `_write_graph_memory` in `simulation_worker.py`

**Verified by:** `tests/test_graph_memory.py` — 28 unit tests plus an `integration` test asserting the line holds against a live Neo4j. The three `integration` files ran together against live Ollama, Neo4j and real spawned processes for this document: **4 passed, 41 deselected in 22 m 46 s**.

### 6.5 — Simulation control API

> Specification: [line 641](REQ_SPEC.md#L641)

**Status:** ✅ Satisfied as specified

**Required:** `POST /api/simulation/create`, `POST /api/simulation/prepare` (async, returns a task id),
`GET /api/simulation/prepare/status`, `GET /api/simulation/<id>/config`,
`GET /api/simulation/<id>/profiles`, `POST /api/simulation/start`, `POST /api/simulation/stop`,
`GET /api/simulation/list`.

**Satisfied by:** All eight are registered, plus `GET /api/simulation/<id>/status` and `GET /api/simulation/budget`
as the specification's account says.

Exercised live through the gateway. `GET /list` and `GET /budget` answer 200 — the budget reports
`{llm_concurrency: 4, api_reserve: 1, max_concurrent_simulations: 2, per_worker: 1, capacity: 2,
running: 0, lingering: 0}`. `GET /prepare/status` with no `task_id` answers **400**, not the 404
it would give if `/<sim_id>` had swallowed the static route. `POST /create` against an unknown
graph answers `{"error": "No graph ..."}`.

The state guards answer **409**, up front, rather than 404 or a worker dying minutes in. On a
freshly created simulation: `/config` → 409 "has not been prepared yet"; `/profiles` → 409 "has
no population yet"; `POST /start` → 409 "has no scenario yet; prepare it first". A missing
simulation is a genuine 404, and `POST /stop` on one is 404 rather than a cheerful "not running".

**The guards live in the manager, not only in the route.** Calling `manager.start()` directly on a
simulation with no `config.json` raises `CapacityError` naming what is missing, and again for a
scenario with no population — so the worker, the scheduler and any future caller hit the same
rule. `manager.stop()` on an unknown id raises `SimulationNotFound`.

`start` resumes: `resuming = meta.state == FAILED`, the manager admits only `draft` and `failed`,
and the response carries `resumed`. `prepare` returns 200 with "Already prepared" unless
`force=true`, which deletes the population outright so Phase 4's resumability cannot helpfully
resume the very population being replaced.

The deadlock fix is present and load-bearing: `graph_context` is async and awaited directly by the
jobs, with `_graph_context` documented as "for Flask handlers only. Never call this from a job."
The `Runtime` owns a `SimulationManager` and calls `reap_orphans()` at startup.

**Where:** `backend/app/api/simulation.py` — `graph_context` (58), `derive_scenario_job` (90), `prepare` (413), `_discard_profiles` (473), `prepare_status` (481), `list_all` (499), `start` (677), `stop` (708), `budget` (731); `SimulationManager.start` (324); `Runtime` (`services/runtime.py:69-72`)

**Verified by:** `tests/test_simulation_control_api.py` — **71 tests**. The deadlock guard mutation-tested: reintroducing the defect (a job reaching the blocking facade) fails **8 tests** across `test_simulation_control_api.py` and `test_simulation_api.py`, because both stubs raise when `run()` is called from inside a running loop.

### 6.6 — Engine test units

> Specification: [line 660](REQ_SPEC.md#L660)

**Status:** ⚠️ Satisfied, with one coverage gap recorded below

**Required:** Six named files: `test_ollama_model_binding.py` (OLLAMA platform, configured local URL, no path to
a cloud model), `test_simulation_lifecycle.py` (create → prepare → start → stop, invalid
transitions rejected clearly), `test_simulation_persistence.py` (**actions, posts and comments**
persist with correct round attribution; checkpoints written), `test_simulation_resume.py` (killed
mid-flight, resumes without duplicating rounds), `test_process_isolation.py` (killing a run does
not affect the API; orphans reaped on restart), `test_simulation_smoke.py` (3 agents, 2 rounds,
real Ollama, `integration`-marked and excluded from the fast suite).

**Satisfied by:** All six exist and pass. Run together with `test_graph_memory.py` and
`test_simulation_control_api.py`: **253 passed, 4 deselected in 21.4 s**. Counts: binding 17,
lifecycle 29, persistence 41, resume 15 (+1 integration), isolation 33, smoke 0 (+2 integration),
graph memory 28 (+1), control API 71.

Every clause has a test named for it. `test_NO_CODE_PATH_CAN_ASK_FOR_ANOTHER_PLATFORM` and
`test_the_platform_is_ollama_not_openai`; `test_the_full_sequence_create_prepare_start_stop` and
`test_STARTING_AN_UNPREPARED_SIMULATION_IS_REJECTED` with `test_the_error_says_what_is_missing`;
`test_KILLING_A_SIMULATION_DOES_NOT_AFFECT_THE_API` and `test_A_LIVE_ORPHAN_IS_ADOPTED_NOT_KILLED`;
`test_every_tracked_table_has_a_usable_rowid`, which is the guard against a future `WITHOUT ROWID`
breaking attribution silently. `test_simulation_smoke.py` collects **nothing** without the
`integration` marker, so it is genuinely excluded from the fast suite.

The lifecycle state machine is tested at the manager rather than over HTTP, as the specification's
account says, and that placement is what caught `manager.start()` spawning a worker for a
simulation with no configuration.

**The gap.** The specification asks for **comments** with correct round attribution, and Step 6
records closing it by generalising the ledger to `rows_by_round(table, id_column)` with
`comments_by_round` as a wrapper. The method exists and is used by `run_reader.py` — but
`test_simulation_persistence.py` **never mentions comments**, and nothing else tests
`comments_by_round` or `rows_by_round` directly. Nor is there live evidence: of the 38 simulation
databases on disk, 37 carry a `comment` table and **all 37 hold zero rows** (the 38th has no such
table), because every run so far has been Twitter, whose action set has no `CREATE_COMMENT`. So the one clause of this step that named a third row type
is carried by an untested method.

Probed rather than assumed: synthesising comments across three rounds and re-reading them,
`comments_by_round()` returned `{0: [1], 1: [2, 3], 2: [4, 5, 6]}` with the right contents. **The
capability works; the coverage does not exist.** Recorded here rather than fixed — this document
does not change code — and the obvious closure is a Reddit smoke run, which would exercise
comments and the other half of Phase 5's action space at the same time.

**Where:** `backend/tests/test_ollama_model_binding.py`, `test_simulation_lifecycle.py`, `test_simulation_persistence.py`, `test_simulation_resume.py`, `test_process_isolation.py`, `test_simulation_smoke.py`

**Verified by:** Run for this document: 253 passed, 4 deselected in 21.4 s. Four mutations each turn the suite red — `classify_host` made permissive (4 failures), `harden_agent` disabled (4), the rollback recount removed (1), the job deadlock reintroduced (8). The comment gap was found by grepping the suite and then confirmed against every database on disk. The three `integration` files ran together against live Ollama, Neo4j and real spawned processes for this document: **4 passed, 41 deselected in 22 m 46 s**.

---

## Phase 7

**Monitoring, data access, and agent interviews**

> Specification: [`REQ_SPEC.md` line 680](REQ_SPEC.md#L680)

### 7.1 — Run status and timeline endpoints

> Specification: [line 682](REQ_SPEC.md#L682)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 7.2 — Content access endpoints

> Specification: [line 697](REQ_SPEC.md#L697)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 7.3 — Agent interview

> Specification: [line 712](REQ_SPEC.md#L712)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 7.4 — Environment health

> Specification: [line 732](REQ_SPEC.md#L732)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 7.5 — Monitoring test units

> Specification: [line 743](REQ_SPEC.md#L743)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 8

**Report generation**

> Specification: [`REQ_SPEC.md` line 760](REQ_SPEC.md#L760)

### 8.1 — Report agent

> Specification: [line 762](REQ_SPEC.md#L762)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 8.2 — Grounding and citation

> Specification: [line 781](REQ_SPEC.md#L781)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 8.3 — Report API and persistence

> Specification: [line 796](REQ_SPEC.md#L796)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 8.4 — Report test units

> Specification: [line 815](REQ_SPEC.md#L815)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 9

**Frontend**

> Specification: [`REQ_SPEC.md` line 835](REQ_SPEC.md#L835)

### 9.1 — Application shell

> Specification: [line 837](REQ_SPEC.md#L837)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.2 — Stage 1 — graph build

> Specification: [line 882](REQ_SPEC.md#L882)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.3 — Stage 2 — environment setup

> Specification: [line 899](REQ_SPEC.md#L899)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.4 — Stage 3 — simulation

> Specification: [line 916](REQ_SPEC.md#L916)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.5 — Stage 4 — report

> Specification: [line 941](REQ_SPEC.md#L941)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.6 — Stage 5 — interaction

> Specification: [line 954](REQ_SPEC.md#L954)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 9.7 — Frontend test units

> Specification: [line 997](REQ_SPEC.md#L997)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 10

**Integration testing, egress verification, and operations**

> Specification: [`REQ_SPEC.md` line 1018](REQ_SPEC.md#L1018)

### 10.1 — Full pipeline integration test

> Specification: [line 1020](REQ_SPEC.md#L1020)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 10.2 — Egress verification suite

> Specification: [line 1037](REQ_SPEC.md#L1037)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 10.3 — Performance baseline

> Specification: [line 1056](REQ_SPEC.md#L1056)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 10.4 — Operational tooling

> Specification: [line 1071](REQ_SPEC.md#L1071)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 10.5 — Documentation

> Specification: [line 1084](REQ_SPEC.md#L1084)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---
