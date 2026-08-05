# CrowdSight — Local Swarm Simulation Engine

> Build-from-scratch requirements specification.
> Functionally equivalent to MiroFish, but built clean and running entirely on local infrastructure.

---

## Overview

CrowdSight is a swarm-intelligence simulation and prediction engine. You give it a source document — a news article, a policy draft, a product announcement, an incident report — and it builds a knowledge graph of the entities and relationships inside it. From that graph it generates a population of hundreds of distinct AI agents, each with its own persona, memory, and behavioural disposition. Those agents are then placed in a simulated social platform (Twitter-style or Reddit-style) and allowed to run for a configurable number of rounds, during which they post, comment, like, repost, follow, search, and react to one another. The result is not a statistical forecast but a *simulated collective reaction*: what a population of plausible individuals would say and do in response to the event, how sentiment clusters and shifts, which sub-narratives take hold, and which agents become influential.

CrowdSight is designed for a fully sealed deployment. All inference runs against a local Ollama instance, all graph memory lives in a local Neo4j instance, and all simulation state lives in local SQLite files. There are no cloud services, no external memory providers, and no telemetry. Network egress is denied at the container-network level rather than merely avoided by configuration, so the "nothing leaves the network" property is structurally enforced and independently verifiable, not a matter of trusting a config file. The only step requiring internet access is one-time provisioning (pulling model weights and Python/npm packages), after which the stack runs sealed.

---

## Hardware requirements

Sized against the reference target (Ryzen 9 8940HX, 32 threads, 61 GB RAM, RTX 5070 Ti Laptop 12 GB VRAM, 1.5 TB free).

- **CPU** — 8+ cores minimum; 16+ threads strongly recommended. Agent turns parallelise well; the profile-generation phase is embarrassingly parallel.
- **RAM** — 16 GB absolute minimum, 32 GB recommended, 64 GB comfortable. Budget: Neo4j heap 4–8 GB, Ollama model resident set 9–10 GB (14b class), backend + simulation workers 4–8 GB.
- **GPU / VRAM** — 10 GB minimum for a 14b-class model fully GPU-resident; 24 GB if you want a 32b-class model at full speed. **12 GB (the reference target) runs 14b comfortably and 32b only with partial CPU offload, which is significantly slower.** CPU-only inference works but is impractical for multi-hundred-agent runs.
- **Disk** — 50 GB minimum, 100 GB+ recommended. Budget: model weights ~10–20 GB, Neo4j store 5–20 GB depending on graph count, SQLite simulation DBs 100 MB–2 GB per run, retained run artefacts.
- **Network** — LAN only at runtime. One-time internet access required for provisioning.

> **Sizing note for the build session:** a 300-agent × 20-round simulation issues on the order of 6,000+ LLM completions plus embedding calls. On a single 12 GB GPU running a 14b model this is hours, not minutes. Design every long-running operation as an async job with progress polling and resumability — never a blocking HTTP request.

---

## Software requirements

- **Python 3.12** — pin exactly. Not 3.13/3.14: the CAMEL/OASIS dependency tree lags. The reference host runs system Python 3.14, so the backend **must** be containerised or run in a pinned 3.12 virtualenv.
- **Node.js 20 LTS+** and npm — frontend build only.
- **Docker Engine 24+** and Docker Compose v2.
- **Ollama 0.30+** — serves both chat completions and embeddings on `:11434`.
- **Neo4j Community Edition 5.15+** — knowledge graph and agent memory store.
- **Python packages** — `flask>=3.0`, `flask-cors>=6.0`, `openai>=1.0` (SDK only, pointed at Ollama), `camel-ai==0.2.78`, `camel-oasis==0.2.5`, `neo4j>=5.15`, `pydantic>=2.0`, `pydantic-settings>=2.2`, `PyMuPDF>=1.24`, `python-dotenv>=1.0`, `httpx>=0.27`, `charset-normalizer`, `chardet`.
- **Test packages** — `pytest`, `pytest-asyncio`, `pytest-cov`, `responses` or `respx` for HTTP mocking, `testcontainers` (optional, for ephemeral Neo4j in integration tests).
- **Ollama models** — `qwen2.5:14b` (reasoning/generation) and `nomic-embed-text` (768-dim embeddings). Optionally `qwen2.5:32b` for higher-VRAM hosts.
- **Frontend** — Vue 3 + Vite, a graph visualisation library (Cytoscape.js or vis-network), and a charting library.

