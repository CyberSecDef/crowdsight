#!/usr/bin/env bash
# Phase 10 Step 4 — back up everything a rebuild cannot recreate.
#
#   ./scripts/backup.sh [destination]      # default: ./backups
#
# Two stores, backed up differently because they behave differently.
#
# `data/` is ordinary files — documents, simulation databases, profiles,
# reports, the task store. It is copied while everything runs, which is safe
# for finished runs and *not* safe for a run in progress, so this refuses to
# start while one is going.
#
# Neo4j is the awkward one. Community edition has no online backup:
# `neo4j-admin database dump` refuses outright while the database is in use,
# and copying the volume underneath a running database can produce a file that
# looks fine and will not restore. So the graph store is taken offline for the
# length of the dump — usually well under a minute — and this says so before it
# does it. A backup you cannot restore is worse than none, because you stop
# worrying about it.

set -euo pipefail

DESTINATION="${1:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${DESTINATION}/crowdsight-${STAMP}"

cd "$(dirname "$0")/.."

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker is not on PATH"

# --- refuse to back up underneath a live run --------------------------------
# A simulation database copied mid-round is a half-written round, and the
# round bookkeeping that resume depends on would be the part that is wrong.
if running=$(curl -fsS --max-time 5 http://127.0.0.1:5000/api/simulation/list 2>/dev/null \
        | python3 -c 'import json,sys; print(",".join(s["sim_id"] for s in json.load(sys.stdin)["simulations"] if s["state"]=="running"))' 2>/dev/null); then
    [ -z "$running" ] || die "simulation(s) running: ${running}. Stop them first — a database copied mid-round backs up a half-written round."
else
    say "note: the API did not answer, so a running simulation could not be ruled out."
fi

mkdir -p "$TARGET"
say "Backing up to ${TARGET}"

# --- data/ ------------------------------------------------------------------
if [ -d data ]; then
    say "  data/            ($(du -sh data | cut -f1))"
    tar -czf "${TARGET}/data.tar.gz" data
else
    say "  data/            (absent, skipping)"
fi

# --- configuration ----------------------------------------------------------
# .env holds NEO4J_PASSWORD, without which the dump below cannot be restored
# into a working stack.
for file in .env docker-compose.yml; do
    [ -f "$file" ] && cp "$file" "${TARGET}/$(basename "$file")"
done
say "  configuration    (.env, docker-compose.yml)"

# --- Neo4j: stop, dump, restart ---------------------------------------------
if docker compose ps --status running --services 2>/dev/null | grep -qx neo4j; then
    say "  neo4j            stopping for a consistent dump (Community has no online backup)"
    docker compose stop neo4j >/dev/null
    RESTART_NEO4J=1
else
    say "  neo4j            already stopped"
    RESTART_NEO4J=0
fi

restore_neo4j() {
    if [ "${RESTART_NEO4J:-0}" = "1" ]; then
        say "  neo4j            restarting"
        docker compose start neo4j >/dev/null || true
    fi
}
# However this exits — success, failure, or Ctrl-C — the database comes back.
trap restore_neo4j EXIT

docker compose run --rm --no-deps \
    -v "$(cd "${TARGET}" && pwd):/backup" \
    --entrypoint neo4j-admin neo4j \
    database dump neo4j --to-path=/backup --overwrite-destination=true \
    >/dev/null 2>&1 || die "the Neo4j dump failed; nothing was left half-written"

say "  neo4j            dumped"

# --- what this is and how to put it back ------------------------------------
cat > "${TARGET}/RESTORE.md" <<EOF
# CrowdSight backup — ${STAMP}

Contents:

* \`data.tar.gz\` — documents, simulation databases, profiles, reports, tasks
* \`neo4j.dump\` — the knowledge graph, dumped offline so it will actually restore
* \`.env\`, \`docker-compose.yml\` — the configuration, including NEO4J_PASSWORD

## Restoring

\`\`\`bash
docker compose down
tar -xzf data.tar.gz -C /path/to/crowdsight
cp .env docker-compose.yml /path/to/crowdsight/

# The graph, into a stopped database:
docker compose run --rm --no-deps -v "\$PWD:/backup" \\
    --entrypoint neo4j-admin neo4j \\
    database load neo4j --from-path=/backup --overwrite-destination=true

docker compose up -d
\`\`\`

The models are **not** here: they are large, unchanging, and pulling them is the
one-time provisioning step. See README.md.
EOF

say ""
say "Done: ${TARGET} ($(du -sh "${TARGET}" | cut -f1))"
say "Restore instructions are in ${TARGET}/RESTORE.md"
