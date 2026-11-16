#!/usr/bin/env bash
# E22: physical remote node. The actuator side (gateway and cell controller)
# runs on a separate machine reached over a real network (Tailscale); XAIR,
# Redis, and the producers run on this host.
#   PHYS_HOST=user@host PHYS_DIR=~/xair-physical SSHPASS=... ./scripts/run_physical.sh
# The remote needs PHYS_DIR/venv (Python 3.12 + requirements.lock) and PHYS_DIR/repo.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
: "${PHYS_HOST:?set PHYS_HOST=user@host}"
PHYS_DIR="${PHYS_DIR:-xair-physical}"
LOCAL_IP="${LOCAL_IP:-$(tailscale ip -4 | head -1)}"
REMOTE_IP="${REMOTE_IP:-${PHYS_HOST#*@}}"
XPORT=18180; GPORT=19192; CPORT=4841; RPORT=6392
CAMP="${CAMPAIGN:-0}"   # CAMPAIGN=k (k>=1): independent repeat, seeds shifted by k, in physical/campaigns/ck
EPS="${PHYS_EPS:-0}"    # declared clock uncertainty (ms) of XAIR and of the remote gateway
OUT="$REPO_ROOT/experiments/results/physical"
[ "$CAMP" = 0 ] || OUT="$OUT/campaigns/c$CAMP"
SSH=(ssh -o ConnectTimeout=10 "$PHYS_HOST"); SCP=(scp -q)
if [ -n "${SSHPASS:-}" ]; then SSH=(sshpass -e "${SSH[@]}"); SCP=(sshpass -e "${SCP[@]}"); fi
mkdir -p "$OUT"

cleanup() {
  "${SSH[@]}" "cd $PHYS_DIR && for f in gw.pid cell.pid; do [ -f \$f ] && kill \$(cat \$f) 2>/dev/null; rm -f \$f; done" || true
  [ -n "${XPID:-}" ] && kill "$XPID" 2>/dev/null || true
  docker rm -f xair-phys-redis >/dev/null 2>&1 || true
}
trap cleanup EXIT