### Designated endpoints — the complete allowlist

The application must communicate with **these and only these**:

| Purpose | Endpoint | Protocol |
|---|---|---|
| LLM chat completions | `http://ollama:11434/v1` | OpenAI-compatible HTTP |
| Text embeddings | `http://ollama:11434/api/embeddings` | Ollama native HTTP |
| Knowledge graph | `bolt://neo4j:7687` | Bolt |
| Simulation state | local SQLite files under `./data/simulations/` | filesystem |
| Backend API | `http://localhost:5000` | HTTP, bound to loopback/LAN |
| Frontend | `http://localhost:5173` (dev) / `:8080` (prod) | HTTP |

The service names above are the preferred form, and loopback (`localhost`, `127.0.0.1`, `::1`) is equally acceptable when running outside Compose. A private LAN address — RFC 1918, link-local, or unique-local — is **permitted where genuinely necessary**, for example when Ollama runs on a separate GPU box, but it is a second-best arrangement and the configuration layer must say so out loud: traffic to another host on the LAN still leaves this machine, and the container-level egress seal cannot cover it. Public addresses and public hostnames are refused outright.

Any other outbound destination is a defect. There is no API key to a third party anywhere in this system. `LLM_API_KEY` exists because the OpenAI SDK requires a non-empty string; it defaults to the literal `ollama`, which Ollama ignores. It is settable so that a local OpenAI-compatible gateway in front of Ollama (LiteLLM, vLLM) can be given a token — a value that, like everything else here, never leaves the perimeter.

---

## Plan

1. **Phase 1** — Foundation, sealed networking, and the configuration contract
2. **Phase 2** — Local service layer: Ollama and Neo4j clients
3. **Phase 3** — Document ingestion and knowledge graph construction
4. **Phase 4** — Agent profile generation
5. **Phase 5** — Simulation configuration generation
6. **Phase 6** — Simulation execution engine
7. **Phase 7** — Monitoring, data access, and agent interviews
8. **Phase 8** — Report generation
9. **Phase 9** — Frontend
10. **Phase 10** — Integration testing, egress verification, and operations

---

## Details

### Phase 1: Foundation, sealed networking, and the configuration contract

**Step 1: Repository scaffold** ✅
Create the project skeleton: `backend/app/{api,services,storage,utils,models}`, `backend/tests`, `frontend/src`, `data/{uploads,graphs,simulations,reports}`, plus `docker-compose.yml`, `Dockerfile`, `backend/requirements.txt`, `backend/requirements-dev.txt`, `.env.example`, `README.md`. Initialise git. Add a `.gitignore` covering `.env`, `data/`, `__pycache__`, `node_modules`, and `*.db`.

**Step 2: The configuration module** ✅
Build `backend/app/config.py` as the single source of truth, using `pydantic-settings`. `Config` subclasses `BaseSettings`: every setting is a typed field bound to the environment variable of the same name, given a local-only default, and constrained declaratively (`ge`, `le`) rather than by hand. Constructing `Config` *is* validating it — pydantic runs field validators and `@model_validator(mode="after")` checks in one pass and reports every failure together. Expose `get_config()` for the process-wide singleton and `reload_config()` to re-read the environment; both raise a typed `ConfigError` rather than leaking pydantic's `ValidationError`, so callers depend on this module's contract instead of pydantic's. (The name `validate` is reserved — `BaseModel.validate` is a deprecated pydantic v1 method — hence `get_config`/`reload_config`.)

Settings: `LLM_BASE_URL` (default `http://ollama:11434/v1`), `LLM_MODEL_NAME` (default `qwen2.5:14b`), `LLM_API_KEY` (`SecretStr`, default `ollama`), `LLM_CONCURRENCY` (default 4), `EMBEDDING_BASE_URL` (default `http://ollama:11434`), `EMBEDDING_MODEL` (default `nomic-embed-text`), `NEO4J_URI` (default `bolt://neo4j:7687`), `NEO4J_USER` (default `neo4j`), `NEO4J_PASSWORD` (`SecretStr`, no default — an operator must choose one), `MAX_ROUNDS` (default 10), `MAX_AGENTS` (default 100), `REPORT_TEMPERATURE` (default 0.5), `CHUNK_SIZE` (default 500), `CHUNK_OVERLAP` (default 50), `MAX_CONTENT_LENGTH` (50 MB), `ALLOWED_EXTENSIONS` (`pdf, md, txt, markdown`), `ALLOWED_HOSTS` (empty).

