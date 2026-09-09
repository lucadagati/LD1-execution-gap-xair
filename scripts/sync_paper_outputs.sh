#!/usr/bin/env bash
# Sync frozen campaign data to data/execution-gap/, metrics, paper figures, and COMMIT.txt (repo root).
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"

PAPER="$REPO_ROOT/ResearchTrack/execution-gap-paper"
DATA="$REPO_ROOT/data/execution-gap"
SRC="$XAIR_ROOT/experiments/results"
PY="${XAIR_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY=python3

CAMPAIGN_FILES=(
  environment.txt
  e0_lifecycle.json
  e1_baselines.csv
  e1_fpr.csv
  e4_load_http.csv
  e4_load_http_detail.csv
  e8_gazebo_cell.csv
  e9_shared_context.csv
  e9_consistency_sweep.csv
  e10_toctou.csv
  e11_stratified.csv
  e12_scaling.csv
  e13_faults.csv
  e14_variants.csv
  e15_opcua_hil.csv
  ros_audit_state.json
)

mkdir -p "$DATA" "$PAPER/figures"
rm -f "$DATA"/*
for name in "${CAMPAIGN_FILES[@]}"; do
  if [ -f "$SRC/$name" ]; then
    cp -f "$SRC/$name" "$DATA/$name"
  fi
done

"$PY" "$XAIR_ROOT/experiments/aggregate_experiment_results.py" \
  --out "$DATA/paper_metrics_summary.json"
"$PY" "$XAIR_ROOT/experiments/plot_results.py" --out "$PAPER/figures"

if git -C "$REPO_ROOT" rev-parse HEAD >/dev/null 2>&1; then
  RELEASE_REF="HEAD"
  if git -C "$REPO_ROOT" rev-parse "v0.2.2-tii-resubmit^{commit}" >/dev/null 2>&1; then
    RELEASE_REF="v0.2.2-tii-resubmit^{commit}"
  fi
  {
    echo "commit=$(git -C "$REPO_ROOT" rev-parse "$RELEASE_REF")"
    echo "describe=$(git -C "$REPO_ROOT" describe --tags --always "$RELEASE_REF" 2>/dev/null || echo none)"
    echo "tag=v0.2.2-tii-resubmit"
    echo "repository=https://github.com/lucadagati/adaptix"
  } > "$REPO_ROOT/COMMIT.txt"
else
  echo "no-git" > "$REPO_ROOT/COMMIT.txt"
fi
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$REPO_ROOT/SYNC_TIMESTAMP.txt"

echo "Synced data/execution-gap/, figures/, and COMMIT.txt (repo root)"
