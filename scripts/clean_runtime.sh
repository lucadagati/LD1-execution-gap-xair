#!/usr/bin/env bash
# Remove regenerable local state (caches, logs, PID files). Keeps .venv unless CLEAN_VENV=1.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
echo "=== Cleaning local runtime state under $REPO_ROOT ==="
rm -rf "$RUN_DIR" "$REPO_ROOT/.pytest_cache"
[ "${CLEAN_VENV:-0}" = "1" ] && rm -rf "$REPO_ROOT/.venv"
find "$REPO_ROOT" -path "$REPO_ROOT/.venv" -prune -o -type d \( -name '__pycache__' -o -name '*.egg-info' \) -prune -exec rm -rf {} + 2>/dev/null || true
echo "OK: cleaned."