Critically, a model validator must **reject any configuration pointing off-host**. Parse `LLM_BASE_URL`, `EMBEDDING_BASE_URL` and `NEO4J_URI`, and classify each hostname:

| Class | Examples | Behaviour |
|---|---|---|
| Loopback | `localhost`, `127.0.0.1`, `::1` | Accept silently — **preferred** |
| Compose service name | `ollama`, `neo4j` | Accept silently — the default deployment |
| Private / link-local | `10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `fc00::/7`, `fe80::/10` | Accept **with a `PerimeterWarning`** — allowed where genuinely needed, never the default |
| Operator opt-in | any name in `ALLOWED_HOSTS` | Accept with a warning noting that names are never resolved |
| Public | `api.openai.com`, `1.1.1.1`, `neo4j+s://…databases.neo4j.io` | **Refuse to start** |

Hostnames must never be resolved during this check: DNS can point anywhere, and a guarantee that depends on what a resolver returns today is not a guarantee. A name is trusted only if it is a known service name or an explicit opt-in; IP literals are judged by the address itself. Warnings are collected on `Config.perimeter_notes` as well as raised through `warnings.warn`, so startup logging and the health endpoint can both surface them. Validate the URI scheme too (`http`/`https` for the LLM and embedding endpoints, `bolt`/`neo4j` and their `+s`/`+ssc` variants for Neo4j) — a cloud Neo4j Aura URI must fail on the host check, not slip through because the scheme looked plausible.

This is a deliberate inversion of the upstream project, which rejected self-hosted memory URLs; here we reject *non*-local ones.

**Step 3: Sealed container networking**
Write `docker-compose.yml` defining four services — `ollama`, `neo4j`, `backend`, `frontend` — on a single user-defined bridge network declared `internal: true`, which removes the default gateway and makes egress impossible. Ollama gets GPU passthrough via `deploy.resources.reservations.devices` (or `--gpus all`). Neo4j gets a named volume plus `NEO4J_AUTH` and heap settings. Publish only the frontend and backend ports to the host; Neo4j and Ollama stay unpublished and reachable only by service name.

Because an internal network blocks model pulls, provide a separate `docker-compose.provision.yml` overlay that temporarily attaches Ollama to a normal bridge network for one-time `ollama pull` operations. Document that this overlay must never be used at runtime.

**Step 4: Egress guard test unit**
**Tests:** `tests/test_config_validation.py` — asserts loopback and service-name URLs are accepted silently; that private/link-local addresses are accepted but emit a `PerimeterWarning` and populate `perimeter_notes`; that public hostnames, raw public IPs, off-box `https://` URLs, cloud Neo4j URIs and wrong schemes all raise `ConfigError`; that a missing required variable (`NEO4J_PASSWORD`) fails loudly rather than defaulting silently; and that field constraints (`MAX_ROUNDS >= 1`, `0.0 <= REPORT_TEMPERATURE <= 2.0`, `CHUNK_OVERLAP < CHUNK_SIZE`) reject bad values. Test `classify_host` directly as well — it is the smallest unit the whole guarantee rests on.
`tests/test_network_isolation.py` — an integration test that execs inside the backend container and asserts a connection attempt to a known-external host fails (DNS resolution failure or no route). This is the test that proves the "never leaves my network" property; it must be part of CI and not skippable.

---

### Phase 2: Local service layer — Ollama and Neo4j clients

**Step 1: LLM client**
Build `backend/app/utils/llm_client.py` wrapping the OpenAI SDK pointed at `LLM_BASE_URL`. Expose `complete(messages, temperature, max_tokens)` and `complete_json(messages, schema)`. The JSON variant is load-bearing: nearly every downstream stage expects structured output from a local model, and 14b-class models are markedly less reliable at strict JSON than frontier models. Implement a repair loop — attempt parse, on failure re-prompt with the parser error appended, retry up to N times, then raise a typed `LLMJSONError`. Strip markdown code fences before parsing. Log every failure with the raw response for debugging.

**Step 2: Retry, timeout, and concurrency control**
Build `backend/app/utils/retry.py` with exponential backoff and jitter for transient failures. Add a global semaphore bounding concurrent Ollama requests (default 4, configurable) — a single Ollama instance serialises internally and unbounded concurrency degrades throughput and can OOM the GPU. Set generous timeouts: local 14b generation can take 30–90 s for long completions.

