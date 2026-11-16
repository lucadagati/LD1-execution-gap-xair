#!/usr/bin/env bash
# E22-LAN: the physical configuration on a local network. XAIR, Redis, and the
# producers run on one machine (CORE_HOST), the gateway and the cell controller
# on another (EDGE_HOST); both sit on the same LAN, so every measured request
# stays on it. This host only orchestrates over ssh and collects the results.
#   CORE_HOST=rpi4 CORE_IP=192.168.0.101 EDGE_HOST=coralmac EDGE_IP=192.168.0.130 ./scripts/run_lan.sh
# Each remote gets LAN_DIR/venv (Python 3.12 + requirements.lock, via uv) and LAN_DIR/repo.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
: "${CORE_HOST:?}" "${CORE_IP:?}" "${EDGE_HOST:?}" "${EDGE_IP:?}"
LAN_DIR="${LAN_DIR:-xair-lan}"
XPORT=18280; GPORT=19292; CPORT=4842; RPORT=6393
CAMP="${CAMPAIGN:-0}"   # CAMPAIGN=k (k>=1): independent repeat, seeds shifted by k, in lan/campaigns/ck
EPS="${LAN_EPS:-0}"     # declared clock uncertainty (ms) of XAIR and of the edge gateway
OUT="$REPO_ROOT/experiments/results/lan"
[ "$CAMP" = 0 ] || OUT="$OUT/campaigns/c$CAMP"
SUITES="${SUITES:-}"
ENV_NAME=environment.txt; [ -z "$SUITES" ] || ENV_NAME="environment_${SUITES// /_}.txt"
mkdir -p "$OUT"
core() { ssh -o ConnectTimeout=10 "$CORE_HOST" "$@"; }
edge() { ssh -o ConnectTimeout=10 "$EDGE_HOST" "$@"; }

cleanup() {
  [ -n "${EDGE_SSH:-}" ] && kill "$EDGE_SSH" 2>/dev/null || true
  edge "cd $LAN_DIR 2>/dev/null && for f in gw.pid cell.pid; do [ -f \$f ] && kill \$(cat \$f) 2>/dev/null; rm -f \$f; done" || true
  core "cd $LAN_DIR 2>/dev/null && [ -f xair.pid ] && kill \$(cat xair.pid) 2>/dev/null; rm -f $LAN_DIR/xair.pid; docker rm -f xair-lan-redis >/dev/null 2>&1" || true
}
trap cleanup EXIT

