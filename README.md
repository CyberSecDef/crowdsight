# CrowdSight

**A local-only swarm simulation and prediction engine.**

CrowdSight takes a source document — a news article, a policy draft, a product
announcement, an incident report — and builds a knowledge graph of the entities and
relationships inside it. From that graph it generates a population of hundreds of
distinct AI agents, each with its own persona, memory, and behavioural disposition.
Those agents are placed in a simulated social platform (Twitter-style or Reddit-style)
and run for a configurable number of rounds, posting, commenting, liking, reposting,
following, searching, and reacting to one another.

The output is not a statistical forecast but a **simulated collective reaction**: what a
population of plausible individuals would say and do in response to an event, how
sentiment clusters and shifts, which sub-narratives take hold, and which agents become
influential.

> Full requirements specification: [`REQ_SPEC.md`](REQ_SPEC.md)

---

## Sealed by design

CrowdSight is built for a fully sealed deployment. Every inference call runs against a
local Ollama instance, all graph memory lives in a local Neo4j instance, and all
simulation state lives in local SQLite files. There are no cloud services, no external
memory providers, and no telemetry.

Network egress is denied at the **container-network level** — the Compose network is
declared `internal: true`, which removes the default gateway — rather than merely avoided
by configuration. The "nothing leaves the network" property is structurally enforced and
independently verifiable, not a matter of trusting a config file.

One container is deliberately outside that seal: a stateless nginx **gateway**, which
publishes the ports and reverse-proxies into the sealed network. It exists because a
container on an `internal: true` network cannot be reached *from the host* either —
reachability and egress are the same property — so the alternative would be giving the
backend itself a route to the internet. The gateway holds no application code, no
credentials and no data. Everything that touches a document, the knowledge graph, an LLM
prompt or simulation data stays sealed.

The only step requiring internet access is one-time provisioning (pulling model weights
and packages), after which the stack runs sealed.

### Designated endpoints — the complete allowlist

| Purpose | Endpoint | Protocol |
|---|---|---|
| LLM chat completions | `http://ollama:11434/v1` | OpenAI-compatible HTTP |
| Text embeddings | `http://ollama:11434/api/embed` | Ollama native HTTP |
| Knowledge graph | `bolt://neo4j:7687` | Bolt |
| Simulation state | `./data/simulations/` | local filesystem |
| Backend API | `http://localhost:5000` | HTTP (loopback/LAN) |
| Frontend | `http://localhost:5173` (dev) / `:8080` (prod) | HTTP |

Service names are the preferred form, and loopback (`localhost`, `127.0.0.1`, `::1`) is
equally good outside Compose. A **private LAN address** — RFC 1918, link-local, or
unique-local — is permitted where genuinely necessary, such as Ollama running on a
separate GPU box, but it is second-best and CrowdSight says so out loud: every such
endpoint logs a warning at startup, because traffic to another machine still leaves this
host and the container-level egress seal cannot cover it. Public addresses and public
hostnames are refused outright.

Any other outbound destination is a defect. There is no third-party API key anywhere in
this system. `LLM_API_KEY` exists because the OpenAI SDK requires a non-empty string; it
defaults to the literal `ollama`, which Ollama ignores. It is settable so a local
OpenAI-compatible gateway (LiteLLM, vLLM) can be given a token — a value that, like
everything else here, never leaves the perimeter.

---

## Features

- **Adaptive ontology generation** — the LLM proposes a domain-appropriate entity and
  relationship schema per document instead of forcing a fixed one; operator reviewable.
- **Knowledge graph construction** — entity/relationship extraction with cross-chunk
  deduplication by normalised name plus embedding similarity, with full provenance back
  to source text.
- **Persona synthesis** — graph entities become agents with name, age, occupation, bio,
  personality traits, interests, leanings, activity level, and writing style.
- **Synthetic population expansion** — plausible unnamed crowd members fill out the
  population at a configurable named-to-synthetic ratio, with `provenance: named|synthetic`
  marked on every profile.
- **OASIS-driven simulation** — CAMEL-AI's OASIS engine bound to local Ollama via
  `ModelPlatformType.OLLAMA`, with per-platform action spaces (including `DO_NOTHING`).
- **Process-isolated runs** — each simulation runs in its own OS process, independently
  killable, checkpointed, and resumable.
- **Agent interviews** — question any agent (or all of them) mid-run or after, in
  character and with its accumulated memory, over the live IPC channel.
- **Grounded reports** — executive summary, sentiment trajectory, dominant and
  counter-narratives, influential agents, emergent behaviour, and caveats — with every
  claim citing specific post IDs, agent IDs, and round numbers.
- **Vue 3 frontend** — five-stage workflow with interactive graph visualisation, live run
  feed, charts, and citation links that jump to the underlying post.

---

## Requirements

### Hardware