**Step 3: Embedding service**
Build `backend/app/storage/embedding_service.py` calling Ollama's embeddings endpoint with `nomic-embed-text`, returning 768-dim vectors. Implement batching and an on-disk cache keyed by content hash — re-embedding unchanged chunks across runs is pure waste.

**Step 4: Neo4j storage layer**
Build `backend/app/storage/neo4j_storage.py` (connection pooling, session management, parameterised Cypher only — never string interpolation) and `neo4j_schema.py` (constraints and indexes: uniqueness on entity UUID, index on entity type and name, vector index on the embedding property if the Neo4j version supports it, otherwise cosine similarity computed in-process).

**Step 5: Service client test units**
**Tests:** `tests/test_llm_client.py` — mock the HTTP layer; assert correct base URL is used, that fenced JSON is stripped, that the repair loop retries on malformed JSON and succeeds on a subsequent valid response, and that it raises `LLMJSONError` after exhausting retries.
`tests/test_embedding_service.py` — assert vector dimensionality is 768, batching splits correctly, and the cache returns without a second HTTP call.
`tests/test_neo4j_storage.py` — against an ephemeral Neo4j (testcontainers or a dedicated test database); assert schema creation is idempotent, CRUD round-trips, and that all queries are parameterised.
`tests/test_retry.py` — assert backoff timing, retry ceiling, and that the concurrency semaphore is respected.

---

### Phase 3: Document ingestion and knowledge graph construction

**Step 1: File parsing**
Build `backend/app/utils/file_parser.py` handling PDF (PyMuPDF), Markdown, and plain text. Detect encoding with `charset-normalizer`/`chardet`. Enforce the 50 MB cap and extension allowlist. Return normalised plain text plus metadata (filename, page count, character count).

**Step 2: Chunking**
Split extracted text into overlapping chunks (default 500 characters, 50 overlap). Prefer semantic boundaries — split on paragraph then sentence before falling back to hard character cuts — so entity mentions are not severed mid-phrase.

**Step 3: Ontology generation**
Build `backend/app/services/ontology_generator.py`. Given a document sample, ask the LLM to propose a domain-appropriate ontology: entity types (e.g. Person, Organisation, Policy, Location, Product, Event) and relationship types, each with a description and expected attributes. This adapts the graph to the document's domain instead of forcing a fixed schema. Return validated JSON; allow the operator to review and override before extraction proceeds.

**Step 4: Entity and relationship extraction**
Build `backend/app/storage/ner_extractor.py`. For each chunk, prompt the LLM to extract entities and relationships conforming to the generated ontology. Deduplicate across chunks by normalised name plus embedding similarity above a threshold — the same person mentioned in eight chunks must become one node, not eight. Merge attributes on collision.

**Step 5: Graph construction**
Build `backend/app/services/graph_builder.py` persisting entities as nodes and relationships as edges in Neo4j, each carrying `graph_id`, source chunk references, and an embedding. Store provenance so any node can be traced back to the text that produced it.

**Step 6: Graph query and search**
Build `backend/app/storage/search_service.py` and `graph_storage.py` supporting: fetch entities by graph, by type, by UUID; semantic search by embedding similarity; and neighbourhood traversal to depth N.

**Step 7: Graph API**
Build `backend/app/api/graph.py`: `POST /api/graph/upload` (accept file, return `graph_id` and async `task_id`), `GET /api/graph/status/<task_id>`, `GET /api/graph/<graph_id>/entities` (filter by type, paginate), `GET /api/graph/<graph_id>/entities/<uuid>`, `GET /api/graph/<graph_id>/subgraph` (for visualisation), `DELETE /api/graph/<graph_id>`.

**Step 8: Ingestion test units**
**Tests:** `tests/test_file_parser.py` — PDF/MD/TXT fixtures parse correctly; oversized files and disallowed extensions are rejected; mis-encoded input is handled.
`tests/test_chunking.py` — chunk size and overlap are honoured; semantic boundaries preferred; a document shorter than one chunk yields exactly one chunk.
`tests/test_ontology_generator.py` — with a mocked LLM, valid ontology JSON parses; malformed output triggers the repair loop; the resulting schema validates.
`tests/test_ner_extractor.py` — entities extract from a fixture chunk; duplicate entities across chunks merge into one node; attribute merge conflicts resolve deterministically.
`tests/test_graph_builder.py` — nodes and edges persist and are retrievable; provenance links back to source chunks; rebuilding the same document is idempotent.
`tests/test_graph_api.py` — every route returns the documented shape; upload returns a task ID immediately rather than blocking; unknown `graph_id` returns 404 not 500.

