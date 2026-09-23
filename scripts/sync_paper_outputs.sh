#!/usr/bin/env bash
# Freeze the latest campaign into data/execution-gap/, recompute the summary, and
# regenerate the paper figures (journal/figures/, local only).
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
DATA="$REPO_ROOT/data/execution-gap"
SRC="$XAIR_RESULTS_DIR"

CAMPAIGN_FILES=(
  environment.txt e0_lifecycle.json e1_baselines.csv e1_fpr.csv e3_conflict_http.csv
  e4_load_http.csv e9_consistency_sweep.csv e10_toctou.csv e10_toctou_boundary.csv
  e11_stratified_seed42.csv e11_stratified_seed7.csv e11_stratified_seed123.csv
  e12_scaling.csv e13_faults.csv e14_variants.csv e16_context_churn.csv
)

missing=0
for name in "${CAMPAIGN_FILES[@]}"; do
  [ -f "$SRC/$name" ] || { echo "MISSING: $SRC/$name" >&2; missing=1; }
done
[ "$missing" -eq 0 ] || { echo "Refusing to freeze an incomplete campaign." >&2; exit 1; }

# Suites that need extra infrastructure (netns/netem, ROS 2 + Gazebo, OPC UA);
# frozen when present.
OPTIONAL_GLOBS=(e6_network*.csv e8_gazebo_campaign*.csv e15_opcua_hil.csv)

mkdir -p "$DATA"
find "$DATA" -maxdepth 1 -type f -delete   # sub-directories (legacy host data) are kept
for name in "${CAMPAIGN_FILES[@]}"; do cp -f "$SRC/$name" "$DATA/$name"; done
shopt -s nullglob
for pattern in "${OPTIONAL_GLOBS[@]}"; do
  for f in "$SRC"/$pattern; do cp -f "$f" "$DATA/"; done
done
shopt -u nullglob

"$PY" "$REPO_ROOT/experiments/aggregate_experiment_results.py" --results "$DATA" \
  --out "$DATA/paper_metrics_summary.json" >/dev/null
if [ -d "$REPO_ROOT/journal" ]; then
  "$PY" "$REPO_ROOT/experiments/plot_results.py" --results "$DATA" --out "$REPO_ROOT/journal/figures"
  "$PY" "$REPO_ROOT/experiments/make_paper_tables.py" --summary "$DATA/paper_metrics_summary.json" --out "$REPO_ROOT/journal/generated"
fi

echo "Frozen campaign in $DATA"
