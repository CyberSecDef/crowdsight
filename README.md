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

Every inference call runs against a local Ollama instance, all graph memory lives in a
local Neo4j instance, and all simulation state lives in local SQLite files. There are no
cloud services, no external memory providers, and no telemetry.

The guarantee is **structural, not behavioural**. Egress is denied at the
container-network level — the Compose network is declared `internal: true`, which removes
the default gateway — so a request to somewhere else fails whether or not anyone meant to
make it, including from a dependency nobody audited. One stateless nginx gateway sits
outside the seal to publish the ports; it holds no application code, no credentials and
no data.

Check it yourself in about ten seconds:

```bash
docker compose exec backend python -m app.egress_check
```

**→ [`docs/PRIVACY.md`](docs/PRIVACY.md)** — the complete allowlist, the four layers that
enforce the seal, the one residual channel that is documented rather than hidden, and
every command needed to verify all of it independently.

**→ [`docs/PROVISIONING.md`](docs/PROVISIONING.md)** — the one time the network is needed,
and two assets that are fetched lazily at runtime and degrade a simulation *silently* if
they are missing.

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
- **Graph memory feedback (optional)** — simulation outcomes written back to Neo4j and
  fed into later rounds' prompts, under their own `Sim*` labels so invented content is
  never confused with what the source document said. Off by default: it changes what the
  simulation measures, so a run with it on is not comparable with one without.
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
`httpx>=0.27`, `mcp>=1.9,<2`, `charset-normalizer`, `chardet`. Full list in
`backend/requirements.txt`.

> `mcp` is pinned below 2.0 deliberately: `camel-ai` imports `FastMCP` from
> `mcp.server`, which 2.0 removed, and an unpinned install makes `import oasis`
> fail outright.

---

## How long a run takes

**Hours, not minutes.** Every agent turn is a full inference call on a local model,
so wall-clock scales with agents × rounds and there is no shortcut. Measured on the
reference machine (RTX 5070 Ti Laptop 12 GB, Ryzen 9 8940HX, 61 GB):

| Stage | 50 agents × 10 rounds |
|---|---|
| Population (50 personas) | 4.3 min |
| Simulation (10 rounds) | 19.4 min |
| Report | 2.9 min |
| **Total** | **26.6 min** |

Two things that table does not show, and both matter when you plan a run:

**Rounds get slower as the run goes on.** The same population took 72 s for round 1
and 145 s for round 10 — roughly double. Agent memory and the feed both grow, so the
prompts grow with them. Estimating a long run from its first couple of rounds will
underestimate it, by about a factor of two over ten rounds.

**Scaling is close to linear in agents.** The spec's headline figure of 300 agents is
about six times this workload, which puts a 300-agent, 10-round run at roughly
**2.5–3 hours** — an overnight job rather than a coffee break. Start small: 20 agents
and 3 rounds finishes in a few minutes and tells you whether the scenario is worth
running properly.

The GPU is genuinely busy throughout — sampled at **83% mean utilisation, 97% median**
during the run — so this is the model's speed, not idle time waiting on coordination.
A single simulation is allocated one concurrent request by default
(`(LLM_CONCURRENCY − API_LLM_RESERVE) // MAX_CONCURRENT_SIMULATIONS`), and raising that
buys less than you would expect for exactly this reason: one 14b generation already
saturates the card.

Re-measure any time, and compare against the stored baseline:

```bash
python scripts/benchmark.py                  # run and report drift
python scripts/benchmark.py --save           # adopt as the new baseline
python scripts/benchmark.py --agents 20 --rounds 3
```

The baseline lives in [`docs/performance-baseline.json`](docs/performance-baseline.json)
with the hardware it was measured on. It reports drift rather than passing or failing:
wall-clock on a shared GPU varies enough that a threshold would either catch nothing or
cry wolf.

---

## Quick start

### Prerequisite: NVIDIA Container Toolkit

Ollama takes the GPU by device reservation, so the toolkit must be installed or the
`ollama` service will not start — deliberately, because silently falling back to CPU
turns an overnight job into a multi-day one. The toolkit is not in Ubuntu's own
repositories, and the kernel module and userspace driver must match.