---

### Phase 4: Agent profile generation

**Step 1: Entity-to-persona mapping**
Build `backend/app/services/profile_generator.py`. Select graph entities eligible to become agents (typically Person, plus optionally Organisation as institutional voices). For each, prompt the LLM to synthesise a persona: name, age, occupation, background bio, personality traits (a Big-Five-style vector or descriptive traits), interests, political/topical leanings, activity level, and a writing-style hint.

**Step 2: Population expansion**
A source document rarely names enough people to form a crowd. Implement synthetic expansion: from the graph's demographic and topical context, generate additional agents that are plausible members of the affected population but not named in the source. Make the named-to-synthetic ratio configurable and always mark provenance on each profile — a reader of the output must be able to distinguish a real named actor from a synthesised crowd member. This distinction is the difference between a defensible simulation and an accidental fabrication about a real person.

**Step 3: OASIS profile schema conformance**
Emit profiles in the JSON schema OASIS expects for agent initialisation, written to `data/simulations/<sim_id>/profiles/{twitter,reddit}.json`. Include the platform-specific fields each environment requires (follower counts, subreddit affiliations, initial post history).

**Step 4: Parallel generation with progress**
Profile generation is the second-most expensive stage. Run it as a bounded parallel job (respecting the global Ollama semaphore) with per-profile progress reporting, partial-result persistence, and resumability after interruption.

**Step 5: Profile test units**
**Tests:** `tests/test_profile_generator.py` — a graph entity yields a schema-valid profile; required fields are present and typed correctly; personality values fall in range.
`tests/test_profile_normalization.py` — LLM field-name and type drift (e.g. `age` returned as `"thirty-four"`) is normalised or rejected cleanly.
`tests/test_synthetic_expansion.py` — requesting N agents from M named entities yields N profiles; every profile carries correct `provenance: named|synthetic`; the named/synthetic ratio is respected.
`tests/test_oasis_profile_contract.py` — generated JSON validates against the OASIS profile schema. **This is the highest-value test in the phase** — a schema mismatch surfaces as an opaque failure deep inside the simulation engine, hours into a run.

---

### Phase 5: Simulation configuration generation

**Step 1: Scenario derivation**
Build `backend/app/services/simulation_config_generator.py`. From the graph and the source document, have the LLM derive: the triggering event description, a simulated time window and round cadence, the initial seed posts that introduce the event, and any scheduled mid-simulation events (a follow-up announcement, a rebuttal, a leak).

**Step 2: Action space configuration**
Define the permitted action set per platform, matching OASIS's supported actions. Twitter: `CREATE_POST, LIKE_POST, REPOST, FOLLOW, QUOTE_POST, DO_NOTHING`. Reddit: `LIKE_POST, DISLIKE_POST, CREATE_POST, CREATE_COMMENT, LIKE_COMMENT, DISLIKE_COMMENT, SEARCH_POSTS, SEARCH_USER, TREND, REFRESH, FOLLOW, MUTE, DO_NOTHING`. Include `DO_NOTHING` — populations that always act are unrealistic and inflate cost.

**Step 3: Config persistence and override**
Write the generated config to `data/simulations/<sim_id>/config.json` and expose it for operator review and editing before the run starts. Generated scenarios are frequently *almost* right; a human edit pass materially improves output quality.

**Step 4: Config test units**
**Tests:** `tests/test_simulation_config.py` — generated config validates against the schema; round count respects `MAX_ROUNDS`; scheduled event rounds fall within the window.
`tests/test_action_space.py` — only OASIS-supported actions appear; per-platform action sets are correct; an unknown action is rejected at validation rather than at runtime.

---

### Phase 6: Simulation execution engine

**Step 1: OASIS integration with local inference**
Build `backend/app/services/simulation_runner.py`. Instantiate the OASIS environment using CAMEL's `ModelFactory` bound to local Ollama:

