#!/usr/bin/env bash
# Run the evaluation on the distributed testbed (docker/distributed/compose.yml).
# Usage: sudo ./scripts/run_distributed.sh [one-way-delay] [jitter]   (default 0.5ms 0.1ms)
# Requires docker, root (nsenter + tc), and the xair-node image
# (docker build -t xair-node -f docker/xair-node/Dockerfile .).
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
DELAY="${1:-0.5ms}"; JITTER="${2:-0.1ms}"
ONLY="${ONLY:-}"   # e.g. ONLY=e10_natural_atomic: run just that suite (environment.txt kept)
COMPOSE=(docker compose -f "$REPO_ROOT/docker/distributed/compose.yml")
OUT="$REPO_ROOT/experiments/results/distributed"
docker image inspect xair-node >/dev/null 2>&1 || docker build -q -t xair-node -f "$REPO_ROOT/docker/xair-node/Dockerfile" "$REPO_ROOT"

cleanup() { "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT
"${COMPOSE[@]}" up -d --wait redis xair gateway bench >/dev/null
for svc in redis xair gateway bench; do
  pid="$(docker inspect -f '{{.State.Pid}}' "xair-dist-$svc-1")"
  nsenter -t "$pid" -n tc qdisc replace dev eth0 root netem delay "$DELAY" "$JITTER" distribution normal
done
mkdir -p "$OUT"
BENCH=("${COMPOSE[@]}" exec -T bench python)
ENV_OUT="$OUT/environment.txt"; [ -z "$ONLY" ] || ENV_OUT=/dev/null
"${BENCH[@]}" - > "$ENV_OUT" <<PY
import json, socket, statistics, time, urllib.request
def rtt(host, port, n=200):
    s = []
    for _ in range(n):
        t = time.perf_counter(); c = socket.create_connection((host, port), timeout=2); s.append((time.perf_counter() - t) * 1000); c.close()
    return round(statistics.median(s), 3), round(sorted(s)[int(0.99 * n) - 1], 3)
print(json.dumps({"netem_one_way_delay": "$DELAY", "netem_jitter": "$JITTER",
                  "tcp_connect_rtt_ms_p50_p99": {"bench->xair": rtt("xair", 8080), "bench->gateway": rtt("gateway", 9092), "bench->redis": rtt("redis", 6379)},
                  "loadavg": open("/proc/loadavg").read().split()[:3]}))
PY
X=experiments
suite() { local name="$1"; shift; [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || return 0; "${BENCH[@]}" "$X/$@"; }
suite e1 run_e1_baselines.py --runs 100 --seed 42
suite e1_fpr run_e1_fpr.py --runs 100
suite e9 run_e9_consistency_sweep.py --runs 10
suite e10_boundary run_e10_toctou.py --mode xair --runs-per-delay 40 --seed 43 --offsets-ms 0 30 40 45 50 55 60 100 --out "/repo/$X/results/distributed/e10_toctou_boundary.csv"
suite e10_atomic run_e10_toctou.py --mode xair_atomic --runs-per-delay 40 --seed 43 --offsets-ms 0 30 40 45 50 55 60 100 --out "/repo/$X/results/distributed/e10_toctou_atomic.csv"
suite e10_natural run_e10_toctou.py --mode xair --runs-per-delay 40 --seed 44 --publish-delay-ms 0 --offsets-ms 0 1 2 --out "/repo/$X/results/distributed/e10_natural_window.csv"
suite e10_natural_atomic run_e10_toctou.py --mode xair_atomic --runs-per-delay 40 --seed 44 --publish-delay-ms 0 --offsets-ms 0 1 2 --out "/repo/$X/results/distributed/e10_natural_window_atomic.csv"
suite e16 run_e16_context_churn.py --runs 100
suite e16_trace run_e16_trace_churn.py --runs 100 --cycles 5
suite e12 run_e12_scaling.py --trials 100 --warmup 20 --repetitions 5 --producers 1 10 50 --context-kb 1 64
"${BENCH[@]}" $X/aggregate_experiment_results.py --results "/repo/$X/results/distributed" >/dev/null
echo "Distributed campaign complete -> $OUT"
