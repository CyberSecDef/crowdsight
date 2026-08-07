#!/usr/bin/env bash
#
# CrowdSight load generator. Opt-in, and deliberately punishing.
#
# Runs every load the system can generate at once — inference far past the
# concurrency bound, large embedding batches, several full simulations with
# populations beyond MAX_AGENTS, document parsing across every core, and
# concurrent graph writes — while sampling CPU and memory inside the container
# and the GPU out here, because the backend image has no nvidia-smi.
#
# This will make the machine unresponsive while it runs. That is the point.
#
#   scripts/stress.sh                 # 12 minutes, the default shape
#   MINUTES=3 scripts/stress.sh       # a short, sharp burst
#   SIMS=6 AGENTS=80 scripts/stress.sh
#
set -uo pipefail

cd "$(dirname "$0")/.."

MINUTES="${MINUTES:-12}"
SIMS="${SIMS:-4}"
AGENTS="${AGENTS:-40}"
INFERENCE="${INFERENCE:-24}"
WORKERS="${WORKERS:-0}"
GPU_LOG="${GPU_LOG:-data/stress-gpu.csv}"
REPORT="${REPORT:-data/stress-report.json}"

echo "==========================================================================="
echo " CrowdSight stress test"
echo "---------------------------------------------------------------------------"
echo "  duration            ${MINUTES} minutes"
echo "  simulations         ${SIMS} concurrent x ${AGENTS} agents"
echo "  completions         ${INFERENCE} concurrent"
echo "  cpu workers         ${WORKERS:-one per core}"
echo ""
echo "  This oversubscribes the GPU on purpose and overrides MAX_AGENTS,"
echo "  MAX_CONCURRENT_SIMULATIONS and the LLM_CONCURRENCY budget."
echo "  The machine will be slow to unresponsive until it finishes."
echo "==========================================================================="
echo ""

if [ "${ASSUME_YES:-}" != "1" ]; then
    read -r -p "Start? [y/N] " reply
    case "$reply" in
        [yY]|[yY][eE][sS]) ;;
        *) echo "Cancelled."; exit 0 ;;
    esac
fi

echo "Bringing the stack up..."
docker compose up -d >/dev/null 2>&1
# Ollama loads a 14b model on first request; give it a moment to be ready.
until docker compose exec -T ollama sh -c 'exit 0' >/dev/null 2>&1; do sleep 1; done

mkdir -p "$(dirname "$GPU_LOG")"

# ---- GPU sampling, from wherever nvidia-smi actually lives ----------------
GPU_SAMPLER=""
if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_SAMPLER="host"
elif docker compose exec -T ollama sh -c 'command -v nvidia-smi' >/dev/null 2>&1; then
    GPU_SAMPLER="ollama"
fi

gpu_sample() {
    case "$GPU_SAMPLER" in
        host)   nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw \
                    --format=csv,noheader,nounits 2>/dev/null ;;
        ollama) docker compose exec -T ollama nvidia-smi \
                    --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw \
                    --format=csv,noheader,nounits 2>/dev/null ;;
    esac
}

GPU_PID=""
if [ -n "$GPU_SAMPLER" ]; then
    echo "GPU sampling via ${GPU_SAMPLER}, logging to ${GPU_LOG}"
    echo "timestamp,util_pct,mem_used_mib,mem_total_mib,temp_c,power_w" > "$GPU_LOG"
    (
        while true; do
            line="$(gpu_sample | head -1 | tr -d ' ')"
            [ -n "$line" ] && echo "$(date +%s),${line}" >> "$GPU_LOG"
            sleep 1
        done
    ) &
    GPU_PID=$!
else
    echo "No nvidia-smi found on the host or in the ollama container;"
    echo "GPU will not be sampled. CPU and memory still will be."
fi

cleanup() {
    [ -n "$GPU_PID" ] && kill "$GPU_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

echo ""
echo "Running. Watch your monitors."
echo ""

START=$(date +%s)
docker compose exec -T \
    -e CROWDSIGHT_STRESS=1 \
    -e CROWDSIGHT_STRESS_MINUTES="$MINUTES" \
    -e CROWDSIGHT_STRESS_SIMS="$SIMS" \
    -e CROWDSIGHT_STRESS_AGENTS="$AGENTS" \
    -e CROWDSIGHT_STRESS_INFERENCE="$INFERENCE" \
    -e CROWDSIGHT_STRESS_WORKERS="$WORKERS" \
    -e CROWDSIGHT_STRESS_REPORT="/app/${REPORT}" \
    backend python -m pytest -m stress tests/test_stress.py -s -q
STATUS=$?
END=$(date +%s)

cleanup
trap - EXIT INT TERM

echo ""
echo "==========================================================================="
echo " Finished in $(( END - START ))s (pytest exit ${STATUS})"
echo "==========================================================================="

if [ -n "$GPU_SAMPLER" ] && [ -s "$GPU_LOG" ]; then
    awk -F, 'NR>1 && NF>=5 {
        n++;
        u+=$2; if ($2>maxu) maxu=$2;
        if ($3>maxm) maxm=$3; total=$4;
        if ($5>maxt) maxt=$5;
        p+=$6; if ($6>maxp) maxp=$6;
        if ($2>=95) pegged++;
    } END {
        if (n) {
            printf "\nGPU over %d samples:\n", n;
            printf "  utilisation   peak %d%%   mean %.1f%%   at/above 95%% for %.1f%% of the run\n",
                   maxu, u/n, 100*pegged/n;
            printf "  memory        peak %d MiB of %d MiB (%.1f%%)\n",
                   maxm, total, (total ? 100*maxm/total : 0);
            printf "  temperature   peak %d C\n", maxt;
            if (p > 0) printf "  power         peak %.1f W   mean %.1f W\n", maxp, p/n;
            printf "\n  Full samples: %s\n", "'"$GPU_LOG"'";
        }
    }' "$GPU_LOG"
fi

if [ -f "$REPORT" ]; then
    echo ""
    echo "Machine and workload report: ${REPORT}"
fi

exit $STATUS
