#!/usr/bin/env bash
# Verifica E2E: XAIR + adapter + context REVOKE (no in-process mock)
set -e

BASE="${1:-http://localhost:9092}"
XAIR="${XAIR_URL:-http://localhost:8080}"
ADAPTIX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE="$ADAPTIX_ROOT/XAIR_Runtime/examples/manufacturing-resume-stale.json"

echo "=== Verify E2E XAIR + AdaptiX ==="

# 1. XAIR metrics
curl -sf "$XAIR/v1/metrics" >/dev/null && echo "[OK] XAIR /v1/metrics" || { echo "[FAIL] XAIR"; exit 1; }

# 2. Context RUN
curl -sf -X POST "$BASE/context" -H "Content-Type: application/json" \
  -d '{"line":{"state":"RUN"},"robot":{"speed":0.05},"gripper":{"state":"OPEN"},"human_proximity_m":1.0}' | grep -q '"ok": true' \
  && echo "[OK] POST /context RUN" || { echo "[FAIL] context RUN"; exit 1; }

# 3. Intent should EXECUTE when line RUN (fresh timestamp)
python3 - <<PY
import json, uuid, urllib.request
from datetime import datetime, timezone
from pathlib import Path
data = json.loads(Path("$EXAMPLE").read_text())
data["id"] = str(uuid.uuid4())
data["timestamp_decision"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
req = urllib.request.Request(
    "$BASE/intent", data=json.dumps(data).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req) as r:
    out = json.loads(r.read())
assert out.get("outcome") == "EXECUTE", out
print("[OK] intent EXECUTE on RUN")
PY

# 4. Pause line → REVOKE
curl -sf -X POST "$BASE/context" -H "Content-Type: application/json" \
  -d '{"line":{"state":"PAUSED"}}' >/dev/null

# Fresh intent with new id
python3 - <<PY
import json, uuid, urllib.request
from pathlib import Path
p = Path("$EXAMPLE")
data = json.loads(p.read_text())
data["id"] = str(uuid.uuid4())
req = urllib.request.Request(
    "$BASE/intent",
    data=json.dumps(data).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req) as r:
    out = json.loads(r.read())
assert out.get("outcome") == "REVOKE", out
print("[OK] intent REVOKED when line PAUSED")
PY

# 5. Adapter health
curl -sf "$BASE/health" >/dev/null && echo "[OK] adapter /health" || echo "[WARN] adapter health skip"

echo ""
echo "E2E verification passed."
