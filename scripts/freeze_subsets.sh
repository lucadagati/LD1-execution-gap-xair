#!/usr/bin/env bash
# Freeze the separate campaigns (reserved-core E4/E12 in pinned/, the five-node
# testbed in distributed/) into data/execution-gap/<subset>/ and recompute the
# summary. The main campaign's top-level files are left untouched.
#   ./scripts/freeze_subsets.sh [--no-summary] [subset ...]   (default: pinned distributed)
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
DATA="$REPO_ROOT/data/execution-gap"
SRC="$XAIR_RESULTS_DIR"

summary=1
if [ "${1:-}" = "--no-summary" ]; then summary=0; shift; fi
subsets=("$@")
[ "${#subsets[@]}" -gt 0 ] || subsets=(pinned distributed)

for sub in "${subsets[@]}"; do
  [ -d "$SRC/$sub" ] || { echo "skip $sub: $SRC/$sub not found"; continue; }
  [ -f "$SRC/$sub/environment.txt" ] || { echo "Refusing to freeze $sub without environment.txt" >&2; exit 1; }
  rm -rf "${DATA:?}/$sub"
  # every campaign directory under the subset (campaigns/cN, sensitivity/<name>) keeps its layout
  (cd "$SRC/$sub" && find . \( -name '*.csv' -o -name environment.txt \) -print0 | while IFS= read -r -d '' f; do
      mkdir -p "$DATA/$sub/$(dirname "$f")" && cp -f "$f" "$DATA/$sub/$f"; done)
  echo "Frozen $sub -> $DATA/$sub"
done

if [ "$summary" -eq 1 ]; then
  "$PY" "$REPO_ROOT/experiments/aggregate_experiment_results.py" --results "$DATA" \
    --out "$DATA/paper_metrics_summary.json" >/dev/null
  if [ -d "$REPO_ROOT/journal" ]; then
    "$PY" "$REPO_ROOT/experiments/plot_results.py" --results "$DATA" --out "$REPO_ROOT/journal/figures"
    "$PY" "$REPO_ROOT/experiments/make_paper_tables.py" --summary "$DATA/paper_metrics_summary.json" --out "$REPO_ROOT/journal/generated"
  fi
fi
