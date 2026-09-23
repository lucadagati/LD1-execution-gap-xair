#!/usr/bin/env bash
# End-to-end check through the gateway: valid intent is released, paused line revokes.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
EXAMPLE="$REPO_ROOT/examples/manufacturing-resume-stale.json"

echo "=== E2E: XAIR ($XAIR_URL) + gateway ($ADAPTER_URL) ==="
curl -sf "$XAIR_URL/v1/metrics" >/dev/null && echo "[OK] XAIR /v1/metrics"
curl -sf "$ADAPTER_URL/health" >/dev/null && echo "[OK] gateway /health"

"$PY" - "$ADAPTER_URL" "$EXAMPLE" <<'PY'
import json, sys, uuid, urllib.request
from datetime import datetime, timezone
from pathlib import Path

base, example = sys.argv[1], Path(sys.argv[2])

def post(path, body):
    req = urllib.request.Request(f"{base}/{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def intent():
    data = json.loads(example.read_text())
    data["id"] = str(uuid.uuid4())
    data["timestamp_decision"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return data

post("context", {"line": {"state": "RUN"}, "gripper": {"state": "OPEN"}, "robot": {"speed": 0.05}})
out = post("intent?mode=xair", intent())
assert out.get("gateway_released") is True, out
print("[OK] valid intent released at the gateway")

post("context", {"line": {"state": "PAUSED"}})
out = post("intent?mode=xair", intent())
assert out.get("outcome") == "REVOKE" and not out.get("gateway_released"), out
print("[OK] intent revoked when line PAUSED")
PY
echo "E2E verification passed."
