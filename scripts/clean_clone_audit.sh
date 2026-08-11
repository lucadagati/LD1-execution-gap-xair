#!/usr/bin/env bash
# Clean-clone audit: verify rebuild from fresh checkout (CI/local).
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
AUDIT_DIR="${1:-/tmp/xair-clean-audit}"
REPO_URL="${REPO_URL:-$REPO_ROOT}"

echo "=== XAIR clean-clone audit ==="
rm -rf "$AUDIT_DIR"
if [[ "${REPO_URL:-}" == https://* ]] || [[ "${REPO_URL:-}" == git@* ]]; then
  git clone "$REPO_URL" "$AUDIT_DIR"
elif [ -d "$REPO_URL/.git" ]; then
  git clone "$REPO_URL" "$AUDIT_DIR"
else
  rsync -a --exclude '.venv' --exclude '.git' --exclude '__pycache__' "$REPO_ROOT/" "$AUDIT_DIR/"
fi

XAIR="$AUDIT_DIR"
SCRIPTS="$AUDIT_DIR/scripts"

python3 -m venv "$XAIR/.venv"
"$XAIR/.venv/bin/pip" install -e "$XAIR[dev]" -q

export XAIR_URL="${XAIR_URL:-http://127.0.0.1:8080}"
export XAIR_ADAPTER_WEBSOCKET=0
"$SCRIPTS/start_full_stack.sh"

PY="$XAIR/.venv/bin/python"
$PY "$XAIR/experiments/run_e0_lifecycle.py" | grep -q '"passed": 7'
$PY "$XAIR/experiments/run_e1_baselines.py" --runs 5 --seed 1 --baselines xair local
$PY "$XAIR/experiments/run_e12_scaling.py" --trials 20 --producers 1 --context-kb 1
$PY "$XAIR/experiments/run_e13_faults.py"

echo "=== Clean-clone audit PASSED ==="