```python
from camel.models import ModelFactory
from camel.types import ModelPlatformType

model = ModelFactory.create(
    model_platform=ModelPlatformType.OLLAMA,
    model_type=Config.LLM_MODEL_NAME,
    url=Config.LLM_BASE_URL,
    model_config_dict={"temperature": 0.7},
)
```

This is the single most important integration point in the project: it is what keeps every one of the thousands of agent turns on local hardware. Verify it before building anything on top — a smoke test of three agents for two rounds, confirming via Ollama's logs that requests arrive locally.

**Step 2: Process isolation and IPC**
Run each simulation in a separate OS process, not a thread. Runs are long, memory-heavy, and must be independently killable without taking down the API. Build `simulation_ipc.py` for control-plane messaging (status, stop, interview requests) over a queue or Unix socket, and `simulation_manager.py` to track PIDs, lifecycle state, and cleanup of orphaned processes on restart.

**Step 3: Round loop and persistence**
Drive the OASIS environment round by round. After each round, persist agent actions, posts, and comments to the run's SQLite database, and write a checkpoint enabling resume. Emit structured progress: current round, total rounds, per-action counts, agents active.

**Step 4: Graph memory feedback (optional, flagged)**
Optionally feed significant simulation outcomes back into the Neo4j graph as new nodes and edges, so agent memory evolves across rounds. Build `backend/app/services/graph_memory_updater.py`. Keep this behind a config flag — it roughly doubles graph writes and materially increases run time. Upstream's equivalent was the single most complex and most-tested subsystem; treat it as genuinely hard and do not attempt it before Phases 1–7 are green.

**Step 5: Simulation control API**
Build `backend/app/api/simulation.py`: `POST /api/simulation/create`, `POST /api/simulation/prepare` (async profile + config generation, returns task ID), `GET /api/simulation/prepare/status`, `GET /api/simulation/<id>/config`, `GET /api/simulation/<id>/profiles`, `POST /api/simulation/start`, `POST /api/simulation/stop`, `GET /api/simulation/list`.

**Step 6: Engine test units**
**Tests:** `tests/test_ollama_model_binding.py` — assert `ModelFactory` is constructed with `ModelPlatformType.OLLAMA` and the configured local URL, and that no code path can construct an OpenAI-cloud model. Guards the core privacy property at the unit level.
`tests/test_simulation_lifecycle.py` — create → prepare → start → stop transitions; invalid transitions (starting an unprepared simulation) are rejected with a clear error.
`tests/test_simulation_persistence.py` — actions, posts, and comments persist to SQLite with correct round attribution; checkpoints are written.
`tests/test_simulation_resume.py` — a run killed mid-flight resumes from its last checkpoint without duplicating rounds.
`tests/test_process_isolation.py` — killing a simulation process does not affect the API; orphaned processes are reaped on manager restart.
`tests/test_simulation_smoke.py` — a genuine end-to-end micro-run (3 agents, 2 rounds) against real local Ollama. Slow; mark it `@pytest.mark.integration` and exclude from the fast unit suite, but require it before any release.

---

### Phase 7: Monitoring, data access, and agent interviews

**Step 1: Run status and timeline endpoints**
Implement `GET /api/simulation/<id>/run-status` (state, current/total rounds, percent, action counts), `GET /api/simulation/<id>/run-status/detail` (recent action log), `GET /api/simulation/<id>/timeline` (per-round aggregates with optional range), `GET /api/simulation/<id>/agent-stats` (per-agent activity).

**Step 2: Content access endpoints**
Implement paginated `GET /api/simulation/<id>/actions` (filter by platform, agent, round), `GET /api/simulation/<id>/posts`, `GET /api/simulation/<id>/comments` (optionally filtered by post). Enforce sane page limits — a large run holds tens of thousands of rows.

**Step 3: Agent interview**
Implement `POST /api/simulation/interview` (ask one agent a question mid-run, in character and with its accumulated memory), `POST /api/simulation/interview/batch`, `POST /api/simulation/interview/all`, and `POST /api/simulation/interview/history`. Interviews route through the IPC channel into the live simulation process. This is the feature that turns a simulation into an instrument you can probe — prioritise it.

**Step 4: Environment health**
Implement `POST /api/simulation/env-status` (is the environment alive and accepting commands) and `POST /api/simulation/close-env` (graceful shutdown with timeout).

