#!/usr/bin/env bash
# Clean-clone audit: verify the published repo reproduces from a fresh checkout,
# independent of any local monorepo layout the working copy might also have.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AUDIT_DIR="${1:-/tmp/xair-clean-audit}"
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

# This repo is flat: xair/, experiments/, scripts/, schemas/, pyproject.toml
# all live at AUDIT_DIR itself (no nested XAIR_Runtime/ subdirectory).
XAIR="$AUDIT_DIR"
AUDIT_SCRIPTS="$AUDIT_DIR/scripts"
test -d "$AUDIT_DIR/xair" && test -d "$AUDIT_SCRIPTS"

# start_full_stack.sh needs .venv/bin/uvicorn to already exist, or it falls
# back to a bare `uvicorn` on PATH and fails to start.
python3 -m venv "$XAIR/.venv"
"$XAIR/.venv/bin/pip" install -e "$XAIR[dev]" -q
"$AUDIT_SCRIPTS/start_full_stack.sh"

PY="$XAIR/.venv/bin/python"
$PY "$XAIR/experiments/run_e0_lifecycle.py" | grep -q '"passed": 7'
$PY "$XAIR/experiments/run_e1_baselines.py" --runs 5 --seed 1 --baselines xair local
$PY "$XAIR/experiments/run_e12_scaling.py" --trials 20 --producers 1 --context-kb 1
$PY "$XAIR/experiments/run_e13_faults.py"

test -f "$XAIR/experiments/results/e1_baselines.csv"
test -f "$XAIR/experiments/results/e13_faults.csv"

"$AUDIT_SCRIPTS/verify_artifact.sh"

echo "=== Clean-clone audit PASSED ==="
