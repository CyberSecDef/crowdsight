"""Phase 10 Step 3 — the performance baseline.

Runs the standard workload against the live stack, records wall-clock timings
per stage, and reports the change against the stored baseline.

    python scripts/benchmark.py                 # run and compare
    python scripts/benchmark.py --save          # run and adopt as the baseline
    python scripts/benchmark.py --agents 20 --rounds 5

**It reports drift rather than passing or failing.** Wall-clock on a shared GPU
varies by a lot — the same three-agent population has taken anywhere from 4 to
32 seconds a round on this machine depending on what else was running. A
threshold loose enough not to cry wolf would not catch a real regression, and a
tight one goes red for reasons that have nothing to do with the change. A
number you read beats a gate you learn to ignore.

**The largest single lever is not in this script.** A lone run gets
``(LLM_CONCURRENCY - API_LLM_RESERVE) // MAX_CONCURRENT_SIMULATIONS`` concurrent
requests — with the defaults that is 1, so one simulation uses a quarter of the
configured budget while the rest sits idle. That is deliberate (a worker's share
never changes underneath it), and it is the first thing to look at if a run is
slower than this baseline says it should be.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / "docs" / "performance-baseline.json"

BASE = "http://127.0.0.1:8080/api"

#: The spec's standard workload.
DEFAULT_AGENTS = 50
DEFAULT_ROUNDS = 10

DOCUMENT = REPO / "backend" / "tests" / "fixtures"


def call(path: str, payload=None, timeout: float = 600.0):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_for(check, *, timeout: float, what: str, interval: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done, value = check()
        if done:
            return value
        time.sleep(interval)
    raise SystemExit(f"timed out after {timeout:.0f}s waiting for {what}")


def hardware() -> dict:
    """What the numbers are numbers *of*. A baseline without this is folklore."""
    info: dict[str, object] = {}
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        info["gpu"] = gpu
    except Exception:
        info["gpu"] = "unknown"
    try:
        info["cpu"] = subprocess.run(
            ["sh", "-c", "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2-"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        info["threads"] = subprocess.run(
            ["nproc"], capture_output=True, text=True, timeout=10).stdout.strip()
        info["ram_gb"] = round(int(subprocess.run(
            ["sh", "-c", "grep MemTotal /proc/meminfo | tr -dc 0-9"],
            capture_output=True, text=True, timeout=10).stdout) / 1_048_576, 1)
    except Exception:
        pass

    _, budget = call("/simulation/budget")
    info["budget"] = budget
    return info


def benchmark(agents: int, rounds: int) -> dict:
    timings: dict[str, float] = {}

    # ---- a graph to build the population from -----------------------------
    status, graphs = call("/graph/")
    existing = graphs.get("graphs") or []
    if not existing:
        raise SystemExit("no graph on disk; upload a document first")
    graph_id = existing[0]["graph_id"]
    print(f"  graph        {graph_id}", flush=True)

    # ---- prepare: personas are generated one per agent --------------------
    started = time.monotonic()
    status, created = call("/simulation/create", {
        "graph_id": graph_id, "platform": "twitter",
        "rounds": rounds, "total_agents": agents})
    if status != 201:
        raise SystemExit(f"create failed: {created}")
    sim_id = created["sim_id"]

    status, prepared = call("/simulation/prepare",
                            {"sim_id": sim_id, "total_agents": agents})
    if status != 202:
        raise SystemExit(f"prepare failed: {prepared}")
    task_id = prepared["task_id"]

    def prepared_yet():
        _, task = call(f"/simulation/prepare/status?task_id={task_id}")
        if task.get("status") in {"awaiting_review", "succeeded"}:
            return True, task
        if task.get("status") == "failed":
            raise SystemExit(f"prepare failed: {task.get('error')}")
        return False, task

    wait_for(prepared_yet, timeout=3600, what="the population")
    timings["prepare_seconds"] = time.monotonic() - started
    print(f"  prepare      {timings['prepare_seconds']:.0f}s "
          f"({agents} personas)", flush=True)

    # ---- the run ----------------------------------------------------------
    started = time.monotonic()
    status, launched = call("/simulation/start", {"sim_id": sim_id})
    if status != 202:
        raise SystemExit(f"start failed: {launched}")

    last_round = -1

    def finished():
        nonlocal last_round
        _, run = call(f"/simulation/{sim_id}/run-status")
        if run.get("rounds_completed", 0) != last_round:
            last_round = run["rounds_completed"]
            print(f"    round {last_round}/{rounds} "
                  f"at {time.monotonic() - started:.0f}s", flush=True)
        return run.get("state") in {"complete", "failed"}, run

    run = wait_for(finished, timeout=14400, what="the run", interval=10)
    timings["run_seconds"] = time.monotonic() - started
    if run.get("state") != "complete":
        raise SystemExit(f"the run did not complete: {run.get('state')}")
    print(f"  run          {timings['run_seconds']:.0f}s "
          f"({rounds} rounds)", flush=True)

    # ---- the report -------------------------------------------------------
    started = time.monotonic()
    status, generating = call("/report/generate", {"sim_id": sim_id})
    if status != 202:
        raise SystemExit(f"report failed: {generating}")
    report_task = generating["task_id"]

    def reported():
        _, task = call(f"/report/status/{report_task}")
        if task.get("status") == "succeeded":
            return True, task
        if task.get("status") == "failed":
            raise SystemExit(f"report failed: {task.get('error')}")
        return False, task

    wait_for(reported, timeout=3600, what="the report")
    timings["report_seconds"] = time.monotonic() - started
    print(f"  report       {timings['report_seconds']:.0f}s", flush=True)

    timings["total_seconds"] = sum(timings.values())

    # ---- per-round detail, from the run's own record -----------------------
    _, timeline = call(f"/simulation/{sim_id}/timeline")
    per_round = []
    previous = None
    for entry in timeline.get("rounds", []):
        ended = entry.get("ended_at")
        if ended and previous:
            per_round.append(round(
                (datetime.fromisoformat(ended)
                 - datetime.fromisoformat(previous)).total_seconds(), 1))
        previous = ended or previous

    _, actions = call(f"/simulation/{sim_id}/actions?limit=1")

    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workload": {"agents": agents, "rounds": rounds},
        "sim_id": sim_id,
        "timings": {k: round(v, 1) for k, v in timings.items()},
        "seconds_per_round": per_round,
        "actions_recorded": actions.get("total", 0),
        "hardware": hardware(),
    }


def compare(current: dict, baseline: dict) -> None:
    print("\nAgainst the stored baseline "
          f"({baseline.get('recorded_at', 'unknown')}):", flush=True)
    if baseline.get("workload") != current.get("workload"):
        print(f"  different workload ({baseline.get('workload')} vs "
              f"{current.get('workload')}) — not comparable")
        return
    for key, value in current["timings"].items():
        was = baseline["timings"].get(key)
        if not was:
            continue
        change = (value - was) / was * 100
        arrow = "slower" if change > 0 else "faster"
        print(f"  {key:18} {was:>8.0f}s -> {value:>8.0f}s  "
              f"{abs(change):5.1f}% {arrow}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=DEFAULT_AGENTS)
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--save", action="store_true",
                        help="adopt this result as the stored baseline")
    args = parser.parse_args()

    print(f"Standard workload: {args.agents} agents, {args.rounds} rounds\n"
          f"This is a real run and takes a long time. Nothing else should be "
          f"using the GPU.\n", flush=True)

    result = benchmark(args.agents, args.rounds)
    print(f"\n  TOTAL        {result['timings']['total_seconds']:.0f}s "
          f"({result['timings']['total_seconds'] / 60:.1f} min)", flush=True)

    if BASELINE.is_file():
        compare(result, json.loads(BASELINE.read_text()))

    if args.save:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(result, indent=2) + "\n")
        print(f"\nBaseline written to {BASELINE.relative_to(REPO)}")
    else:
        print(f"\nNot saved. Re-run with --save to adopt this as the baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
