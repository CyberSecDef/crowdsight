# Privacy and the seal

CrowdSight's central claim is that **nothing leaves your machine**. This document says
exactly what that means, how it is enforced, and — most importantly — how to check it
yourself rather than take this document's word for it.

---

## What the claim actually is

Every inference call runs against a local Ollama instance. All graph memory lives in a
local Neo4j instance. All simulation state lives in local SQLite files. There are no
cloud services, no external memory providers, and no telemetry.

The claim is **structural, not behavioural**. It does not rest on the code choosing not
to make a request. The container that holds every document, prompt and simulation has no
route off the host, so a request to somewhere else fails whether or not anyone meant to
make it — including from a dependency nobody audited.

**One container is deliberately outside the seal.** A stateless nginx gateway publishes
the ports and reverse-proxies inward. It exists because a container on an
`internal: true` network cannot be reached *from the host* either — reachability and
egress are the same property — so the alternative would be giving the backend itself a
route out. The gateway holds no application code, no credentials and no data.

---

## The complete allowlist

| Purpose | Endpoint | Protocol |
|---|---|---|
| LLM chat completions | `http://ollama:11434/v1` | OpenAI-compatible HTTP |
| Text embeddings | `http://ollama:11434/api/embed` | Ollama native HTTP |
| Knowledge graph | `bolt://neo4j:7687` | Bolt |
| Simulation state | `./data/simulations/` | local filesystem |
| Backend API | `http://localhost:5000` | HTTP (loopback/LAN) |
| Frontend | `http://localhost:8080` | HTTP |

Anything else is a defect.

Service names are the preferred form; loopback (`localhost`, `127.0.0.1`, `::1`) is
equally good outside Compose. A **private LAN address** — RFC 1918, link-local or
unique-local — is permitted where genuinely necessary, such as Ollama on a separate GPU
box. It is second-best, and CrowdSight says so out loud: every such endpoint logs a
warning at startup, because traffic to another machine still leaves this host and the
container-level seal cannot cover it. Public addresses and public hostnames are refused
outright — the process will not start.

There is no third-party API key anywhere in this system. `LLM_API_KEY` exists only
because the OpenAI SDK requires a non-empty string; it defaults to the literal `ollama`,
which Ollama ignores.

---

## How the seal is enforced

Four layers, each of which would have to fail independently.

**1. The network has no route out.** The Compose network is declared `internal: true`,
which removes the default gateway. There is nothing to route through.

```yaml
networks:
  sealed:
    driver: bridge
    internal: true
```

**2. The edge network has no NAT.** The gateway sits on a second network with masquerade
disabled, so its outbound packets have no return path.

**3. Configuration refuses an external endpoint.** Validation resolves every configured
URL and classifies the host. Public means the process does not start. This catches the
case where someone points `LLM_BASE_URL` at a cloud provider on a machine that *does*
have a network.

**4. No source file names a host we did not choose.** `backend/app` and `frontend/src`
are audited for URL literals. Today they name exactly one: `http://ollama`.

### One residual channel, stated rather than hidden

Docker's embedded DNS resolver still answers external name lookups on the **non-internal**
`edge` network, so the gateway can *resolve* names even though it cannot reach them. The
gateway holds no data and runs no application code, so there is nothing to exfiltrate
through a DNS query — but it is a real asymmetry and it is documented here and in
`docker-compose.yml` rather than papered over. The absolute guarantee lives on the sealed
network, where nothing resolves and nothing routes.

---

## Verifying it yourself

Do not trust this document. Every claim above is checkable in under a minute.

### The quickest check

```bash
docker compose exec backend python -m app.egress_check
```

It attempts real connections to real external hosts and reports each one. It exits
non-zero if any succeeds.

### Is the network actually internal?

```bash
docker network inspect crowdsight_sealed --format '{{.Internal}}'   # true
docker compose exec backend ip route                                # no default route
docker inspect crowdsight-backend --format '{{json .NetworkSettings.Ports}}'  # {} 
```

### Try it by hand

```bash
docker compose exec backend sh -c 'wget -T 5 -qO- https://api.openai.com || echo REFUSED'
docker compose exec backend python -c "import socket; socket.gethostbyname('huggingface.co')"
```

Both fail. The second fails at DNS: on the sealed network there is no resolver that
answers external names.

### The full compliance gate

Two test files, both marked `egress` so the default suite cannot skip them. Run from the
host, where the source tree and the Docker daemon are both visible:

```bash
pytest backend/tests/test_network_isolation.py backend/tests/test_egress_verification.py
```

| File | What it proves |
|---|---|
| `test_network_isolation.py` | The running network has no route off-host: no outbound TCP, no external DNS, no default route, no published ports on the backend, and the sealed network really is `internal` |
| `test_egress_verification.py` | What survives a restart: no runtime source file names a host outside the allowlist, every npm dependency resolves from `registry.npmjs.org`, configuration refuses a public endpoint *for that reason*, and the frontend container is confined exactly as the backend is |

**The seal proof never skips.** A test that quietly passes by skipping itself when it
cannot verify the seal is worse than no test at all: it produces a green run that means
nothing. With the stack down, the suite goes red rather than green.

### The shipped frontend

```bash
./scripts/verify_frontend.sh
```

Among other things it asserts that the compiled bundle names no external host and that
the frontend container is refused when it reaches for the npm registry it was built from.

---

## What is deliberately *not* claimed

**Traffic capture is not part of the gate.** Capturing packets inside the container would
mean installing tcpdump and granting `NET_RAW` to the very container the project exists to
confine — weakening the thing being tested in order to test it. The evidence already
available is stronger: there is no route for traffic to be captured *on*, and the gate
attempts real connections to real external hosts and requires them to fail.

**Build time is not run time.** Building the images needs the internet: base images from
Docker Hub, Python packages from PyPI, npm packages from the registry. That is the same
category as pulling the model weights, it happens under your control, and the running
stack has no network at all. Every dependency is pinned in a lockfile, and the gate checks
that they all resolve from the registry they claim to.

**A private LAN endpoint is your call, and the seal cannot cover it.** If you point Ollama
at another box, traffic leaves this host. CrowdSight permits it, warns at startup, and
surfaces the warning in `GET /api/health`.

---

## Where the data lives

| What | Where | Leaves the machine |
|---|---|---|
| Uploaded documents | `data/uploads/`, `data/graphs/` | No |
| Knowledge graph | Neo4j volume `crowdsight_neo4j_data` | No |
| Agent personas | `data/simulations/<sim_id>/profiles/` | No |
| Simulation results | `data/simulations/<sim_id>/simulation.db` | No |
| Reports | `data/reports/` | No |
| Model weights | Ollama volume `crowdsight_ollama_models` | No |

Nothing is sent anywhere for analysis, indexing, telemetry or improvement. There is no
code that could: see the source audit above.

See also [`PROVISIONING.md`](PROVISIONING.md) for the one time the network is needed, and
[`ARCHITECTURE.md`](ARCHITECTURE.md) for how the pieces fit together.
