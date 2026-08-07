"""An opt-in load generator. Not part of any suite.

This deliberately does what the rest of the codebase is built to prevent. Phase
2's concurrency gate, `MAX_AGENTS`, `MAX_CONCURRENT_SIMULATIONS` and the
`API_LLM_RESERVE` all exist to stop this machine's GPU being oversubscribed;
every one of them is overridden here, on purpose, to find out what happens at
the edge and above it.

It is therefore gated twice — a `stress` marker that the default `addopts`
deselects, **and** an explicit `CROWDSIGHT_STRESS=1` in the environment. Either
alone would be one accident away from a colleague's laptop locking up.

Five loads run at once, each aimed at a different resource:

* **inference flood** — chat completions far past the concurrency bound (GPU)
* **embedding flood** — large batches through the embedding service (GPU, RAM)
* **simulation fleet** — several full simulations in parallel processes, each
  with a population well past `MAX_AGENTS` (GPU, CPU, RAM)
* **document mill** — parsing, chunking and name normalisation across every
  core (CPU)
* **graph storm** — concurrent Neo4j writes and reads (CPU, RAM, disk)

Run it with `scripts/stress.sh`, which adds host-side GPU sampling; the
container has no `nvidia-smi`.

Knobs, all environment variables:

    CROWDSIGHT_STRESS=1              required, or everything skips
    CROWDSIGHT_STRESS_MINUTES=12     how long the sustained loads run
    CROWDSIGHT_STRESS_SIMS=4         concurrent simulations (max is normally 2)
    CROWDSIGHT_STRESS_AGENTS=40      agents per simulation
    CROWDSIGHT_STRESS_INFERENCE=24   concurrent completions (budget is normally 4)
    CROWDSIGHT_STRESS_WORKERS=0      CPU workers; 0 means one per core
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import random
import shutil
import statistics
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# --------------------------------------------------------------------------
# Opt-in
# --------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


ENABLED = os.environ.get("CROWDSIGHT_STRESS") == "1"
MINUTES = float(os.environ.get("CROWDSIGHT_STRESS_MINUTES") or 12)
SIMS = _int_env("CROWDSIGHT_STRESS_SIMS", 4)
AGENTS = _int_env("CROWDSIGHT_STRESS_AGENTS", 40)
INFERENCE = _int_env("CROWDSIGHT_STRESS_INFERENCE", 24)
CPU_WORKERS = _int_env("CROWDSIGHT_STRESS_WORKERS", 0) or (os.cpu_count() or 8)
#: How long a simulation is given to reach a round boundary once the window
#: closes. Rounds take minutes under full load, so this rarely succeeds; it
#: exists so a run that *is* nearly done gets to finish rather than be killed.
STOP_GRACE = _int_env("CROWDSIGHT_STRESS_STOP_GRACE", 45)

pytestmark = [
    pytest.mark.stress,
    pytest.mark.skipif(
        not ENABLED,
        reason="Set CROWDSIGHT_STRESS=1 to run the load generator. It "
               "deliberately oversubscribes the GPU and will make the machine "
               "unresponsive for the duration.",
    ),
]


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------


@dataclass
class Sample:
    at: float
    cpu_percent: float
    mem_used_gb: float
    mem_percent: float
    processes: int


@dataclass
class Monitor:
    """Watches the machine while the load runs. Host figures, not cgroup ones."""

    interval: float = 0.5
    samples: list[Sample] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        previous = self._cpu_times()
        while not self._stop.wait(self.interval):
            current = self._cpu_times()
            busy = (current[0] - previous[0])
            total = (current[1] - previous[1]) or 1
            previous = current
            used, percent = self._memory()
            self.samples.append(Sample(
                at=time.time(), cpu_percent=100.0 * busy / total,
                mem_used_gb=used, mem_percent=percent,
                processes=self._process_count(),
            ))

    @staticmethod
    def _cpu_times() -> tuple[float, float]:
        with open("/proc/stat", encoding="utf-8") as handle:
            fields = [float(v) for v in handle.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
        return sum(fields) - idle, sum(fields)

    @staticmethod
    def _memory() -> tuple[float, float]:
        values: dict[str, float] = {}
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                values[key] = float(rest.strip().split()[0])
        total = values.get("MemTotal", 1.0)
        available = values.get("MemAvailable", 0.0)
        used = total - available
        return used / 1024 / 1024, 100.0 * used / total

    @staticmethod
    def _process_count() -> int:
        return sum(1 for entry in os.listdir("/proc") if entry.isdigit())

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def report(self, until: float | None = None) -> dict[str, Any]:
        """Statistics over the whole run, or only up to ``until``."""
        samples = [s for s in self.samples
                   if until is None or s.at <= until] or self.samples
        if not samples:
            return {}
        cpu = [s.cpu_percent for s in samples]
        mem = [s.mem_used_gb for s in samples]
        return {
            "duration_s": round(samples[-1].at - samples[0].at, 1),
            "cpu_peak_pct": round(max(cpu), 1),
            "cpu_mean_pct": round(statistics.fmean(cpu), 1),
            "cpu_over_90_pct_of_time": round(
                100.0 * sum(1 for v in cpu if v > 90) / len(cpu), 1),
            "mem_peak_gb": round(max(mem), 2),
            "mem_start_gb": round(mem[0], 2),
            "mem_growth_gb": round(max(mem) - mem[0], 2),
            "mem_peak_pct": round(max(s.mem_percent for s in samples), 1),
            "peak_processes": max(s.processes for s in samples),
            "samples": len(samples),
        }


@dataclass
class Counters:
    """What the load actually achieved, as opposed to what it attempted."""

    completions: int = 0
    completion_errors: int = 0
    embeddings: int = 0
    embedding_errors: int = 0
    documents: int = 0
    chunks: int = 0
    graph_writes: int = 0
    graph_errors: int = 0
    latencies: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, seconds: float) -> None:
        with self.lock:
            self.completions += 1
            self.latencies.append(seconds)

    def report(self) -> dict[str, Any]:
        out = {
            "completions": self.completions,
            "completion_errors": self.completion_errors,
            "embeddings": self.embeddings,
            "embedding_errors": self.embedding_errors,
            "documents_parsed": self.documents,
            "chunks_produced": self.chunks,
            "graph_writes": self.graph_writes,
            "graph_errors": self.graph_errors,
        }
        if self.latencies:
            ordered = sorted(self.latencies)
            out["completion_latency_s"] = {
                "p50": round(ordered[len(ordered) // 2], 2),
                "p95": round(ordered[int(len(ordered) * 0.95)], 2),
                "max": round(ordered[-1], 2),
            }
        return out


# --------------------------------------------------------------------------
# Load: inference
# --------------------------------------------------------------------------

PROMPT = (
    "You are analysing a municipal housing consultation. Summarise the "
    "arguments for and against four-storey development along a transit "
    "corridor, considering traffic, schools, drainage, construction noise, "
    "property values, and the twenty-one day consultation window. Be thorough "
    "and specific; consider at least six distinct stakeholder perspectives and "
    "explain how each would weigh the trade-offs differently."
)


async def inference_flood(config, counters: Counters, deadline: float) -> None:
    """Chat completions far past the concurrency bound. Aimed at the GPU."""
    from app.utils.llm_client import LLMClient
    from app.utils.retry import RetryPolicy

    client = LLMClient(config, retry_policy=RetryPolicy(max_attempts=1))

    async def one(index: int) -> None:
        while time.time() < deadline:
            started = time.monotonic()
            try:
                await client.complete(
                    [{"role": "user",
                      "content": f"{PROMPT}\n\nPerspective {index}."}],
                    temperature=0.9, max_tokens=400,
                )
                counters.record(time.monotonic() - started)
            except Exception:  # noqa: BLE001 - saturation is the point
                with counters.lock:
                    counters.completion_errors += 1
                await asyncio.sleep(0.5)

    try:
        await asyncio.gather(*(one(i) for i in range(INFERENCE)))
    finally:
        await client.aclose()


async def embedding_flood(config, counters: Counters, deadline: float) -> None:
    """Large embedding batches. Aimed at the GPU and at memory."""
    from app.storage.embedding_service import EmbeddingService

    service = EmbeddingService(config)
    round_index = 0
    try:
        while time.time() < deadline:
            round_index += 1
            # Unique text each time, so the cache never absorbs the load.
            texts = [f"stress batch {round_index} item {i}: {PROMPT}"
                     for i in range(256)]
            try:
                vectors = await service.embed_texts(texts)
                with counters.lock:
                    counters.embeddings += len(vectors)
            except Exception:  # noqa: BLE001
                with counters.lock:
                    counters.embedding_errors += 1
                await asyncio.sleep(1.0)
    finally:
        await service.aclose()


# --------------------------------------------------------------------------
# Load: CPU
# --------------------------------------------------------------------------


def _mill_once(seed: int) -> tuple[int, int]:
    """Parse, chunk and normalise a large synthetic document. CPU-bound.

    Module level and picklable: this runs in a process pool, one per core.
    """
    from app.config import Config
    from app.storage.ner_extractor import normalise_name
    from app.utils.chunker import chunk_text
    from app.utils.file_parser import parse_bytes

    rng = random.Random(seed)
    names = ["Councillor Jane Doe", "Mayor Alan Reyes", "the Planning Committee",
             "Riverbend Residents Association", "the Regional Housing Board"]
    paragraphs = []
    for index in range(600):
        who = rng.choice(names)
        paragraphs.append(
            f"Paragraph {index}. {who} addressed the consultation regarding "
            f"four-storey development along the Eastgate corridor. {PROMPT}"
        )
    document = "\n\n".join(paragraphs).encode("utf-8")

    parsed = parse_bytes(document, f"stress-{seed}.txt", Config(
        _env_file=None, NEO4J_PASSWORD="stress"))
    chunks = chunk_text(parsed.text, config=Config(
        _env_file=None, NEO4J_PASSWORD="stress"))
    for chunk in chunks:
        for name in names:
            normalise_name(f"{name} {chunk.text[:40]}")
    return 1, len(chunks)


def document_mill(counters: Counters, deadline: float) -> None:
    """Every core parsing and chunking real documents until time runs out."""
    context = multiprocessing.get_context("spawn")
    with context.Pool(processes=CPU_WORKERS) as pool:
        seed = 0
        pending = []
        while time.time() < deadline:
            while len(pending) < CPU_WORKERS * 2:
                seed += 1
                pending.append(pool.apply_async(_mill_once, (seed,)))
            still: list[Any] = []
            for handle in pending:
                if handle.ready():
                    try:
                        documents, chunks = handle.get(timeout=1)
                        with counters.lock:
                            counters.documents += documents
                            counters.chunks += chunks
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    still.append(handle)
            pending = still
            time.sleep(0.1)
        pool.terminate()


# --------------------------------------------------------------------------
# Load: graph
# --------------------------------------------------------------------------


async def graph_storm(config, counters: Counters, deadline: float,
                      namespace: str) -> None:
    """Concurrent Neo4j writes and reads."""
    from app.storage.neo4j_storage import Neo4jStorage

    storage = Neo4jStorage(config)
    batch = 0

    async def pruner() -> None:
        """Keep the write pressure high without exhausting Neo4j's heap.

        Unbounded, this writes tens of thousands of 768-dimension vectors a
        minute; over a full run that is enough to take the database down,
        which is a different failure from the one being tested. Deleting as
        fast as it writes keeps the load real and the stack alive.
        """
        while time.time() < deadline:
            await asyncio.sleep(20)
            try:
                await storage.write(
                    "MATCH (e:StressEntity {graph_id: $g}) "
                    "WITH e LIMIT 40000 DETACH DELETE e", g=namespace)
            except Exception:  # noqa: BLE001
                pass

    async def writer(worker: int) -> None:
        nonlocal batch
        while time.time() < deadline:
            batch += 1
            rows = [{
                "uuid": f"{namespace}-{worker}-{batch}-{i}",
                "name": f"Stress Entity {worker} {batch} {i}",
                "normalised": f"stress entity {worker} {batch} {i}",
                "embedding": [random.random() for _ in range(768)],
            } for i in range(50)]
            try:
                await storage.write(
                    "UNWIND $rows AS row "
                    "CREATE (e:StressEntity {graph_id: $g, uuid: row.uuid, "
                    "name: row.name, normalised: row.normalised, "
                    "embedding: row.embedding})",
                    rows=rows, g=namespace)
                await storage.read(
                    "MATCH (e:StressEntity {graph_id: $g}) "
                    "RETURN count(e) AS n", g=namespace)
                with counters.lock:
                    counters.graph_writes += len(rows)
            except Exception:  # noqa: BLE001
                with counters.lock:
                    counters.graph_errors += 1
                await asyncio.sleep(0.5)

    try:
        await asyncio.gather(*(writer(w) for w in range(8)), pruner())
    finally:
        try:
            await storage.write(
                "MATCH (e:StressEntity {graph_id: $g}) DETACH DELETE e", g=namespace)
        finally:
            await storage.aclose()


# --------------------------------------------------------------------------
# Load: the simulation fleet
# --------------------------------------------------------------------------


def build_population(count: int):
    """Personas built directly, not generated.

    The point is to stress the *run*, and paying for hundreds of persona
    completions first would spend the whole budget before a single agent acted.
    """
    from app.services.profile_generator import PersonaProfile

    occupations = ["carpenter", "bus driver", "nurse", "teacher", "plumber",
                   "shop assistant", "electrician", "care worker", "chef",
                   "landscaper", "mechanic", "librarian", "paramedic", "welder"]
    leanings = ["opposed to the development", "in favour of more housing",
                "undecided", "concerned about traffic", "concerned about schools"]
    rng = random.Random(count)
    return [
        PersonaProfile(
            name=f"Agent {index:04d} Surname{index}",
            age=rng.randint(19, 82),
            occupation=occupations[index % len(occupations)],
            activity_level="high",
            gender=rng.choice(["female", "male"]),
            country="United Kingdom",
            background=(f"Lives on the Eastgate corridor and works as a "
                        f"{occupations[index % len(occupations)]}."),
            leanings=leanings[index % len(leanings)],
            traits=["forthright"], interests=["housing", "transport"],
            writing_style="plain",
        )
        for index in range(count)
    ]


SCENARIO = {
    "graph_id": "stress",
    "rounds": 6,
    "hours_per_round": 6,
    "event": "Riverbend Council published a draft housing density policy "
             "permitting four-storey development along the Eastgate corridor.",
    "broadcaster": {"name": "Riverbend Wire", "description": "Local news"},
    "seed_posts": [{"content": "Council publishes draft density policy for "
                               "Eastgate. Four-storey development permitted. "
                               "Consultation runs 21 days."}],
}


def simulation_fleet(config, base: Path, deadline: float) -> list[dict[str, Any]]:
    """Several full simulations at once, each larger than `MAX_AGENTS` allows."""
    from app.services.oasis_profiles import write_profiles
    from app.services.simulation_config_generator import SimulationConfig
    from app.services.simulation_manager import SimulationManager
    from app.services.simulation_store import SimulationStore

    # Every guard that exists to protect the GPU, lifted.
    settings = config.model_copy(update={
        "MAX_AGENTS": max(AGENTS * 2, 1000),
        "MAX_CONCURRENT_SIMULATIONS": SIMS,
        "API_LLM_RESERVE": 0,
        "LLM_CONCURRENCY": max(INFERENCE, SIMS * 8),
        "SIMULATION_MEMORY_ROUNDS": 8,
    })
    store = SimulationStore(base / "simulations")
    manager = SimulationManager(store, config=settings)

    people = build_population(AGENTS)
    sim_ids: list[str] = []
    for _ in range(SIMS):
        meta = store.create(SimulationConfig.model_validate(SCENARIO))
        write_profiles(people, store.profiles_dir(meta.sim_id),
                       default_country="United Kingdom")
        sim_ids.append(meta.sim_id)

    for sim_id in sim_ids:
        manager.start(sim_id)

    while time.time() < deadline and manager.running():
        time.sleep(2.0)

    outcomes = []
    for sim_id in sim_ids:
        status = manager.status(sim_id)
        was_running = bool(status.get("running"))
        stop_outcome = None
        if was_running:
            # A round under this much contention takes minutes, and a graceful
            # stop only takes effect at a round boundary, so this will usually
            # escalate to a kill. That is the time box expiring, not the run
            # failing -- but the manager cannot tell the difference and marks
            # a killed worker failed, so record what actually happened.
            stop_outcome = manager.stop(sim_id, timeout=STOP_GRACE)

        state = store.load_meta(sim_id).state
        if not was_running:
            ended = "finished on its own"
        elif stop_outcome == "stopped":
            ended = "stopped cleanly at the time limit"
        else:
            ended = "killed at the time limit, mid-round"

        outcomes.append({
            "sim_id": sim_id,
            "rounds_completed": status.get("round", 0),
            "stage": status.get("stage", "unknown"),
            "ended": ended,
            "stop_outcome": stop_outcome,
            "state": state,
        })
    return outcomes


# --------------------------------------------------------------------------
# The test
# --------------------------------------------------------------------------


def test_peg_the_machine(integration_config, capsys):
    """Run every load at once and report what the machine did.

    Deliberately makes no assertion about throughput. The point is to generate
    load and measure it, not to pass or fail on a number that depends on
    whatever else the box is doing.
    """
    duration = MINUTES * 60
    deadline = time.time() + duration
    base = Path(tempfile.mkdtemp(prefix="cs-stress-", dir="/tmp"))
    namespace = f"stress-{os.getpid()}"
    monitor = Monitor()
    counters = Counters()

    banner = (
        f"\n{'=' * 74}\n"
        f"CrowdSight stress test — {MINUTES:g} minutes\n"
        f"  {SIMS} concurrent simulations x {AGENTS} agents "
        f"(MAX_CONCURRENT_SIMULATIONS and MAX_AGENTS overridden)\n"
        f"  {INFERENCE} concurrent completions "
        f"(LLM_CONCURRENCY is normally {integration_config.LLM_CONCURRENCY})\n"
        f"  {CPU_WORKERS} CPU workers, 8 graph writers, 256-text embedding batches\n"
        f"{'=' * 74}\n"
    )
    print(banner, flush=True)

    fleet: list[dict[str, Any]] = []
    errors: list[str] = []

    def run_fleet() -> None:
        try:
            fleet.extend(simulation_fleet(integration_config, base, deadline))
        except Exception as exc:  # noqa: BLE001 - one load must not stop the rest
            errors.append(f"simulation fleet: {type(exc).__name__}: {exc}")

    def run_mill() -> None:
        try:
            document_mill(counters, deadline)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"document mill: {type(exc).__name__}: {exc}")

    async def run_async_loads() -> None:
        await asyncio.gather(
            inference_flood(integration_config, counters, deadline),
            embedding_flood(integration_config, counters, deadline),
            graph_storm(integration_config, counters, deadline, namespace),
            return_exceptions=True,
        )

    def run_async() -> None:
        try:
            asyncio.run(run_async_loads())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"async loads: {type(exc).__name__}: {exc}")

    monitor.start()
    threads = [
        threading.Thread(target=run_fleet, name="fleet"),
        threading.Thread(target=run_mill, name="mill"),
        threading.Thread(target=run_async, name="async"),
    ]
    started = time.time()
    try:
        for thread in threads:
            thread.start()

        # Progress while it runs, so a watcher sees it is alive.
        while any(t.is_alive() for t in threads) and time.time() < deadline + 120:
            time.sleep(15)
            if monitor.samples:
                latest = monitor.samples[-1]
                print(f"  [{int(time.time() - started):4d}s] "
                      f"cpu {latest.cpu_percent:5.1f}%  "
                      f"mem {latest.mem_used_gb:5.1f} GB "
                      f"({latest.mem_percent:4.1f}%)  "
                      f"procs {latest.processes:4d}  "
                      f"completions {counters.completions:5d}  "
                      f"embeddings {counters.embeddings:6d}  "
                      f"chunks {counters.chunks:6d}", flush=True)

        for thread in threads:
            thread.join(timeout=180)
    finally:
        monitor.stop()
        shutil.rmtree(base, ignore_errors=True)

    report = {
        "configuration": {
            "minutes": MINUTES, "simulations": SIMS, "agents_each": AGENTS,
            "concurrent_completions": INFERENCE, "cpu_workers": CPU_WORKERS,
        },
        "timing": {
            "load_window_s": round(duration, 1),
            "total_s": round(time.time() - started, 1),
            "note": ("The CPU-bound loads stop when the window closes; the "
                     "simulations run on until they finish a round or are "
                     "stopped, so the total exceeds the window and the CPU "
                     "mean is diluted by the tail."),
        },
        "machine_during_load_window": monitor.report(until=started + duration),
        "machine": monitor.report(),
        "work": counters.report(),
        "simulations": fleet,
        "errors": errors,
    }

    print(f"\n{'=' * 74}\nSTRESS REPORT\n{'=' * 74}", flush=True)
    print(json.dumps(report, indent=2), flush=True)

    destination = Path(os.environ.get("CROWDSIGHT_STRESS_REPORT")
                       or "/app/data/stress-report.json")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written to {destination}", flush=True)
    except OSError as exc:
        print(f"\nCould not write the report: {exc}", flush=True)

    # The only real failure is generating no load at all.
    assert monitor.samples, "the monitor never sampled anything"
    assert counters.completions or counters.chunks or counters.graph_writes, (
        "no load was generated at all — check that Ollama and Neo4j are up")