Sized against the reference target (Ryzen 9 8940HX, 32 threads, 61 GB RAM,
RTX 5070 Ti Laptop 12 GB VRAM, 1.5 TB free).

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 8 cores | 16+ threads |
| RAM | 16 GB | 32–64 GB |
| VRAM | 10 GB (14b-class model) | 24 GB (32b-class at full speed) |
| Disk | 50 GB | 100 GB+ |
| Network | LAN only at runtime; internet once for provisioning | |

12 GB VRAM runs a 14b model comfortably and 32b only with partial CPU offload, which is
significantly slower. CPU-only inference works but is impractical for multi-hundred-agent
runs.

### Software

- **Python 3.11** — pinned exactly. Not 3.12+: every published version of `camel-oasis`
  declares `Requires-Python <3.12`, so the simulation engine will not install on 3.12.
  The backend must be containerised or run in a pinned 3.11 virtualenv.
- **Node.js 20 LTS+** and npm — frontend build only.
- **Docker Engine 24+** and Docker Compose v2.
- **Ollama 0.30+** — serves both chat completions and embeddings on `:11434`.
- **Neo4j Community Edition 5.15+**.
- **Models** — `qwen2.5:14b` (reasoning/generation) and `nomic-embed-text` (768-dim
  embeddings). Optionally `qwen2.5:32b` on higher-VRAM hosts.

Key Python packages: `flask>=3.0`, `flask-cors>=6.0`, `openai>=1.0` (SDK only, pointed at
Ollama), `camel-ai==0.2.78`, `camel-oasis==0.2.5`, `neo4j>=5.15`, `pydantic>=2.0`,
`pydantic-settings>=2.2`, `jsonschema>=4.0`, `PyMuPDF>=1.24`, `python-dotenv>=1.0`,
`httpx>=0.27`, `charset-normalizer`, `chardet`. Full list in `backend/requirements.txt`.

---

## Quick start

### Prerequisite: NVIDIA Container Toolkit

Ollama takes the GPU by device reservation, so the toolkit must be installed or the
`ollama` service will not start. That failure is deliberate — CPU-only inference is
impractical for multi-hundred-agent runs, and silently falling back to it turns an
overnight job into a multi-day one.

The toolkit is not in Ubuntu's own repositories; add NVIDIA's first.

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# verify
docker info --format '{{.Runtimes}}' | grep -q nvidia && echo "runtime registered"
docker run --rm --gpus all ubuntu:24.04 nvidia-smi --query-gpu=name --format=csv,noheader
```

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

The provisioning overlay must **never** be used at runtime.

### Normal sealed startup

```bash
docker compose up -d                 # sealed network only — no egress
docker compose exec backend python -m app.egress_check                 # prove it
```

Then open <http://localhost:8080>. Until Phase 9 builds the frontend, that address serves
a placeholder and the API is the live part:

```bash
curl http://localhost:8080/api/health     # through the gateway
curl http://localhost:5000/api/health     # direct to the API
```

Ports bind to `127.0.0.1` by default. There is no authentication in front of this stack;
set `CROWDSIGHT_BIND=0.0.0.0` in `.env` only if you mean to expose it to your LAN. Once
the frontend exists, bring it up with `docker compose --profile frontend up -d`.

### Verifying nothing left your network

```bash
docker compose exec backend python -m app.egress_check   # expect: SEALED
docker network inspect crowdsight_sealed --format '{{.Internal}}'   # expect: true
docker port crowdsight-backend                            # expect: no output
```

`app.egress_check` attempts TCP connections to three external addresses and DNS
resolution of three external names from inside the backend container, and exits non-zero
if any of them succeeds.

---

## Running a simulation

1. **Upload** a source document (PDF, Markdown, or text, under 50 MB). Review the proposed
   ontology and adjust entity/relationship types, then start extraction. Inspect the
   resulting knowledge graph.
2. **Generate agents.** Choose how many, and the named-to-synthetic ratio. Review the
   personas — check that named agents match the source and synthetic ones are plausible.
   Edit or drop any that look wrong; errors here propagate through the whole run.
3. **Review the scenario config.** Check the event description, seed posts, round count,
   and any scheduled mid-run events. This is the cheapest point to improve output quality.
4. **Start the run.** Pick platform and rounds, then watch the live feed. **Begin with 3–5
   agents and 2 rounds to validate the pipeline before committing to a long run.**
5. **Interview agents** mid-run or after — why they posted something, what they think of
   another agent's claim, how they'd respond to a hypothetical. Most of the analytical
   value lives here.
6. **Generate the report.** Read it with the citations open; every claim should trace to
   specific posts. Export to Markdown or HTML.

> **Runs take hours, not minutes.** A 300-agent × 20-round simulation issues on the order
> of 6,000+ LLM completions plus embedding calls. On a single 12 GB GPU running a 14b
> model that is an overnight job. Every long-running operation is an async job with
> progress polling and resumability — never a blocking HTTP request.

---

## Architecture

```
Document ─▶ Parse & chunk ─▶ Ontology ─▶ NER extraction ─▶ Neo4j knowledge graph
                                                                   │
                                                                   ▼
                                             Persona synthesis + synthetic expansion
                                                                   │
                                                                   ▼
                                            Scenario config (event, seeds, schedule)
                                                                   │
                                                                   ▼
                                  OASIS simulation (isolated process) ─▶ SQLite per run
                                                   │                          │
                                             Interviews (IPC)          Grounded report
