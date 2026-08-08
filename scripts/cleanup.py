"""Phase 10 Step 4 — remove old simulation databases, safely.

    python scripts/cleanup.py                      # show what would go
    python scripts/cleanup.py --older-than 30
    python scripts/cleanup.py --older-than 30 --delete

**Dry run by default.** Deleting a run is not recoverable from anything else in
the project: the database holds every post, action and interview that run
produced, and none of it can be regenerated because the model does not answer
the same way twice.

**A run that has been reported on is never deleted.** Every claim in a report
cites post ids in that database, so removing it turns each citation in a
published document into a dead link — and the report survives to be read,
saying nothing about the evidence having gone. That is the single worst thing
this script could do, so it will not, and it says why each survivor stayed.

Also protected: anything running, and anything still inside its interview
window, where the agents remain answerable.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SIMULATIONS = REPO / "data" / "simulations"
REPORTS = REPO / "data" / "reports"

API = "http://127.0.0.1:5000/api"

FINISHED = {"complete", "failed"}


def api(path: str):
    try:
        with urllib.request.urlopen(f"{API}{path}", timeout=5) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def reported_simulations() -> set[str]:
    """Simulations something has been published about.

    Read from disk rather than the API so the protection still holds when the
    stack is down — which is exactly when someone is most likely to be tidying
    up.
    """
    reported: set[str] = set()
    if not REPORTS.is_dir():
        return reported
    for entry in REPORTS.iterdir():
        report = entry / "report.json"
        if not report.is_file():
            continue
        try:
            reported.add(json.loads(report.read_text()).get("sim_id", ""))
        except (OSError, ValueError):
            continue
    return reported - {""}


def size_of(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def survey(older_than_days: int) -> tuple[list, list]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    reported = reported_simulations()

    live = api("/simulation/list?limit=200") or {}
    by_id = {s["sim_id"]: s for s in live.get("simulations", [])}
    if not live:
        print("note: the API did not answer, so running simulations cannot be "
              "ruled out by state. Directories modified recently are kept.\n")

    removable, kept = [], []
    for directory in sorted(SIMULATIONS.iterdir()) if SIMULATIONS.is_dir() else []:
        if not directory.is_dir():
            continue
        sim_id = directory.name
        meta = by_id.get(sim_id, {})
        modified = datetime.fromtimestamp(directory.stat().st_mtime, timezone.utc)
        entry = (sim_id, size_of(directory), modified)

        if sim_id in reported:
            kept.append((*entry, "a report cites it"))
        elif meta.get("state") == "running" or meta.get("running"):
            kept.append((*entry, "running"))
        elif meta and meta.get("state") not in FINISHED:
            kept.append((*entry, f"state is {meta.get('state')}"))
        elif not meta and modified > cutoff:
            # No API answer and recently touched: could be live.
            kept.append((*entry, "recently modified, and its state is unknown"))
        elif modified > cutoff:
            kept.append((*entry, f"newer than {older_than_days} days"))
        else:
            removable.append(entry)
    return removable, kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--older-than", type=int, default=30,
                        metavar="DAYS", help="delete finished runs older than this")
    parser.add_argument("--delete", action="store_true",
                        help="actually delete; without it nothing is removed")
    args = parser.parse_args()

    if not SIMULATIONS.is_dir():
        print(f"No simulations directory at {SIMULATIONS}")
        return 0

    removable, kept = survey(args.older_than)

    if kept:
        print(f"Keeping {len(kept)}:")
        for sim_id, size, modified, why in kept:
            print(f"  {sim_id}  {human(size):>9}  {modified:%Y-%m-%d}  — {why}")
        print()

    if not removable:
        print(f"Nothing older than {args.older_than} days is safe to remove.")
        return 0

    total = sum(size for _, size, _ in removable)
    print(f"{'Would remove' if not args.delete else 'Removing'} "
          f"{len(removable)}, freeing {human(total)}:")
    for sim_id, size, modified in removable:
        print(f"  {sim_id}  {human(size):>9}  {modified:%Y-%m-%d}")

    if not args.delete:
        print("\nDry run. Re-run with --delete to remove them.")
        return 0

    for sim_id, _, _ in removable:
        shutil.rmtree(SIMULATIONS / sim_id, ignore_errors=True)
    print(f"\nRemoved {len(removable)} simulation(s), freeing {human(total)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
