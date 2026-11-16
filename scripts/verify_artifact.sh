#!/usr/bin/env bash
# Verify the runtime package, the unit tests, the E0 lifecycle cases, and every data manifest.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
[ -d "$REPO_ROOT/xair/core" ] && [ -d "$REPO_ROOT/xair/adapters" ] || { echo "FAIL: xair package incomplete" >&2; exit 1; }
(cd "$REPO_ROOT" && "$PY" -m pytest -q tests)
"$PY" "$REPO_ROOT/experiments/run_e0_lifecycle.py" >/dev/null
"$PY" "$REPO_ROOT/scripts/make_manifest.py" --check "$REPO_ROOT/data"
echo "OK: runtime importable, unit tests and E0 pass, data match their manifests."
