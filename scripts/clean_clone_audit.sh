#!/usr/bin/env bash
# Clean-clone audit: verify monorepo reproduction from fresh checkout.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AUDIT_DIR="${1:-/tmp/adaptix-clean-audit}"
REPO_URL="${REPO_URL:-file://$ROOT}"

echo "=== Clean-clone audit ==="
rm -rf "$AUDIT_DIR"
if [[ "$REPO_URL" == file://* ]]; then
  cp -a "${REPO_URL#file://}" "$AUDIT_DIR"
else
  git clone "$REPO_URL" "$AUDIT_DIR"
fi
cd "$AUDIT_DIR"
git -C "$ROOT" rev-parse HEAD >/dev/null 2>&1 && git checkout "$(git -C "$ROOT" rev-parse HEAD)" 2>/dev/null || true

AUDIT_SCRIPTS="$AUDIT_DIR/scripts"
XAIR="$AUDIT_DIR/XAIR_Runtime"
test -d "$XAIR" && test -d "$AUDIT_SCRIPTS"

"$AUDIT_SCRIPTS/start_full_stack.sh"
python3 -m venv "$XAIR/.venv"
"$XAIR/.venv/bin/pip" install -e "$XAIR[dev]" -q

PY="$XAIR/.venv/bin/python"
$PY "$XAIR/experiments/run_e0_lifecycle.py" | grep -q '"passed": 7'
$PY "$XAIR/experiments/run_e1_baselines.py" --runs 5 --seed 1 --baselines xair local
$PY "$XAIR/experiments/run_e12_scaling.py" --trials 20 --producers 1 --context-kb 1
$PY "$XAIR/experiments/run_e13_faults.py"

"$AUDIT_SCRIPTS/verify_artifact.sh"
test -f "$AUDIT_DIR/data/execution-gap/e10_toctou.csv"
test -f "$AUDIT_DIR/COMMIT.txt"

echo "=== Clean-clone audit PASSED ==="
