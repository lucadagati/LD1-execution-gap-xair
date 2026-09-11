#!/usr/bin/env bash
# Remove ephemeral runtime state from the paper artifact bundle before release/tag.
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"

ART_CODE="$ADAPTIX_ROOT"
XAIR="$XAIR_ROOT"

echo "=== Cleaning ephemeral artifact state under $ART_CODE ==="

rm -rf "$XAIR/.venv" "$ART_CODE/.run" "$ART_CODE/AdaptiX-Quest/TestResults"
find "$ART_CODE" "$XAIR" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$ART_CODE" "$XAIR" -type d -name '*.egg-info' -prune -exec rm -rf {} + 2>/dev/null || true
find "$ART_CODE" "$XAIR" -type f \( -name '*.pid' -o -name '*.log' \) -delete 2>/dev/null || true

# Keep campaign CSVs; drop regenerated local experiment scratch only when syncing separately.
rm -f "$XAIR/experiments/results/e15_transport.log" 2>/dev/null || true

echo "OK: artifact cleaned for release packaging."
