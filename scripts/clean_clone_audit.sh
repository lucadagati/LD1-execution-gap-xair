#!/usr/bin/env bash
# Clean-clone audit: verify rebuild from fresh checkout (CI/local).
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
AUDIT_DIR="${1:-/tmp/xair-clean-audit}"
REPO_URL="${REPO_URL:-$REPO_ROOT}"

echo "=== XAIR clean-clone audit ==="
rm -rf "$AUDIT_DIR"
if [ -d "$REPO_URL/.git" ] && [ "$REPO_URL" = "$REPO_ROOT" ]; then
  git clone "$REPO_ROOT" "$AUDIT_DIR"
elif [[ "${REPO_URL:-}" == https://* ]] || [[ "${REPO_URL:-}" == git@* ]]; then
  git clone "$REPO_URL" "$AUDIT_DIR"
else
  rsync -a --exclude '.venv' --exclude '.git' --exclude '__pycache__' "$REPO_ROOT/" "$AUDIT_DIR/"
fi

cd "$AUDIT_DIR"
XAIR="$AUDIT_DIR"
SCRIPTS="$AUDIT_DIR/scripts"

"$SCRIPTS/start_full_stack.sh"
python3 -m venv "$XAIR/.venv"
"$XAIR/.venv/bin/pip" install -e "$XAIR[dev]" -q

PY="$XAIR/.venv/bin/python"
$PY "$XAIR/experiments/run_e0_lifecycle.py" | grep -q '"passed": 7'
$PY "$XAIR/experiments/run_e1_baselines.py" --runs 5 --seed 1 --baselines xair local
$PY "$XAIR/experiments/run_e12_scaling.py" --trials 20 --producers 1 --context-kb 1
$PY "$XAIR/experiments/run_e13_faults.py"

echo "=== Clean-clone audit PASSED ==="
