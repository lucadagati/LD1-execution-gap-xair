#!/usr/bin/env bash
# Run the evaluation on the distributed testbed (docker/distributed/compose.yml).
# Usage: sudo ./scripts/run_distributed.sh [one-way-delay] [jitter]   (default 0.5ms 0.1ms)
#   SUITES="e10_atomic e16"   run only these suites (default: all)
#   OUT_SUB=distributed/campaigns/c2   output sub-directory of experiments/results (default distributed)
#   SEED_OFFSET=1             added to every suite seed, for independent campaigns
#   COMPOSE_EXTRA=file.yml    extra compose file (e.g. compose.shared-cpu.yml)
#   TRACE_PHASE=fixed         E16-trace submission phase (sensitivity check)
# Requires docker, root (nsenter + tc), and the xair-node image
# (docker build -t xair-node -f docker/xair-node/Dockerfile .).
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
DELAY="${1:-0.5ms}"; JITTER="${2:-0.1ms}"
SUITES="${SUITES:-${ONLY:-}}"
OUT_SUB="${OUT_SUB:-distributed}"
S="${SEED_OFFSET:-0}"
TRACE_PHASE="${TRACE_PHASE:-random}"
COMPOSE=(docker compose -f "$REPO_ROOT/docker/distributed/compose.yml")
[ -z "${COMPOSE_EXTRA:-}" ] || COMPOSE+=(-f "$REPO_ROOT/docker/distributed/$COMPOSE_EXTRA")
OUT="$REPO_ROOT/experiments/results/$OUT_SUB"
RES="/repo/experiments/results/$OUT_SUB"
docker image inspect xair-node >/dev/null 2>&1 || docker build -q -t xair-node -f "$REPO_ROOT/docker/xair-node/Dockerfile" "$REPO_ROOT"

cleanup() { "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT
"${COMPOSE[@]}" up -d --wait redis xair gateway bench >/dev/null
for svc in redis xair gateway bench; do
  pid="$(docker inspect -f '{{.State.Pid}}' "xair-dist-$svc-1")"
  nsenter -t "$pid" -n tc qdisc replace dev eth0 root netem delay "$DELAY" "$JITTER" distribution normal
done
mkdir -p "$OUT"
BENCH=("${COMPOSE[@]}" exec -T -e XAIR_RESULTS_DIR="$RES" bench python)
ENV_OUT="$OUT/environment.txt"
"${BENCH[@]}" - > "$ENV_OUT" <<PY
import json, socket, statistics, time, urllib.request
def rtt(host, port, n=200):
    s = []
    for _ in range(n):
        t = time.perf_counter(); c = socket.create_connection((host, port), timeout=2); s.append((time.perf_counter() - t) * 1000); c.close()
    return round(statistics.median(s), 3), round(sorted(s)[int(0.99 * n) - 1], 3)
def shared_clock(n=50):
    # the store-side read bounds XAIR reports must fall inside this process's request interval
    ok = 0
    for _ in range(n):
        t0 = time.perf_counter() * 1000
        r = json.loads(urllib.request.urlopen("http://xair:8080/v1/context/snapshot", timeout=2).read())
        t1 = time.perf_counter() * 1000
        lo, hi = r["store_read_mono_ms"]
        ok += t0 <= lo <= hi <= t1
    return f"{ok}/{n}"
print(json.dumps({"netem_one_way_delay": "$DELAY", "netem_jitter": "$JITTER", "suites": "${SUITES:-all}",
                  "seed_offset": $S, "compose_extra": "${COMPOSE_EXTRA:-}", "trace_phase": "$TRACE_PHASE",
                  "tcp_connect_rtt_ms_p50_p99": {"bench->xair": rtt("xair", 8080), "bench->gateway": rtt("gateway", 9092), "bench->redis": rtt("redis", 6379)},
                  "shared_monotonic_clock_check": shared_clock(),
                  "loadavg": open("/proc/loadavg").read().split()[:3]}))
PY
X=experiments
suite() { local name="$1"; shift; [ -z "$SUITES" ] || [[ " $SUITES " == *" $name "* ]] || return 0; "${BENCH[@]}" "$X/$@"; }
suite e1 run_e1_baselines.py --runs 100 --seed $((42 + S))
suite e1_fpr run_e1_fpr.py --runs 100
suite e9 run_e9_consistency_sweep.py --runs 10
suite e10_boundary run_e10_toctou.py --mode xair --runs-per-delay 40 --seed $((43 + S)) --offsets-ms 0 30 40 45 50 55 60 100 --out "$RES/e10_toctou_boundary.csv"
suite e10_atomic run_e10_toctou.py --mode xair_atomic --runs-per-delay 40 --seed $((43 + S)) --offsets-ms 0 30 40 45 50 55 60 100 --out "$RES/e10_toctou_atomic.csv"
suite e10_natural run_e10_toctou.py --mode xair --runs-per-delay 40 --seed $((44 + S)) --publish-delay-ms 0 --offsets-ms 0 1 2 --out "$RES/e10_natural_window.csv"
suite e10_natural_atomic run_e10_toctou.py --mode xair_atomic --runs-per-delay 40 --seed $((44 + S)) --publish-delay-ms 0 --offsets-ms 0 1 2 --out "$RES/e10_natural_window_atomic.csv"
suite e10_deadline run_e10_deadline.py --runs 10 --seed $((10 + S)) --out "$RES/e10_deadline.csv"
suite e16 run_e16_context_churn.py --runs 100 --seed $((42 + S))
suite e16_trace run_e16_trace_churn.py --runs 100 --cycles 5 --seed $((16 + S)) --phase "$TRACE_PHASE"
suite e17 run_e17_policy.py --runs 100
suite e12 run_e12_scaling.py --trials 100 --warmup 20 --repetitions 5 --producers 1 10 50 --context-kb 1 64
if [ -z "$SUITES" ] || [[ " $SUITES " == *" e18 "* ]]; then
  "${COMPOSE[@]}" exec -T -e XAIR_RESULTS_DIR="$RES" -e XAIR_E18_REDIS_URL=redis://redis:6379/2 bench \
    python "$X/run_e18_outbox_faults.py" --reps 10 --n 100 --seed $((18 + S)) --out "$RES/e18_outbox_faults.csv"
fi
echo "Distributed campaign complete -> $OUT"
