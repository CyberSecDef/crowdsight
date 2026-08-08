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
| [4](#phase-4) | Agent profile generation | 5 | _pending_ |
| [5](#phase-5) | Simulation configuration generation | 4 | _pending_ |
| [6](#phase-6) | Simulation execution engine | 6 | _pending_ |
| [7](#phase-7) | Monitoring, data access, and agent interviews | 5 | _pending_ |
| [8](#phase-8) | Report generation | 4 | _pending_ |
| [9](#phase-9) | Frontend | 7 | _pending_ |
| [10](#phase-10) | Integration testing, egress verification, and operations | 5 | _pending_ |
| | **Total** | **53** | **17 / 53** |

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

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 4.2 — Population expansion

> Specification: [line 396](REQ_SPEC.md#L396)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 4.3 — OASIS profile schema conformance

> Specification: [line 413](REQ_SPEC.md#L413)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 4.4 — Parallel generation with progress

> Specification: [line 434](REQ_SPEC.md#L434)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 4.5 — Profile test units

> Specification: [line 449](REQ_SPEC.md#L449)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 5

**Simulation configuration generation**

> Specification: [`REQ_SPEC.md` line 465](REQ_SPEC.md#L465)

### 5.1 — Scenario derivation

> Specification: [line 467](REQ_SPEC.md#L467)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 5.2 — Action space configuration

> Specification: [line 485](REQ_SPEC.md#L485)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 5.3 — Config persistence and override

> Specification: [line 502](REQ_SPEC.md#L502)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 5.4 — Config test units

> Specification: [line 519](REQ_SPEC.md#L519)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

---

## Phase 6

**Simulation execution engine**

> Specification: [`REQ_SPEC.md` line 537](REQ_SPEC.md#L537)

### 6.1 — OASIS integration with local inference

> Specification: [line 539](REQ_SPEC.md#L539)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 6.2 — Process isolation and IPC

> Specification: [line 576](REQ_SPEC.md#L576)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 6.3 — Round loop and persistence

> Specification: [line 607](REQ_SPEC.md#L607)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 6.4 — Graph memory feedback (optional, flagged)

> Specification: [line 624](REQ_SPEC.md#L624)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 6.5 — Simulation control API

> Specification: [line 641](REQ_SPEC.md#L641)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

### 6.6 — Engine test units

> Specification: [line 660](REQ_SPEC.md#L660)

**Status:** _pending_

**Required:** _pending_

**Satisfied by:** _pending_

**Where:** _pending_

**Verified by:** _pending_

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