```

| Layer | Component | Network |
|---|---|---|
| Ingress | nginx reverse proxy, stateless | `edge` (publishes `:8080`, `:5000`) |
| Inference | Ollama (`qwen2.5:14b`, `nomic-embed-text`) | `sealed` |
| Graph memory | Neo4j 5.15+ over Bolt | `sealed` |
| Simulation | CAMEL-AI OASIS (Apache 2.0), one OS process per run | `sealed` |
| Run state | SQLite under `data/simulations/<sim_id>/` | `sealed` |
| API | Flask | `sealed` |
| UI | Vue 3 + Vite, Cytoscape.js / vis-network | `sealed` |

```
host :8080 / :5000
      |
[ gateway ]  <- edge network, the only container with a route off-host
      |
======|====== sealed network (internal: true, no default gateway) ======
      |
[ frontend ] [ backend ] --> [ ollama ] [ neo4j ]
```

### Repository layout

```
backend/
  app/
    api/              # graph, simulation, report routes
    services/         # ontology, graph builder, profiles, config, runner, report agent
    storage/          # neo4j, embeddings, NER, search
    utils/            # llm client, retry, file parser
    config.py         # single source of truth; rejects off-host endpoints
    main.py           # Flask entrypoint + health
    egress_check.py   # proves the seal from inside the container
  tests/
  requirements.txt
