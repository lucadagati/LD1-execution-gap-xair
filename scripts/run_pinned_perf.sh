#!/usr/bin/env bash
# E4 and E12 on reserved cores: XAIR, gateway, harness, and a dedicated Redis
# container each pinned to disjoint CPU sets, to limit interference on a shared host.
# Usage: ./scripts/run_pinned_perf.sh   (override with XAIR_CPUS, GW_CPUS, BENCH_CPUS, REDIS_CPUS)
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
XAIR_CPUS="${XAIR_CPUS:-28-31}"; GW_CPUS="${GW_CPUS:-32-34,36}"; BENCH_CPUS="${BENCH_CPUS:-16-20}"; REDIS_CPUS="${REDIS_CPUS:-3,6}"
PORT_X="${PINNED_XAIR_PORT:-18080}"; PORT_G="${PINNED_GW_PORT:-19092}"; PORT_W="${PINNED_WS_PORT:-19091}"; PORT_R="${PINNED_REDIS_PORT:-6380}"
OUT="$REPO_ROOT/experiments/results/pinned"; RUN="$RUN_DIR/pinned"; mkdir -p "$OUT" "$RUN"
export XAIR_URL="http://127.0.0.1:$PORT_X" ADAPTER_URL="http://127.0.0.1:$PORT_G" XAIR_RESULTS_DIR="$OUT"

cleanup() { kill "$(cat "$RUN/xair.pid")" "$(cat "$RUN/gw.pid")" 2>/dev/null || true; docker rm -f xair-pinned-redis >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --rm --name xair-pinned-redis --cpuset-cpus "$REDIS_CPUS" -p "127.0.0.1:$PORT_R:6379" redis:7-alpine redis-server --save "" --appendonly no >/dev/null
(cd "$REPO_ROOT" && exec setsid env REDIS_URL="redis://127.0.0.1:$PORT_R/0" PYTHONPATH="$REPO_ROOT" taskset -c "$XAIR_CPUS" "$PY" -m uvicorn xair.adapters.http_server:app --host 127.0.0.1 --port "$PORT_X" > "$RUN/xair.log" 2>&1 < /dev/null) &
echo $! > "$RUN/xair.pid"
sleep 2
(exec setsid env XAIR_URL="$XAIR_URL" PYTHONPATH="$REPO_ROOT:$SCRIPTS" taskset -c "$GW_CPUS" "$PY" "$SCRIPTS/adaptix_quest_adapter.py" "$PORT_W" "$PORT_G" > "$RUN/gw.log" 2>&1 < /dev/null) &
echo $! > "$RUN/gw.pid"
for _ in $(seq 1 25); do curl -sf "$ADAPTER_URL/health" >/dev/null && break; sleep 0.2; done
{ echo "pinned: xair=$XAIR_CPUS gateway=$GW_CPUS bench=$BENCH_CPUS redis=$REDIS_CPUS"; echo "loadavg_start=$(cut -d' ' -f1-3 /proc/loadavg)"; } > "$OUT/environment.txt"
for rep in 1 2 3; do
  taskset -c "$BENCH_CPUS" "$PY" "$REPO_ROOT/experiments/run_e4_http_load.py" --intents 10000 --out "$OUT/e4_load_http_rep$rep.csv" >/dev/null
done
taskset -c "$BENCH_CPUS" "$PY" "$REPO_ROOT/experiments/run_e12_scaling.py" --trials 100 --warmup 20 --repetitions 5 --producers 1 10 50 --context-kb 1 64 --out "$OUT/e12_scaling.csv"
echo "loadavg_end=$(cut -d' ' -f1-3 /proc/loadavg)" >> "$OUT/environment.txt"
echo "Pinned performance runs complete -> $OUT"