**Step 5: Monitoring test units**
**Tests:** `tests/test_monitoring_api.py` — every endpoint returns the documented shape; pagination boundaries are correct; filters compose.
`tests/test_interview.py` — a single interview returns a response attributed to the right agent; batch returns one result per request; interviewing a non-existent agent errors cleanly; an interview against a stopped simulation fails fast rather than hanging.
`tests/test_ipc.py` — control messages round-trip; a timeout on an unresponsive process is handled without deadlocking the API.

---

### Phase 8: Report generation

**Step 1: Report agent**
Build `backend/app/services/report_agent.py`. Given a completed run, produce a structured analytical report: executive summary, sentiment trajectory across rounds, dominant narratives and counter-narratives, influential agents and how influence propagated, notable emergent behaviour, and explicit caveats. Give the agent read-only tools over the run data (query posts, aggregate sentiment, fetch agent history) with a bounded tool-call budget (default 5) and bounded reflection rounds (default 2) — unbounded agent loops on a local 14b model are a reliable way to burn an afternoon.

**Step 2: Grounding and citation**
Every claim in the report must cite the underlying data — specific post IDs, agent IDs, round numbers. A simulation report that cannot be traced back to simulated evidence is indistinguishable from the model's prior assumptions, which defeats the purpose.

**Step 3: Report API and persistence**
Build `backend/app/api/report.py`: `POST /api/report/generate` (async, returns task ID), `GET /api/report/status/<task_id>`, `GET /api/report/<report_id>`, `GET /api/report/<report_id>/export` (Markdown and HTML). Persist reports under `data/reports/`.

**Step 4: Report test units**
**Tests:** `tests/test_report_agent.py` — with a fixture run, a report generates containing all required sections; the tool-call budget is enforced; reflection rounds are capped.
`tests/test_report_grounding.py` — every citation in a generated report resolves to a real post/agent/round in the run database. Directly tests the anti-fabrication property.
`tests/test_report_sanitizer.py` — tool results are sanitised before entering the prompt; oversized results are truncated rather than blowing the context window.
`tests/test_report_api.py` — generation is async; status polling works; export produces valid Markdown and HTML.

---

### Phase 9: Frontend

**Step 1: Application shell**
Vue 3 + Vite. Router with views for Home/project list, the five-stage workflow, and a run history browser. An API client module wrapping the backend with consistent error handling and polling helpers.

**Step 2: Stage 1 — graph build**
Upload UI (drag-drop, type and size validation client-side), ontology review and edit, extraction progress, and an interactive graph visualisation (Cytoscape.js or vis-network) with type filtering and node inspection.

**Step 3: Stage 2 — environment setup**
Profile review: browse generated agents, inspect personas, see the named-vs-synthetic breakdown clearly, and edit or remove agents before the run.

**Step 4: Stage 3 — simulation**
Config review and edit, platform selection, round count, launch controls, and a live run view — progress bar, round counter, streaming action feed, per-agent activity.

**Step 5: Stage 4 — report**
Rendered report with charts (sentiment over rounds, action distribution, influence graph), citation links that jump to the underlying post, and export buttons.

**Step 6: Stage 5 — interaction**
Interview UI: pick an agent (or all), ask a question, view responses, browse interview history.

**Step 7: Frontend test units**
**Tests:** component tests with Vitest for upload validation, config form validation, and polling state machines (idle → running → complete → error). One Playwright end-to-end test walking upload → graph → profiles → short run → report against a live sealed stack.

---

### Phase 10: Integration testing, egress verification, and operations

**Step 1: Full pipeline integration test**
`tests/test_e2e_pipeline.py` — a fixture document runs the complete pipeline end to end (upload → graph → profiles → config → 3-agent/2-round simulation → report) against real local services. Marked `integration`, run before every release.

**Step 2: Egress verification suite**
`tests/test_egress_verification.py` — the compliance gate. Assert the backend container has no route off-host; assert config validation rejects external URLs; assert no source file contains a non-allowlisted URL literal (grep the tree for `http(s)://` and diff against the allowlist); optionally capture traffic during a short run and assert every destination is in the allowlist. **Treat a failure here as a release blocker, not a warning.**

**Step 3: Performance baseline**
Record wall-clock timings for a standard workload (50 agents, 10 rounds) on the target hardware. Store as a baseline so regressions are visible. Document expected duration prominently — users must know a real run takes hours, not minutes.

**Step 4: Operational tooling**
Health endpoint reporting Ollama reachability, Neo4j connectivity, model availability, and disk headroom. Structured JSON logging. A backup script for the Neo4j store and `data/`. A cleanup command for old simulation databases.

