#!/usr/bin/env bash
# Remove ephemeral venv, logs, and PID files before GitHub release packaging.
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"

echo "=== Cleaning ephemeral runtime state ==="
rm -rf "$XAIR_ROOT/.venv" "$REPO_ROOT/.run"
find "$XAIR_ROOT" "$SCRIPTS" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$XAIR_ROOT" "$SCRIPTS" -type d -name '*.egg-info' -prune -exec rm -rf {} + 2>/dev/null || true
find "$XAIR_ROOT" "$SCRIPTS" -type f \( -name '*.pid' -o -name '*.log' \) -delete 2>/dev/null || true
echo "OK: cleaned."
