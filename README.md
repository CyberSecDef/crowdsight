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

The only step requiring internet access is one-time provisioning (pulling model weights
and packages), after which the stack runs sealed.

### Designated endpoints — the complete allowlist

| Purpose | Endpoint | Protocol |
|---|---|---|
| LLM chat completions | `http://ollama:11434/v1` | OpenAI-compatible HTTP |
| Text embeddings | `http://ollama:11434/api/embeddings` | Ollama native HTTP |
| Knowledge graph | `bolt://neo4j:7687` | Bolt |
| Simulation state | `./data/simulations/` | local filesystem |
| Backend API | `http://localhost:5000` | HTTP (loopback/LAN) |
| Frontend | `http://localhost:5173` (dev) / `:8080` (prod) | HTTP |

Any other outbound destination is a defect. There is no third-party API key anywhere in
this system; `LLM_API_KEY` exists solely because the OpenAI SDK requires a non-empty
string, and its value must be the literal `ollama`.

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

- **Python 3.12** — pinned exactly. Not 3.13/3.14; the CAMEL/OASIS dependency tree lags.
  The backend must be containerised or run in a pinned 3.12 virtualenv.
- **Node.js 20 LTS+** and npm — frontend build only.
- **Docker Engine 24+** and Docker Compose v2.
- **Ollama 0.30+** — serves both chat completions and embeddings on `:11434`.
- **Neo4j Community Edition 5.15+**.
- **Models** — `qwen2.5:14b` (reasoning/generation) and `nomic-embed-text` (768-dim
  embeddings). Optionally `qwen2.5:32b` on higher-VRAM hosts.

Key Python packages: `flask>=3.0`, `flask-cors>=6.0`, `openai>=1.0` (SDK only, pointed at
Ollama), `camel-ai==0.2.78`, `camel-oasis==0.2.5`, `neo4j>=5.15`, `pydantic>=2.0`,
`PyMuPDF>=1.24`, `python-dotenv>=1.0`, `httpx>=0.27`, `charset-normalizer`, `chardet`.

---

## Quick start

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
docker compose up -d                 # internal network only — no egress
docker compose exec backend pytest tests/test_egress_verification.py   # prove it
```

Then open <http://localhost:8080>.

### Verifying nothing left your network

```bash
docker network inspect crowdsight_internal | grep -i internal    # expect: "Internal": true
docker compose exec backend python -c "import socket; socket.create_connection(('1.1.1.1',443),3)"
# expect failure — no route
```

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

| Layer | Component |
|---|---|
| Inference | Ollama (`qwen2.5:14b`, `nomic-embed-text`) |
| Graph memory | Neo4j 5.15+ over Bolt |
| Simulation | CAMEL-AI OASIS (Apache 2.0), one OS process per run |
| Run state | SQLite under `data/simulations/<sim_id>/` |
| API | Flask |
| UI | Vue 3 + Vite, Cytoscape.js / vis-network |

### Repository layout

```
backend/
  app/
    api/          # graph, simulation, report routes
    services/     # ontology, graph builder, profiles, config, runner, report agent
    storage/      # neo4j, embeddings, NER, search
    utils/        # llm client, retry, file parser
    config.py     # single source of truth; validate() rejects off-host URLs
  tests/
frontend/src/
data/{uploads,graphs,simulations,reports}
docker-compose.yml
docker-compose.provision.yml
```

---

## Configuration

All settings live in `backend/app/config.py`, read from environment variables with
defaults. Copy `.env.example` to `.env` to override.

| Variable | Default |
|---|---|
| `LLM_BASE_URL` | `http://ollama:11434/v1` |
| `LLM_MODEL_NAME` | `qwen2.5:14b` |
| `LLM_API_KEY` | `ollama` (literal — the SDK just needs a non-empty string) |
| `EMBEDDING_BASE_URL` | Ollama embeddings endpoint |
| `EMBEDDING_MODEL` | `nomic-embed-text` |
| `NEO4J_URI` | `bolt://neo4j:7687` |
| `NEO4J_USER` / `NEO4J_PASSWORD` | operator-supplied |
| `MAX_ROUNDS` | `10` |
| `MAX_AGENTS` | `100` |
| `REPORT_TEMPERATURE` | `0.5` |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `500` / `50` |
| `MAX_CONTENT_LENGTH` | 50 MB |
| `ALLOWED_EXTENSIONS` | `pdf, md, txt, markdown` |

`Config.validate()` parses `LLM_BASE_URL`, `EMBEDDING_BASE_URL`, and `NEO4J_URI` and
**refuses to start** if a hostname is not in the allowlist (`localhost`, `127.0.0.1`,
`ollama`, `neo4j`, or an operator-supplied `ALLOWED_HOSTS`). Missing required variables
fail loudly rather than defaulting silently.

---

## Testing

```bash
docker compose exec backend pytest                            # fast unit suite
docker compose exec backend pytest -m integration             # slow, needs live services
docker compose exec backend pytest tests/test_egress_verification.py
```

Integration tests (`test_simulation_smoke.py`, `test_e2e_pipeline.py`) run real micro-runs
against local Ollama and are required before any release.

**`tests/test_egress_verification.py` is the compliance gate.** It asserts the backend
container has no route off-host, that config validation rejects external URLs, and that no
source file contains a non-allowlisted URL literal. A failure there is a release blocker,
not a warning.

Other high-value tests: `test_oasis_profile_contract.py` (schema mismatches otherwise
surface hours into a run), `test_ollama_model_binding.py` (no code path can construct a
cloud model), and `test_report_grounding.py` (every citation resolves to real run data).

---

## Operational notes

- Ollama serialises requests; concurrency above ~4 degrades throughput. Tune
  `LLM_CONCURRENCY` rather than raising it blindly.
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