**Step 5: Documentation**
`README.md` (quick start), `docs/ARCHITECTURE.md` (component diagram and data flow), `docs/PROVISIONING.md` (the one-time internet-connected model pull, and how to re-seal afterwards), `docs/PRIVACY.md` (the allowlist, how sealing is enforced, how to verify it independently).

---

## Instructions

### One-time provisioning (requires internet)

```bash
git clone https://github.com/CyberSecDef/crowdsight && cd crowdsight
cp .env.example .env          # defaults are already local-only; set NEO4J_PASSWORD

# Temporarily attach Ollama to a routable network and pull models
docker compose -f docker-compose.yml -f docker-compose.provision.yml up -d ollama
docker compose exec ollama ollama pull qwen2.5:14b
docker compose exec ollama ollama pull nomic-embed-text
docker compose -f docker-compose.yml -f docker-compose.provision.yml down
```

### Normal sealed startup

```bash
docker compose up -d                 # internal network only — no egress
docker compose exec backend pytest tests/test_egress_verification.py   # prove it
```

Open `http://localhost:8080`.

### Running a simulation

1. **Upload** a source document (PDF, Markdown, or text, under 50 MB). Review the proposed ontology and adjust entity/relationship types, then start extraction. Inspect the resulting knowledge graph.
2. **Generate agents.** Choose how many, and the named-to-synthetic ratio. Review the personas — check that named agents match the source and synthetic ones are plausible. Edit or drop any that look wrong. Errors here propagate through the whole run.
3. **Review the scenario config.** Check the event description, seed posts, round count, and any scheduled mid-run events. Edit freely — this is the cheapest point to improve output quality.
4. **Start the run.** Pick platform (Twitter-style or Reddit-style) and rounds. Watch the live feed. **Begin with 3–5 agents and 2 rounds to validate the pipeline before committing to a long run.** A 300-agent × 20-round run on a 12 GB GPU is an overnight job.
5. **Interview agents** mid-run or after. Ask why they posted something, what they think of another agent's claim, how they would respond to a hypothetical. This is where most of the analytical value is.
6. **Generate the report.** Read it with the citations open — every claim should trace to specific posts. Export to Markdown or HTML.

### Verifying nothing left your network

```bash
docker network inspect crowdsight_internal | grep -i internal    # expect: "Internal": true
docker compose exec backend python -c "import socket; socket.create_connection(('1.1.1.1',443),3)"
# expect failure — no route
```

### Operational notes

- Ollama serialises requests; concurrency above ~4 degrades throughput. Tune `LLM_CONCURRENCY`, don't raise it blindly.
- Neo4j heap defaults are conservative. Raise `NEO4J_server_memory_heap_max__size` for graphs over ~10k nodes.
- Each run's SQLite database lives in `data/simulations/<sim_id>/`. They accumulate; prune periodically.
- If you move to a 24 GB+ GPU, switch `LLM_MODEL_NAME` to `qwen2.5:32b` for materially better persona coherence and report quality.

---

## Appendix: What this replaces, and why from scratch

The reference implementation (MiroFish) is a Flask + Vue orchestration layer over CAMEL-AI's OASIS simulation engine, using Zep Cloud for graph memory and an external LLM API for inference. Two of its dependencies are structurally incompatible with a sealed deployment:

- **Zep Cloud is unavoidable upstream.** Its config exposes only `ZEP_API_KEY`, and `Config.validate()` *explicitly rejects* a `ZEP_API_URL` override — self-hosting is deliberately blocked. Zep also stopped maintaining and releasing its self-hosted Community Edition in April 2025 — the repository remains Apache 2.0 but receives no updates or support, and open-source effort moved to Graphiti — so there is no supported local Zep to point at.
- **The default LLM path is a cloud API.** This one is genuinely configurable and needs no rework.

This build keeps what is genuinely good and open — **OASIS (Apache 2.0)** for the agent simulation engine, driven through CAMEL's first-class `ModelPlatformType.OLLAMA` binding — and replaces the memory layer with Neo4j plus local embeddings. Building fresh rather than forking also drops ~1,000 strings of translation scaffolding, the cloud-contract test suite (10 of 18 upstream tests exist solely to test Zep), and lets the egress guarantee be designed in from Phase 1 rather than retrofitted onto a codebase that assumed cloud access throughout.
