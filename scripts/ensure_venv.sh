#!/usr/bin/env bash
# Create the repository venv (Python >= 3.12, as required by pyproject.toml) and
# install the project with its dev extras. Uses uv when available.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
VENV="$REPO_ROOT/.venv"

is_py312() { "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' 2>/dev/null; }

if [ -x "$VENV/bin/python" ] && ! is_py312 "$VENV/bin/python"; then
  echo "ERROR: $VENV uses $("$VENV/bin/python" --version 2>&1); remove it and rerun (Python >= 3.12 required)." >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  [ -x "$VENV/bin/python" ] || uv venv --quiet --python ">=3.12" "$VENV"
  uv pip install --quiet --python "$VENV/bin/python" -e "$REPO_ROOT[dev]"
else
  if [ ! -x "$VENV/bin/python" ]; then
    PYBIN=""
    for cand in "${PYTHON:-}" python3.14 python3.13 python3.12 python3; do
      [ -n "$cand" ] && command -v "$cand" >/dev/null 2>&1 && is_py312 "$(command -v "$cand")" && { PYBIN="$(command -v "$cand")"; break; }
    done
    [ -n "$PYBIN" ] || { echo "ERROR: no Python >= 3.12 found (set PYTHON=/path/to/python3.12 or install uv)." >&2; exit 1; }
    "$PYBIN" -m venv "$VENV"
  fi
  "$VENV/bin/python" -m pip install -q -e "$REPO_ROOT[dev]"
fi
echo "OK: $VENV ready ($("$VENV/bin/python" --version))"