# sync code and environments
tar czf /tmp/xair_lan_src.tgz -C "$REPO_ROOT" xair scripts experiments/*.py schemas requirements.lock pyproject.toml
for h in "$CORE_HOST" "$EDGE_HOST"; do
  ssh "$h" "mkdir -p $LAN_DIR"
  scp -q /tmp/xair_lan_src.tgz "$h:$LAN_DIR/"
  ssh "$h" "export PATH=\$HOME/.local/bin:\$PATH; cd $LAN_DIR && rm -rf repo && mkdir repo && tar xzf xair_lan_src.tgz -C repo && \
    { [ -x venv/bin/python ] || uv venv -q -p 3.12 venv; } && VIRTUAL_ENV=\$PWD/venv uv pip install -q -r repo/requirements.lock"
done

# core: Redis + XAIR on the LAN address
core "docker rm -f xair-lan-redis >/dev/null 2>&1; docker run -d --name xair-lan-redis -p 127.0.0.1:$RPORT:6379 redis:7-alpine@sha256:520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7 redis-server --save '' --appendonly no >/dev/null && sleep 1 && \
  cd $LAN_DIR/repo && (XAIR_STOP_FENCE=${XAIR_STOP_FENCE:-1} XAIR_CLOCK_UNCERTAINTY_MS=$EPS REDIS_URL=redis://127.0.0.1:$RPORT/0 nohup ../venv/bin/python -m uvicorn xair.adapters.http_server:app --host $CORE_IP --port $XPORT --log-level warning > ../xair.log 2>&1 & echo \$! > ../xair.pid) && \
  for _ in \$(seq 120); do curl -sf http://$CORE_IP:$XPORT/v1/metrics >/dev/null && exit 0; sleep 0.5; done; echo XAIR did not start >&2; tail ../xair.log >&2; exit 1"

# edge: cell controller + gateway
edge "for i in \$(seq 60); do curl -s -m 3 -o /dev/null http://$CORE_IP:$XPORT/v1/metrics && exit 0; sleep 1; done; exit 1" || { echo 'edge cannot reach XAIR' >&2; core "tail $LAN_DIR/xair.log; pgrep -af uvicorn" >&2; exit 1; }
# The edge processes stay children of one ssh session held open for the whole
# campaign: macOS denies local-network access ("No route to host") to processes
# detached from a closed session, which silently broke every request to XAIR.
ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 "$EDGE_HOST" "cd $LAN_DIR/repo && \
  ../venv/bin/python scripts/cell_controller.py --port $CPORT --scan-ms 10 --xair http://$CORE_IP:$XPORT > ../cell.log 2>&1 & echo \$! > ../cell.pid; sleep 2; \
  XAIR_CLOCK_UNCERTAINTY_MS=$EPS XAIR_URL=http://$CORE_IP:$XPORT CELL_URL=opc.tcp://127.0.0.1:$CPORT/cell/ PYTHONPATH=. ../venv/bin/python scripts/actuator_gateway.py 19291 $GPORT > ../gw.log 2>&1 & echo \$! > ../gw.pid; \
  wait" &
EDGE_SSH=$!
core "for _ in \$(seq 120); do curl -s -m 2 -o /dev/null -w '%{http_code}' http://$EDGE_IP:$GPORT/ | grep -q '[1-5]' && exit 0; sleep 0.5; done; exit 1" || { echo 'gateway did not start' >&2; edge "tail $LAN_DIR/gw.log" >&2; exit 1; }

# the campaign runs on the core host; producers talk to the gateway over the LAN
core "cd $LAN_DIR/repo/experiments && mkdir -p ../../out && rm -f ../../out/* && cat > ../../campaign.sh" <<EOF
set -euo pipefail
export XAIR_URL=http://$CORE_IP:$XPORT ADAPTER_URL=http://$EDGE_IP:$GPORT CELL_URL=opc.tcp://$EDGE_IP:$CPORT/cell/ \
       XAIR_RESULTS_DIR=\$HOME/$LAN_DIR/out XAIR_SHARED_CLOCK=0
PY=\$HOME/$LAN_DIR/venv/bin/python
cd \$HOME/$LAN_DIR/repo/experiments
\$PY - > \$HOME/$LAN_DIR/out/$ENV_NAME <<'PYE'
import json, socket, statistics, platform
import time
def rtt(host, port, n=200):
    s = []
    for _ in range(n):
        t = time.perf_counter(); c = socket.create_connection((host, port), timeout=5); s.append((time.perf_counter() - t) * 1000); c.close()
    s.sort()
    return {"p50": round(statistics.median(s), 2), "p90": round(s[int(0.9 * n) - 1], 2), "p99": round(s[int(0.99 * n) - 1], 2)}
print(json.dumps({"topology": "producers+XAIR+Redis on $CORE_IP ($CORE_HOST); gateway+cell controller on $EDGE_IP ($EDGE_HOST); same Wi-Fi LAN",
                  "tcp_connect_rtt_ms core->gateway": rtt("$EDGE_IP", $GPORT),
                  "core": platform.platform(), "loadavg": open("/proc/loadavg").read().split()[:3]}))
PYE
suite() { local name="\$1"; shift; [ -z "$SUITES" ] || [[ " $SUITES " == *" \$name "* ]] || return 0; echo "== \$name"; \$PY "\$@"; }
suite e1 run_e1_baselines.py --runs 100 --seed 42 --baselines direct naive local xair
suite e1_fpr run_e1_fpr.py --runs 100
suite e10_natural run_e10_toctou.py --mode xair --runs-per-delay 40 --seed $((44 + CAMP)) --publish-delay-ms 0 --offsets-ms 0 5 10 20 40 --out \$XAIR_RESULTS_DIR/e10_natural_window.csv
suite e10_natural_atomic run_e10_toctou.py --mode xair_atomic --runs-per-delay 40 --seed $((44 + CAMP)) --publish-delay-ms 0 --offsets-ms 0 5 10 20 40 --out \$XAIR_RESULTS_DIR/e10_natural_window_atomic.csv
suite e10_aba run_e10_aba.py --seed $((11 + CAMP)) --delay-ms ${ABA_DELAY:-200} --pause-ms ${ABA_PAUSE:-80} --resume-ms ${ABA_RESUME:-130} --gates xair/readset xair/truth xair/predicate xair_atomic/readset xair_atomic/truth --until-valid
suite e21 run_e21_cell.py --seed $((21 + CAMP)) --offsets-ms 0 10 20 30 40 60 80 100 120
suite e21_aba run_e21_cell.py --seed $((31 + CAMP)) --aba-gap-ms 10 --offsets-ms 0 10 20 30 40 60 80 --out \$XAIR_RESULTS_DIR/e21_cell_aba.csv
for g in ${ABA_GAPS:-}; do suite e21_aba_gap run_e21_cell.py --seed $((31 + CAMP)) --aba-gap-ms \$g --offsets-ms 0 10 20 30 40 60 80 100 --out \$XAIR_RESULTS_DIR/e21_cell_aba_gap\$g.csv; done
suite e10_deadline run_e10_deadline.py --deadline-ms ${DL_MS:-100} --delays-ms ${DL_DELAYS:-80 130 2.5} --runs ${DL_RUNS:-10} --seed $((12 + CAMP)) --remote-offset-ms ${REMOTE_OFFSET_MS:-0} --producer-offset-ms ${DL_PROD:-0} --out \$XAIR_RESULTS_DIR/e10_deadline_eps${EPS}_prod${DL_PROD:-0}_d${DL_MS:-100}.csv
suite e16 run_e16_context_churn.py --runs 100 --seed $((42 + CAMP)) --rates-hz 0 5 20 100 --scopes global readset truth
echo "LAN campaign complete"
EOF
edge "uname -srm; sysctl -n machdep.cpu.brand_string 2>/dev/null; networksetup -getairportnetwork en1 2>/dev/null | sed 's/:.*//' || true" > "$OUT/edge.txt"
core "bash $LAN_DIR/campaign.sh" 2>&1 | tee "$OUT/campaign.log" || echo "campaign exited with an error: collecting what it produced" >&2
core "uname -srm" >> "$OUT/edge.txt"
scp -q "$CORE_HOST:$LAN_DIR/out/*" "$OUT/"
# refuse a campaign in which any request failed for infrastructure reasons
if grep -l -E "xair_unreachable|context_untrusted|adapter_unreachable" "$OUT"/*.csv; then
  echo "infrastructure failures in the files above: campaign rejected" >&2; exit 1
fi
echo "LAN campaign collected -> $OUT"
