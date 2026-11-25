#!/usr/bin/env bash
# E25: XAIR on embedded edge boards. Redis, XAIR, the actuator gateway, and the
# harness run on one board, each pinned to its own core, over loopback, so the
# figures are the cost of the runtime on that processor (no network).
#   BOARD=rpi4 ./scripts/run_embedded.sh                      Redis from docker
#   BOARD=vf2 REDIS_BIN='~/xair-emb/redis/src/redis-server' ./scripts/run_embedded.sh
# The board gets EMB_DIR/venv (Python 3.12 via uv, runtime dependencies only:
# no plotting, no OPC UA) and EMB_DIR/repo. Results: experiments/results/embedded/$BOARD.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
: "${BOARD:?}"
EMB_DIR="${EMB_DIR:-xair-emb}"
REDIS_BIN="${REDIS_BIN:-}"
XPORT=18480; GPORT=19492; WPORT=19491; RPORT=6395
CAMP="${CAMPAIGN:-0}"   # CAMPAIGN=k (k>=1): a repeat of the E3 and E5 suites with seeds shifted by k, in campaigns/ck
OUT="$REPO_ROOT/experiments/results/embedded/$BOARD"
[ "$CAMP" = 0 ] || OUT="$OUT/campaigns/c$CAMP"
# FENCE_COST=1: only the throughput run (E7), to price the stop fence: XAIR_STOP_FENCE=0/1 -> campaigns/fence0, fence1
if [ -n "${FENCE_COST:-}" ]; then OUT="$REPO_ROOT/experiments/results/embedded/$BOARD/campaigns/fence${XAIR_STOP_FENCE:-1}"; fi
mkdir -p "$OUT"
b() { ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 "$BOARD" "$@"; }

