#!/usr/bin/env bash
# Verify the runtime package is importable and the unit tests pass.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
[ -d "$REPO_ROOT/xair/core" ] && [ -d "$REPO_ROOT/xair/adapters" ] || { echo "FAIL: xair package incomplete" >&2; exit 1; }
(cd "$REPO_ROOT" && "$PY" -m pytest -q tests)
"$PY" "$REPO_ROOT/experiments/run_e0_lifecycle.py" >/dev/null
echo "OK: runtime importable, unit tests and E0 pass."