# sync code
tar czf /tmp/xair_phys_src.tgz -C "$REPO_ROOT" xair scripts experiments/*.py schemas requirements.lock pyproject.toml
"${SCP[@]}" /tmp/xair_phys_src.tgz "$PHYS_HOST:$PHYS_DIR/"
"${SSH[@]}" "cd $PHYS_DIR && rm -rf repo && mkdir repo && tar xzf xair_phys_src.tgz -C repo"

# local: Redis + XAIR bound to the tailnet address
docker rm -f xair-phys-redis >/dev/null 2>&1 || true
docker run -d --name xair-phys-redis -p 127.0.0.1:$RPORT:6379 redis:7-alpine@sha256:520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7 redis-server --save "" --appendonly no >/dev/null
sleep 1
XAIR_STOP_FENCE=${XAIR_STOP_FENCE:-1} XAIR_CLOCK_UNCERTAINTY_MS=$EPS REDIS_URL=redis://127.0.0.1:$RPORT/0 taskset -c "${XAIR_CPUS:-28-31}" "$PY" -m uvicorn xair.adapters.http_server:app --host "$LOCAL_IP" --port $XPORT \
  --log-level warning > "$OUT/xair.log" 2>&1 &
XPID=$!
for _ in $(seq 50); do curl -sf "http://$LOCAL_IP:$XPORT/v1/metrics" >/dev/null && break; sleep 0.2; done

# remote: cell controller + gateway
"${SSH[@]}" "cd $PHYS_DIR/repo && (nohup ../venv/bin/python scripts/cell_controller.py --port $CPORT --scan-ms 10 --xair http://$LOCAL_IP:$XPORT > ../cell.log 2>&1 & echo \$! > ../cell.pid) && sleep 2 && \
  (XAIR_CLOCK_UNCERTAINTY_MS=$EPS XAIR_URL=http://$LOCAL_IP:$XPORT CELL_URL=opc.tcp://127.0.0.1:$CPORT/cell/ PYTHONPATH=. nohup ../venv/bin/python scripts/actuator_gateway.py 19191 $GPORT > ../gw.log 2>&1 & echo \$! > ../gw.pid)"
for _ in $(seq 50); do curl -sf -o /dev/null "http://$REMOTE_IP:$GPORT/" -m 2 && break; curl -s -m 2 -o /dev/null -w '%{http_code}' "http://$REMOTE_IP:$GPORT/" | grep -q '[1-5]' && break; sleep 0.5; done

export XAIR_URL="http://$LOCAL_IP:$XPORT" ADAPTER_URL="http://$REMOTE_IP:$GPORT" CELL_URL="opc.tcp://$REMOTE_IP:$CPORT/cell/" \
       XAIR_RESULTS_DIR="$OUT" XAIR_SHARED_CLOCK=0
ENV_OUT="$OUT/environment.txt"; [ -z "${SUITES:-}" ] || ENV_OUT="$OUT/environment_${SUITES// /_}.txt"
"$PY" - > "$ENV_OUT" <<PY
import json, socket, statistics, time, subprocess, platform
def rtt(host, port, n=100):
    s = []
    for _ in range(n):
        t = time.perf_counter(); c = socket.create_connection((host, port), timeout=5); s.append((time.perf_counter() - t) * 1000); c.close()
    return round(statistics.median(s), 2), round(sorted(s)[int(0.99 * n) - 1], 2)
print(json.dumps({"topology": "producers+XAIR+Redis on $LOCAL_IP (this host); gateway+cell controller on $REMOTE_IP (separate physical machine, Tailscale)",
                  "tcp_connect_rtt_ms_p50_p99": {"host->gateway": rtt("$REMOTE_IP", $GPORT)},
                  "local": platform.platform(), "loadavg": open("/proc/loadavg").read().split()[:3]}))
PY
"${SSH[@]}" "uname -srm; sysctl -n machdep.cpu.brand_string 2>/dev/null; python3 -c 'import time; print(time.time())'" >> "$ENV_OUT"
cd "$REPO_ROOT/experiments"
PY="taskset -c ${BENCH_CPUS:-32-34} $PY"
SUITES="${SUITES:-}"
suite() { local name="$1"; shift; [ -z "$SUITES" ] || [[ " $SUITES " == *" $name "* ]] || return 0; echo "== $name"; $PY "$@" || echo "suite $name failed (exit $?): continuing" >&2; }
suite e1 run_e1_baselines.py --runs 100 --seed 42 --baselines direct naive local xair
suite e1_fpr run_e1_fpr.py --runs 100
suite e10_natural run_e10_toctou.py --mode xair --runs-per-delay 40 --seed $((44 + CAMP)) --publish-delay-ms 0 --offsets-ms 0 20 40 60 80 --out "$OUT/e10_natural_window.csv"
suite e10_natural_atomic run_e10_toctou.py --mode xair_atomic --runs-per-delay 40 --seed $((44 + CAMP)) --publish-delay-ms 0 --offsets-ms 0 20 40 60 80 --out "$OUT/e10_natural_window_atomic.csv"
suite e10_aba run_e10_aba.py --seed $((11 + CAMP)) --delay-ms 400 --pause-ms 280 --resume-ms 450 --gates xair/readset xair/truth xair/predicate xair_atomic/readset xair_atomic/truth --until-valid
suite e21 run_e21_cell.py --seed $((21 + CAMP)) --offsets-ms 0 20 40 60 80 100 120 160 200 240 280 320 400
suite e21_aba run_e21_cell.py --seed $((31 + CAMP)) --aba-gap-ms 20 --offsets-ms 80 100 120 140 160 180 200 240 280 320 400 --out "$OUT/e21_cell_aba.csv"
for g in ${ABA_GAPS:-}; do suite e21_aba_gap run_e21_cell.py --seed $((31 + CAMP)) --aba-gap-ms $g --offsets-ms 80 100 120 140 160 180 200 240 280 --out "$OUT/e21_cell_aba_gap$g.csv"; done
suite e10_deadline run_e10_deadline.py --deadline-ms ${DL_MS:-100} --delays-ms ${DL_DELAYS:-80 130 2.5} --runs ${DL_RUNS:-10} --seed $((12 + CAMP)) --remote-offset-ms ${REMOTE_OFFSET_MS:-0} --producer-offset-ms ${DL_PROD:-0} --out "$OUT/e10_deadline_eps${EPS}_prod${DL_PROD:-0}_d${DL_MS:-100}.csv"
suite e16 run_e16_context_churn.py --runs 100 --seed $((42 + CAMP)) --rates-hz 0 5 20 --scopes global readset truth
echo "Physical campaign complete -> $OUT"
