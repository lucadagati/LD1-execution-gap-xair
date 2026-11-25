#!/usr/bin/env bash
# E24: conditional actuation on a real IEC 61131-3 runtime (OpenPLC v3, 10 ms task).
# Builds/starts the OpenPLC container, deploys docker/openplc/cell_ctl.st, runs a local
# Redis + XAIR + gateway, a Modbus polling bridge PLC -> XAIR, and the E21 harness.
#   ./scripts/run_openplc.sh        (OpenPLC image: docker build -t xair-openplc <OpenPLC_v3 checkout>)
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
CAMP="${CAMPAIGN:-0}"   # CAMPAIGN=k (k>=1): independent repeat, seed 24+k, in openplc/campaigns/ck
OUT="$REPO_ROOT/experiments/results/openplc"; [ "$CAMP" = 0 ] || OUT="$OUT/campaigns/c$CAMP"; mkdir -p "$OUT"
WEB=18082; MB=15020; RPORT=6395; XPORT=18181; GPORT=19194; WSPORT=19193
cleanup() { for p in ${PIDS:-}; do kill "$p" 2>/dev/null || true; done
            docker rm -f xair-openplc xair-plc-redis >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker rm -f xair-openplc xair-plc-redis >/dev/null 2>&1 || true
docker run -d --name xair-openplc -p 127.0.0.1:$WEB:8080 -p 127.0.0.1:$MB:502 xair-openplc:latest >/dev/null
docker run -d --name xair-plc-redis -p 127.0.0.1:$RPORT:6379 redis:7-alpine@sha256:520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7 redis-server --save "" --appendonly no >/dev/null
for _ in $(seq 60); do curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$WEB/login" | grep -q 200 && break; sleep 2; done
"$PY" "$REPO_ROOT/scripts/openplc_deploy.py" --url "http://127.0.0.1:$WEB" "$REPO_ROOT/docker/openplc/cell_ctl.st"
export PLC_ADDR="127.0.0.1:$MB"
XAIR_STOP_FENCE=${XAIR_STOP_FENCE:-1} REDIS_URL=redis://127.0.0.1:$RPORT/0 taskset -c 28-31 "$PY" -m uvicorn xair.adapters.http_server:app --host 127.0.0.1 --port $XPORT --log-level warning > "$OUT/xair.log" 2>&1 &
PIDS="$!"
for _ in $(seq 50); do curl -sf "http://127.0.0.1:$XPORT/v1/metrics" >/dev/null && break; sleep 0.2; done
XAIR_URL=http://127.0.0.1:$XPORT taskset -c 32-34 "$PY" "$REPO_ROOT/scripts/actuator_gateway.py" $WSPORT $GPORT > "$OUT/gw.log" 2>&1 &
PIDS="$PIDS $!"
taskset -c 36-37 "$PY" "$REPO_ROOT/scripts/plc_modbus.py" bridge --plc "127.0.0.1:$MB" --xair "http://127.0.0.1:$XPORT" --poll-ms 5 > "$OUT/bridge.log" 2>&1 &
PIDS="$PIDS $!"
sleep 3
cat > "$OUT/environment.txt" <<ENV
{"runtime": "OpenPLC v3 ($(docker run --rm --entrypoint git xair-openplc -C /workdir log -1 --format=%h 2>/dev/null || echo unknown)), task interval 10 ms, Modbus TCP",
 "bridge": "Modbus polling every 5 ms -> XAIR", "loadavg": "$(cut -d' ' -f1-3 /proc/loadavg)"}
ENV
cd "$REPO_ROOT/experiments"
XAIR_URL=http://127.0.0.1:$XPORT ADAPTER_URL=http://127.0.0.1:$GPORT XAIR_RESULTS_DIR="$OUT" \
  taskset -c 16-20 "$PY" run_e21_cell.py --backend openplc --seed $((24 + CAMP)) --runs 20 --controls 20 \
  --offsets-ms 0 10 20 30 40 50 60 80 100 --out "$OUT/e24_openplc.csv"
echo "OpenPLC campaign complete -> $OUT"
