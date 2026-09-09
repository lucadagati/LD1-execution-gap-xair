#!/usr/bin/env bash
# Verify XAIR_Runtime is self-contained and contract unit tests pass.
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"

if [ ! -d "$XAIR_ROOT/xair/core" ] || [ ! -d "$XAIR_ROOT/xair/adapters" ]; then
  echo "FAIL: missing xair/core or xair/adapters under $XAIR_ROOT" >&2
  exit 1
fi

PY="${XAIR_ROOT}/.venv/bin/python"
if [ ! -x "$PY" ]; then
  python3 -m venv "${XAIR_ROOT}/.venv"
  "$PY" -m pip install -e "$XAIR_ROOT[dev]" -q
fi

echo "=== XAIR unit tests (PYTHONPATH=$XAIR_ROOT) ==="
PYTHONPATH="$XAIR_ROOT" "$PY" -m unittest discover -s "$XAIR_ROOT/tests" -p 'test_contract*.py' -v
echo "OK: runtime importable and contract tests pass."