tmp="$(mktemp -d)"
tar czf "$tmp/src.tgz" -C "$REPO_ROOT" xair scripts experiments/*.py schemas requirements.lock pyproject.toml
grep -vE '^(matplotlib|pillow|contourpy|fonttools|kiwisolver|cycler|numpy|pandas|scipy|pyparsing|asyncua|cryptography|cffi|pycparser|pyopenssl|aiofiles|aiosqlite|sortedcontainers|wait-for2)==' \
  "$REPO_ROOT/requirements.lock" > "$tmp/req_emb.txt"
b "mkdir -p $EMB_DIR"
scp -q "$tmp/src.tgz" "$tmp/req_emb.txt" "$BOARD:$EMB_DIR/"
rm -rf "$tmp"
b "export PATH=\$HOME/.local/bin:\$PATH; cd $EMB_DIR && rm -rf repo && mkdir repo && tar xzf src.tgz -C repo && \
   { [ -x venv/bin/python ] || uv venv -q -p 3.12 venv; } && { VIRTUAL_ENV=\$PWD/venv uv pip install -q -r req_emb.txt || venv/bin/python -c 'import fastapi, uvicorn, redis, pydantic, jsonschema'; }"

b "cat > $EMB_DIR/campaign.sh" <<EOF
set -euo pipefail
cd \$HOME/$EMB_DIR
PY=\$PWD/venv/bin/python
mkdir -p out run
rm -f out/*
cleanup() { for f in run/xair.pid run/gw.pid run/redis.pid; do { [ -f \$f ] && kill \$(cat \$f) 2>/dev/null; } || true; rm -f \$f; done; docker rm -f xair-emb-redis >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
# core 3: Redis, core 2: XAIR, core 1: gateway, core 0: harness
if [ -n "$REDIS_BIN" ]; then
  taskset -c 3 $REDIS_BIN --port $RPORT --bind 127.0.0.1 --save '' --appendonly no > run/redis.log 2>&1 & echo \$! > run/redis.pid
else
  docker run -d --rm --name xair-emb-redis --cpuset-cpus 3 -p 127.0.0.1:$RPORT:6379 redis:7-alpine@sha256:520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7 redis-server --save '' --appendonly no >/dev/null
fi
sleep 1
(cd repo; XAIR_STOP_FENCE=${XAIR_STOP_FENCE:-1} REDIS_URL=redis://127.0.0.1:$RPORT/0 PYTHONPATH=\$PWD taskset -c 2 \$PY -m uvicorn xair.adapters.http_server:app --host 127.0.0.1 --port $XPORT --log-level warning > ../run/xair.log 2>&1 & echo \$! > ../run/xair.pid)
for _ in \$(seq 240); do curl -sf http://127.0.0.1:$XPORT/v1/metrics >/dev/null && break; sleep 0.5; done
(cd repo; XAIR_URL=http://127.0.0.1:$XPORT PYTHONPATH=\$PWD:\$PWD/scripts taskset -c 1 \$PY scripts/actuator_gateway.py $WPORT $GPORT > ../run/gw.log 2>&1 & echo \$! > ../run/gw.pid)
for _ in \$(seq 240); do curl -sf http://127.0.0.1:$GPORT/health >/dev/null && break; sleep 0.5; done
export XAIR_URL=http://127.0.0.1:$XPORT ADAPTER_URL=http://127.0.0.1:$GPORT XAIR_RESULTS_DIR=\$PWD/out XAIR_SHARED_CLOCK=1
X=repo/experiments
B="taskset -c 0 \$PY"
\$PY - > out/environment.txt <<'PYE'
import json, platform, os
cpu = ""
for line in open("/proc/cpuinfo"):
    if line.split(":")[0].strip() in ("model name", "uarch", "isa", "Model"):
        cpu += line.split(":", 1)[1].strip() + "; "
print(json.dumps({"board": "$BOARD", "platform": platform.platform(), "python": platform.python_version(),
                  "cpu": cpu.strip("; "), "cores": os.cpu_count(), "kernel": os.uname().version,
                  "loadavg": open("/proc/loadavg").read().split()[:3],
                  "pinning": "redis core 3, XAIR core 2, gateway core 1, harness core 0"}))
PYE
if [ "$CAMP" = 0 ] || [ -n "${FENCE_COST:-}" ]; then
for rep in 1 2 3; do \$B \$X/run_e4_http_load.py --intents 3000 --out out/e4_load_http_rep\$rep.csv > /dev/null; done
fi
if [ "$CAMP" = 0 ] && [ -z "${FENCE_COST:-}" ]; then
for s in 1 2 3; do \$B \$X/run_e11_stratified.py --runs 100 --seed \$s --out out/e11_stratified_seed\$s.csv > /dev/null; done
fi
if [ -z "${FENCE_COST:-}" ]; then
\$B \$X/run_e10_toctou.py --mode xair --runs-per-delay 40 --seed $((44 + CAMP)) --publish-delay-ms 0 --offsets-ms 0 1 2 --out out/e10_natural_window.csv > /dev/null
\$B \$X/run_e10_toctou.py --mode xair_atomic --runs-per-delay 40 --seed $((44 + CAMP)) --publish-delay-ms 0 --offsets-ms 0 1 2 --out out/e10_natural_window_atomic.csv > /dev/null
\$B \$X/run_e16_context_churn.py --runs 100 --seed $((42 + CAMP)) --rates-hz 0 20 100 --scopes global readset truth > /dev/null
fi
# resident memory of the runtime processes after the campaign
\$PY - >> out/environment.txt <<'PYE'
import json
def rss(pidfile):
    try:
        pid = open(pidfile).read().strip()
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS"):
                return int(line.split()[1]) // 1024
    except Exception:
        return None
print(json.dumps({"rss_mb": {"xair": rss("run/xair.pid"), "gateway": rss("run/gw.pid"), "redis": rss("run/redis.pid")},
                  "loadavg_end": open("/proc/loadavg").read().split()[:3]}))
PYE
echo "embedded campaign complete"
EOF
b "bash $EMB_DIR/campaign.sh" 2>&1 | tee "$OUT/campaign.log"
scp -q "$BOARD:$EMB_DIR/out/*" "$OUT/"
if grep -l -E "xair_unreachable|context_untrusted|adapter_unreachable" "$OUT"/*.csv; then
  echo "infrastructure failures in the files above: campaign rejected" >&2; exit 1
fi
echo "E25 on $BOARD complete -> $OUT"
