#!/usr/bin/env bash
# Version wrap on the OpenPLC cell controller: after 32768 changes the 16-bit counter of
# docker/openplc/cell_ctl.st is back at its old value, and only the epoch tells the two apart.
# A witness taken before the wrap must be refused after it; a fresh witness must execute.
#   ./scripts/run_plc_wrap.sh        (about ten minutes; OpenPLC image: docker build -t xair-openplc <OpenPLC_v3 checkout>)
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
OUT="$REPO_ROOT/experiments/results/openplc"; mkdir -p "$OUT"
WEB=18083; MB=15021
cleanup() { docker rm -f xair-openplc-wrap >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker rm -f xair-openplc-wrap >/dev/null 2>&1 || true
docker run -d --name xair-openplc-wrap -p 127.0.0.1:$WEB:8080 -p 127.0.0.1:$MB:502 xair-openplc:latest >/dev/null
for _ in $(seq 60); do curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$WEB/login" | grep -q 200 && break; sleep 2; done
"$PY" "$REPO_ROOT/scripts/openplc_deploy.py" --url "http://127.0.0.1:$WEB" "$REPO_ROOT/docker/openplc/cell_ctl.st"
sleep 3
PLC_ADDR="127.0.0.1:$MB" PYTHONPATH="$REPO_ROOT/scripts" "$PY" "$REPO_ROOT/experiments/run_plc_wrap.py" --out "$OUT/plc_wrap.json"
