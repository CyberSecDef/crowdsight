#!/usr/bin/env bash
# Phase 9 Step 1 verification — the shell, through the real gateway.
#
# Run from the host with the stack up:  ./scripts/verify_frontend.sh
#
# The interesting checks are not "does it render" but the two properties the
# project rests on: every route is served by the SPA rather than 404ing, and
# the shipped bundle reaches nothing off this machine.

set -uo pipefail

BASE="${CROWDSIGHT_BASE:-http://127.0.0.1:8080}"
pass=0
fail=0

check() {
    local label="$1" condition="$2" detail="${3:-}"
    if [ "$condition" = "0" ]; then
        pass=$((pass + 1))
        printf '[PASS] %s%s\n' "$label" "${detail:+ — $detail}"
    else
        fail=$((fail + 1))
        printf '[FAIL] %s%s\n' "$label" "${detail:+ — $detail}"
    fi
}

# --- the app is served, and the placeholder is gone -------------------------
root=$(curl -s "$BASE/")
grep -q 'id="app"' <<<"$root"; check "the app shell is served at /" $?
grep -q '<script type="module"' <<<"$root"; check "the bundle is linked" $?
grep -qi 'placeholder' <<<"$root"; [ $? -ne 0 ]
check "the Phase 9 placeholder is no longer served" $?

# --- history-mode routing ---------------------------------------------------
for route in /runs /graphs/new /simulations/sim-x/run /simulations/sim-x/report; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$route")
    [ "$code" = "200" ]
    check "deep link $route is served by the SPA" $? "HTTP $code"
done

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/assets/does-not-exist.js")
[ "$code" = "404" ]
check "a missing asset is still a 404, not the app" $? "HTTP $code"

# --- the asset the page actually asks for -----------------------------------
asset=$(grep -o '/assets/[A-Za-z0-9._-]*\.js' <<<"$root" | head -1)
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$asset")
[ "$code" = "200" ]
check "the entry bundle loads" $? "$asset -> HTTP $code"

cache=$(curl -sI "$BASE$asset" | grep -i '^cache-control' | tr -d '\r')
grep -qi 'immutable' <<<"$cache"; check "hashed assets are cacheable" $? "$cache"

index_cache=$(curl -sI "$BASE/" | grep -i '^cache-control' | tr -d '\r')
grep -qi 'no-cache' <<<"$index_cache"
check "index.html is not cached" $? "${index_cache:-<none>}"

# --- the API still works through the same origin ----------------------------
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/health/live")
[ "$code" = "200" ]
check "the API is still proxied on the UI origin" $? "HTTP $code"

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/simulation/list")
[ "$code" = "200" ]
check "a real API route answers through the gateway" $? "HTTP $code"

# --- the seal ---------------------------------------------------------------
csp=$(curl -sI "$BASE/" | grep -i '^content-security-policy' | tr -d '\r')
grep -q "default-src 'self'" <<<"$csp"; check "the CSP confines the page to its own origin" $?
grep -qE "https?://" <<<"$csp"; [ $? -ne 0 ]
check "the CSP allowlists no external host" $?

# Nothing in the shipped bundle should name a host that is not ours.
external=$(docker compose exec -T frontend sh -c \
    "grep -rhoE 'https?://[a-zA-Z0-9.-]+' /usr/share/nginx/html --include='*.js' --include='*.html' --include='*.css' 2>/dev/null \
     | grep -vE '://(localhost|127\.0\.0\.1|www\.w3\.org)' | sort -u")
[ -z "$external" ]
check "THE SHIPPED BUNDLE NAMES NO EXTERNAL HOST" $? "${external:-none found}"

# The frontend container must have no way off the machine.
docker compose exec -T frontend timeout 5 wget -q -O /dev/null https://registry.npmjs.org 2>/dev/null
[ $? -ne 0 ]
check "the frontend container cannot reach the internet" $? "npm registry refused at runtime"

networks=$(docker inspect crowdsight-frontend \
    --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}')
grep -q 'crowdsight_sealed' <<<"$networks"
check "the frontend is on the sealed network" $? "$networks"
grep -q 'crowdsight_edge' <<<"$networks"; [ $? -ne 0 ]
check "the frontend is NOT on the edge network" $? "only the gateway is"

# --- the shapes the views actually read -------------------------------------
# A field the UI reads and the API does not send renders as "unknown" or "—".
# That looks broken rather than wrong, and no HTTP-level check catches it, so
# the contract is asserted here field by field.
shapes=$(python3 - "$BASE" <<'PYEOF'
import json, sys, urllib.request

base = sys.argv[1] + "/api"
problems = []


def fetch(path):
    with urllib.request.urlopen(f"{base}{path}", timeout=20) as response:
        return json.load(response)


def expect(where, item, fields):
    missing = [f for f in fields if f not in item]
    if missing:
        problems.append(f"{where} is missing {missing}")


graphs = fetch("/graph/").get("graphs", [])
if graphs:
    expect("graph list entry", graphs[0], ["graph_id", "entity_count", "domain", "filename"])

sims = fetch("/simulation/list?limit=1").get("simulations", [])
if sims:
    expect("simulation list entry", sims[0],
           ["sim_id", "state", "platform", "prepared", "graph_id", "finished_at"])
    # The trap: a simulation has a `state`; only a background task has a `status`.
    if "status" in sims[0]:
        problems.append("simulation list entry now has BOTH state and status")

    run = fetch(f"/simulation/{sims[0]['sim_id']}/run-status")
    expect("run-status", run, ["state", "round", "total_rounds", "percent", "has_data"])

    profiles = fetch(f"/simulation/{sims[0]['sim_id']}/profiles?limit=1")
    expect("profiles envelope", profiles, ["count", "profiles"])
    if profiles.get("profiles"):
        expect("profile", profiles["profiles"][0],
               ["user_id", "name", "username", "occupation", "provenance"])

reports = fetch("/report/?limit=1").get("reports", [])
if reports:
    expect("report list entry", reports[0],
           ["report_id", "sim_id", "summary", "citations_resolved"])

print("; ".join(problems))
PYEOF
)
[ -z "$shapes" ]
check "THE UI READS THE FIELDS THE API ACTUALLY SENDS" $? "${shapes:-every field the views read is present}"

printf '\n%d/%d checks passed\n' "$pass" $((pass + fail))
[ "$fail" -eq 0 ]