**→ [`docs/PROVISIONING.md`](docs/PROVISIONING.md#the-gpu)** has the repository setup, the
verification commands, and the driver-mismatch symptom, which is a Docker mount error that
mentions nothing about versions.

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

Then open <http://localhost:8080> for the UI. The API is reachable on the same origin, and
directly on `:5000` for tooling:

```bash
curl http://localhost:8080/api/health     # through the gateway
curl http://localhost:5000/api/health     # direct to the API
```

Ports bind to `127.0.0.1` by default. There is no authentication in front of this stack;
set `CROWDSIGHT_BIND=0.0.0.0` in `.env` only if you mean to expose it to your LAN.

**Building the frontend image needs the npm registry.** It is the one part of the project
that touches the internet outside provisioning, and it does so only during
`docker compose build frontend` — the same category as pulling a base image. `npm ci`
installs exactly what `frontend/package-lock.json` pins, and the resulting container
carries a compiled bundle with no Node and no package manager, on the sealed network.
Verify the shipped UI with `./scripts/verify_frontend.sh` (38 checks), which asserts among
other things that the bundle names no external host and that the container cannot reach
one. The frontend also carries its own suites, run from `frontend/`:

```bash
npm test               # 247 unit + component tests, no services needed
npm run test:e2e       # 80 browser tests — needs the stack up; npx playwright install chromium
npm run test:e2e:pipeline   # the whole walk: upload -> graph -> profiles -> run -> report
```

The browser tests run against the real gateway rather than a dev server, because the CSP,
the cache headers and the history-mode fallback are all things a dev server papers over.

The API sets `nosniff`, a content-type-specific CSP, `Referrer-Policy` and `X-Frame-Options`
on every response, and the gateway sets them again behind `proxy_hide_header` so exactly one
of each reaches the browser. **There is no CORS wildcard**: the UI is same-origin, so no
cross-origin header is needed at all. `CROWDSIGHT_CORS_ORIGINS` accepts a comma-separated
list of explicit origins if you need one, and refuses `*` even when asked.

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

The API mirrors these steps: `POST /api/simulation/create` reserves a run,
`POST /api/simulation/prepare` builds the population and derives the scenario (async —
poll `GET /api/simulation/prepare/status?task_id=…`), `POST /api/simulation/start` runs
it, `GET /api/simulation/<id>/status` reports live progress, and `POST
/api/simulation/stop` ends it at the next round boundary. `GET /api/simulation/budget`
shows how the inference budget is divided when a run seems slow. Starting a run that
previously failed resumes it from its last checkpoint and says so.

1. **Upload** a source document (PDF, Markdown, or text, under 50 MB). Review the proposed
   ontology and adjust entity/relationship types, then start extraction. Inspect the
   resulting knowledge graph.
2. **Generate agents.** Choose how many, and the named-to-synthetic ratio. Review the
   personas — check that named agents match the source and synthetic ones are plausible.
   Edit or drop any that look wrong; errors here propagate through the whole run.
3. **Review the scenario config.** Check the event description, seed posts, round count,
   and any scheduled mid-run events (generated ones are counterfactual and start disabled;
   enabling one is a review decision). This is the cheapest point to improve output quality.
   The config lives at `data/simulations/<sim_id>/config.json` and is editable over
   `PUT /api/simulations/<sim_id>/config` until the run starts. Edits are re-verified
   against the source exactly as generated output is — a quote that is not in the document
   is reassigned to the broadcaster and the correction reported back, so review can improve
   a scenario but cannot attribute an invented statement to a real person. Editing a
   simulation that has already started forks it into a new one rather than rewriting the
   config a run is executing from.
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

Five containers: a stateless **gateway** publishing the ports, the **frontend** serving a
compiled bundle, the **backend** API, **Ollama** holding the GPU, and **Neo4j** holding
the graph. Every simulation runs in its own OS process, spawned rather than forked,
independently killable and resumable from its last completed round.

**→ [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — component and data-flow diagrams,
what each of the five stages owns, and the decisions worth knowing before changing
anything: why the worker builds its own config, how the inference budget is divided, why
round boundaries are rowid high-water marks, and why provenance cannot be edited.

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
| `LLM_CONCURRENCY` | `4` (per process; divided across simulation workers) |
| `MAX_CONCURRENT_SIMULATIONS` | `2` (the budget is divided by this at spawn) |
| `API_LLM_RESERVE` | `1` (held back so the API still answers during a run) |
| `SIMULATION_MEMORY_ROUNDS` | `3` (how many past turns an agent keeps) |
| `GRAPH_MEMORY_FEEDBACK` | `false` (write outcomes to the graph and back into prompts) |
| `GRAPH_MEMORY_MIN_ENGAGEMENT` | `1` (reactions a post needs to be recorded) |
| `GRAPH_MEMORY_TOP_N` | `5` (posts per round reaching the graph) |
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

The suite is 1577 tests: 1514 unit (no services, ~55s) and 63 integration against live
Neo4j and Ollama (~2 min), including a real document upload driven through to a built
graph, a real scenario derivation checked against the config schema, a three-agent
two-round simulation driven end to end against local inference, a live run killed with
SIGKILL mid-round and resumed, and the whole create-prepare-start pipeline. `test_oasis_profile_contract.py` and `test_action_space.py` run in the default
suite despite costing ~4s to import OASIS: they are the checks that a simulation will
actually load its agents and that those agents will actually be able to act, and they
are worth always running.

### Stress testing

`scripts/stress.sh` runs an opt-in load generator that deliberately does what the rest
of the system prevents: it overrides `MAX_AGENTS`, `MAX_CONCURRENT_SIMULATIONS` and the
`LLM_CONCURRENCY` budget, then runs five loads at once — inference far past the
concurrency bound, large embedding batches, several full simulations in parallel
processes, document parsing across every core, and concurrent graph writes.

**It will make the machine unresponsive while it runs.** That is its purpose. It is
gated twice — a `stress` marker the default `addopts` deselects, and an explicit
`CROWDSIGHT_STRESS=1` — so it cannot run by accident.

```bash
scripts/stress.sh                      # 12 minutes, the default shape
MINUTES=3 scripts/stress.sh            # a short, sharp burst
SIMS=6 AGENTS=80 scripts/stress.sh     # heavier
```

It samples CPU, memory and process count from inside the container and the GPU from the
host (the backend image has no `nvidia-smi`), and writes `data/stress-report.json` plus
a per-second `data/stress-gpu.csv`. It asserts nothing about throughput — the numbers
depend on whatever else the box is doing — only that load was generated at all.

The graph load prunes as it writes: unbounded, it produces enough 768-dimension vectors
to exhaust Neo4j's heap within a run, which is a different failure from the one being
looked for.

**`tests/test_simulation_smoke.py` is the release gate.** Two tests: the micro-run
(3 agents, 2 rounds against real local Ollama, asserting round attribution and per-action
counts), and the whole pipeline — create, prepare, start, complete — with a population the
model actually generates. Both are `integration`-marked and excluded from the fast loop.
Run them before any release; assembling the pieces is what has caught the last two real
bugs, neither of which any unit test could see. `test_simulation_config.py`
also carries two: mocked tests can only prove the code does what the model was *assumed*
to do, so these generate a scenario against the live model and assert it validates — and
that every quote it attributes to a named person re-locates in the source at exactly the
offsets recorded.

**`tests/test_network_isolation.py` is the compliance gate.** It asserts that the backend
container cannot open TCP connections off-host, cannot resolve external DNS names, and has
no default route — and it **never skips**. A test that quietly passes by skipping itself
when it cannot verify the seal is worse than no test at all, so with the stack down the
suite goes red rather than green. A failure there is a release blocker, not a warning.

Run it either way: inside the container it asserts against its own network stack; from the
host it shells in via `docker compose exec`. The topology assertions (network flags, port
bindings, gateway) inspect the Docker daemon and so run host-side only.

**`tests/test_egress_verification.py` is the other half of the gate** — the parts a packet
capture cannot see. It audits `backend/app` and `frontend/src` for any URL literal naming a
host outside the allowlist (today they name exactly one: `http://ollama`), checks that every
one of the 182 dependencies in `frontend/package-lock.json` resolves from
`registry.npmjs.org`, asserts the configuration refuses an endpoint outside the perimeter
*for that reason*, and confines the frontend container the same way — no published ports, no
default route, sealed network only, and the npm registry unreachable at runtime.

Both carry the `egress` marker. Run the full gate from the host, where the source tree and
the Docker daemon are both visible:

```bash
pytest backend/tests/test_network_isolation.py backend/tests/test_egress_verification.py
```

Traffic capture during a run is deliberately not part of this: it would mean granting
`NET_RAW` to the container the project exists to confine. The sealed network is
`internal: true`, so there is no route to capture traffic on, and the gate already attempts
real connections to real external hosts and requires them to fail.

Other high-value tests: `test_oasis_profile_contract.py` (schema mismatches otherwise
surface hours into a run), `test_action_space.py` (OASIS answers an unrecognised action
with a log line and a silently shorter tool list, so a typo yields an agent that simply
never acts), `test_ollama_model_binding.py` (no code path can construct a cloud model),
and `test_report_grounding.py` (every citation resolves to real run data).

---

## Operations

### Health

`GET /api/health` reports four things, and they fail differently:

* **reachability** — can the backend open a socket to Ollama and Neo4j
* **model availability** — are `qwen2.5:14b` and `nomic-embed-text` actually pulled.
  Reachable is not the same as usable: a sealed stack whose model was never pulled
  looks perfectly healthy until the first inference call fails, and it **cannot fix
  itself**, because pulling needs the internet the seal removes. A missing model
  makes the endpoint report `degraded`.
* **disk headroom** — free space where runs are written. A run that fills the disk
  loses the round it was writing, and SQLite's error for that is not obviously about
  space.
* **configuration** — validity and any perimeter warnings

`GET /api/health/live` is the liveness probe Docker uses. It touches nothing and
answers 200 while the process is up — a readiness check that failed because Ollama
was busy would restart a container mid-simulation.

### Logging

Human-readable by default. Set `CROWDSIGHT_LOG_FORMAT=json` for one JSON object per
line, with `sim_id`, `round` and anything else a caller attached as real fields rather
than text to be parsed back out:

```json
{"time": "2026-08-08T13:14:26+0000", "level": "INFO", "logger": "app.services.simulation_runner",
 "message": "Round 3: 27 acted, 18 quiet, 0 failed", "sim_id": "sim-20260808-013214-373656", "round": 3}
```

Text is the default on purpose: the worker prefixes every line with its `sim_id` so a
human can follow one run through a stack running two, and that is how a long run is
actually watched.

### Backup

```bash
./scripts/backup.sh [destination]     # default ./backups
```

Captures `data/` (documents, simulation databases, profiles, reports, tasks), the Neo4j
graph, and the configuration including `NEO4J_PASSWORD` — without which the dump cannot
be restored into a working stack.

**Neo4j is stopped for the length of the dump**, usually well under a minute. Community
edition has no online backup: `neo4j-admin database dump` refuses outright while the
database is in use, and copying the volume underneath a running database can produce a
file that looks fine and will not restore. The script says so before it does it, brings
the database back however it exits, and **refuses to start while a simulation is
running** — a database copied mid-round backs up a half-written round.

Models are deliberately not included: they are large, unchanging, and pulling them is
the one-time provisioning step.

### Cleaning up old runs

```bash
python scripts/cleanup.py                        # dry run, 30 days
python scripts/cleanup.py --older-than 60 --delete
```

Dry run by default, because nothing here is recoverable: a run's database holds every
post, action and interview it produced, and the model does not answer the same way
twice.

**A run that has been reported on is never deleted.** Every claim in a report cites
post ids in that database, so removing it would turn each citation in a published
document into a dead link while the report survives to be read. Also protected:
anything running, anything still inside its interview window, and — deliberately —
anything that is not a finished run, so a draft you have not got round to yet is left
alone even if it is old.

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
| [`docs/PRIVACY.md`](docs/PRIVACY.md) | The allowlist, the four layers that enforce the seal, and every command needed to verify it independently |
| [`docs/PROVISIONING.md`](docs/PROVISIONING.md) | The one-time model pull, re-sealing afterwards, the GPU toolkit, and two assets that degrade a simulation silently if missing |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Component and data-flow diagrams, what each stage owns, and the decisions worth knowing before changing anything |
| [`docs/performance-baseline.json`](docs/performance-baseline.json) | Measured timings for the standard workload, with the hardware they were measured on |
| [`REQ_SPEC.md`](REQ_SPEC.md) | The full specification — every one of the 53 steps carries an account of what was found, including the defects |

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