frontend/src/
docker/gateway/       # nginx reverse-proxy config + placeholder page
data/{uploads,graphs,simulations,reports}
docker-compose.yml
docker-compose.provision.yml
Dockerfile
```

---

## Configuration

All settings live in `backend/app/config.py`, a `pydantic-settings` `BaseSettings` model.
Every field is typed, constrained, and bound to the environment variable of the same name.
Copy `.env.example` to `.env` to override. Inspect the resolved configuration — and check
it — with:

```bash
docker compose exec backend python -m app.config
```

| Variable | Default |
|---|---|
| `LLM_BASE_URL` | `http://ollama:11434/v1` |
| `LLM_MODEL_NAME` | `qwen2.5:14b` |
| `LLM_API_KEY` | `ollama` (inert; the SDK just needs a non-empty string) |
| `LLM_CONCURRENCY` | `4` (per process — Phase 6 divides it across simulation workers) |
| `LLM_TIMEOUT` / `LLM_CONNECT_TIMEOUT` | `300` / `10` seconds |
| `LLM_MAX_ATTEMPTS` | `3` |
| `LLM_RETRY_BASE_DELAY` / `LLM_RETRY_MAX_DELAY` | `1.0` / `30.0` seconds |
| `EMBEDDING_BASE_URL` | `http://ollama:11434` |
| `EMBEDDING_MODEL` | `nomic-embed-text` |
| `EMBEDDING_DIM` | `768` |
| `EMBEDDING_BATCH_SIZE` | `32` |
| `EMBEDDING_CACHE_PATH` | `data/cache/embeddings.db` |
| `EMBEDDING_CACHE_ENABLED` | `true` |
| `ENTITY_SIMILARITY_THRESHOLD` | `0.90` |
| `NEO4J_URI` | `bolt://neo4j:7687` |
| `NEO4J_USER` | `neo4j` |
| `NEO4J_PASSWORD` | **none — you must set this** |
| `NEO4J_DATABASE` | `neo4j` |
| `NEO4J_MAX_POOL_SIZE` | `50` |
| `NEO4J_CONNECTION_TIMEOUT` | `30` seconds |
| `MAX_ROUNDS` | `10` |
| `MAX_AGENTS` | `100` |
| `POPULATION_NAMED_RATIO` | `0.25` |
| `REPORT_TEMPERATURE` | `0.5` |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1500` / `150` characters |
| `MAX_CONTENT_LENGTH` | 50 MB |
| `ALLOWED_EXTENSIONS` | `pdf, md, txt, markdown` |
| `ALLOWED_HOSTS` | empty |

Constructing the config validates it. A model validator parses `LLM_BASE_URL`,
`EMBEDDING_BASE_URL`, and `NEO4J_URI` and classifies each host:

| Host | Result |
|---|---|
| `localhost`, `127.0.0.1`, `::1` | accepted silently — **preferred** |
| `ollama`, `neo4j` | accepted silently — the default deployment |
| `10.x`, `172.16–31.x`, `192.168.x`, `169.254.x`, `fd00::/8` | accepted **with a warning** |
| anything in `ALLOWED_HOSTS` | accepted with a warning |
| public IPs and public hostnames | **refused — the process does not start** |

Hostnames are never resolved during this check. DNS can point anywhere, and a guarantee
that depends on what a resolver returns today is not a guarantee — so a name is trusted
only if it is a known service name or an explicit opt-in, while IP literals are judged by
the address itself. Missing required variables and out-of-range values (`MAX_ROUNDS < 1`,
`REPORT_TEMPERATURE > 2.0`, `CHUNK_OVERLAP >= CHUNK_SIZE`) fail loudly rather than
defaulting silently, and every problem is reported at once rather than one per restart.

---

## Testing

```bash
docker compose exec backend pytest                  # unit + egress, no services needed
docker compose exec backend pytest -m integration   # Neo4j-backed; needs neo4j up
docker compose exec backend pytest -m ""            # everything
pytest backend/tests/test_network_isolation.py      # from the host, stack running
```

`integration` is deselected by default so the unit loop stays fast. `egress` is **not** —
a check you have to remember to ask for is one that eventually nobody asks for. Neither
skips to pass: stop Neo4j and `pytest -m integration` errors rather than going green.

The image's `dev` build target carries pytest; production images are built with
`--target runtime` and stay lean.

The suite is 452 tests: 396 unit (no services, ~2.5s) and 56 integration against live
Neo4j and Ollama (~85s), including a real document upload driven through to a built graph.

Integration tests (`test_simulation_smoke.py`, `test_e2e_pipeline.py`) run real micro-runs
against local Ollama and are required before any release.

**`tests/test_network_isolation.py` is the compliance gate.** It asserts that the backend
container cannot open TCP connections off-host, cannot resolve external DNS names, and has
no default route — and it **never skips**. A test that quietly passes by skipping itself
when it cannot verify the seal is worse than no test at all, so with the stack down the
suite goes red rather than green. A failure there is a release blocker, not a warning.

Run it either way: inside the container it asserts against its own network stack; from the
host it shells in via `docker compose exec`. The topology assertions (network flags, port
bindings, gateway) inspect the Docker daemon and so run host-side only.

Other high-value tests: `test_oasis_profile_contract.py` (schema mismatches otherwise
surface hours into a run), `test_ollama_model_binding.py` (no code path can construct a
cloud model), and `test_report_grounding.py` (every citation resolves to real run data).

---

## Operational notes

- Ollama serialises requests; concurrency above ~4 degrades throughput and risks a GPU
  OOM. Tune `LLM_CONCURRENCY` rather than raising it blindly. The bound is **per process**
  and shared between chat and embeddings, since both contend for the same GPU.
- Transient failures (connection resets, read timeouts, 5xx while a model loads into VRAM)
  are retried with exponential backoff and full jitter. Malformed requests are not retried
  — retrying a 400 wastes minutes and buries the real error.
- Neo4j heap defaults are conservative. Raise `NEO4J_server_memory_heap_max__size` for
  graphs over ~10k nodes.
- Each run's SQLite database lives in `data/simulations/<sim_id>/`. They accumulate; prune
  periodically.
- On a 24 GB+ GPU, switch `LLM_MODEL_NAME` to `qwen2.5:32b` for materially better persona
  coherence and report quality.

---

## Documentation

| Document | Contents |
|---|---|
| [`REQ_SPEC.md`](REQ_SPEC.md) | Full requirements specification, all 10 phases |
| `docs/ARCHITECTURE.md` | Component diagram and data flow |
| `docs/PROVISIONING.md` | One-time model pull and how to re-seal afterwards |
| `docs/PRIVACY.md` | The allowlist, how sealing is enforced, how to verify it |

---

## Background

CrowdSight is a clean-room build, functionally equivalent to MiroFish (a Flask + Vue
orchestration layer over OASIS) but running entirely on local infrastructure. Two upstream
dependencies are structurally incompatible with a sealed deployment: Zep Cloud, whose
config exposes only `ZEP_API_KEY` and explicitly rejects a self-hosted URL override (and
whose Community Edition stopped being maintained in April 2025, with open-source effort
moving to Graphiti), and a default cloud LLM path.

This build keeps what is genuinely good and open — **OASIS (Apache 2.0)** for the agent
simulation engine, driven through CAMEL's first-class `ModelPlatformType.OLLAMA` binding —
and replaces the memory layer with Neo4j plus local embeddings. Building fresh rather than
forking drops the cloud-contract test suite and lets the egress guarantee be designed in
from Phase 1 rather than retrofitted onto a codebase that assumed cloud access throughout.
