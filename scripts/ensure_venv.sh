#!/usr/bin/env bash
# Create the repository venv and install the project with its dev extras (used by CI and fresh clones).
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
  python3 -m venv "$REPO_ROOT/.venv"
fi
"$REPO_ROOT/.venv/bin/python" -m pip install -q -e "$REPO_ROOT[dev]"
echo "OK: $REPO_ROOT/.venv ready"
