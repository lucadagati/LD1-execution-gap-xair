#!/usr/bin/env bash
# Fast reproduction smoke test — does NOT overwrite canonical paper CSVs.
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
XAIR="$XAIR_ROOT"
VERIFY_DIR="${VERIFY_RESULTS_DIR:-/tmp/adaptix-verify-results}"
export XAIR_URL="${XAIR_URL:-http://127.0.0.1:8080}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"

echo "=== AdaptiX reproduction verifier (smoke; results -> $VERIFY_DIR) ==="
mkdir -p "$VERIFY_DIR"

if pgrep -f "run_e8_gazebo_cell.py --runs" >/dev/null 2>&1; then
  echo "ERROR: E8 Gazebo run in progress" >&2
  exit 1
fi

"$SCRIPTS/start_full_stack.sh"

if [ ! -x "$XAIR/.venv/bin/python" ]; then
  python3 -m venv "$XAIR/.venv"
fi
"$XAIR/.venv/bin/pip" install -e "$XAIR[dev]" -q
PY="$XAIR/.venv/bin/python"

curl -sf "$XAIR_URL/v1/metrics" >/dev/null
curl -sf http://127.0.0.1:9092/health >/dev/null

CANON="$XAIR/experiments/results"
mkdir -p "$CANON"
BACKUP="$VERIFY_DIR/canon-backup-$$"
mkdir -p "$BACKUP"
cp -a "$CANON"/*.csv "$BACKUP/" 2>/dev/null || true
cp -a "$CANON"/*.json "$BACKUP/" 2>/dev/null || true

restore_canon() {
  cp -a "$BACKUP"/*.csv "$CANON/" 2>/dev/null || true
  cp -a "$BACKUP"/*.json "$CANON/" 2>/dev/null || true
  rm -rf "$BACKUP"
}
trap restore_canon EXIT

echo "[1/8] E0..."
$PY "$XAIR/experiments/run_e0_lifecycle.py" | grep -q '"passed": 7'

echo "[2/8] E1 spot (5 runs)..."
$PY "$XAIR/experiments/run_e1_baselines.py" --runs 5 --seed 1 --baselines xair local

echo "[3/8] E9 sweep spot (2 runs/cell)..."
$PY "$XAIR/experiments/run_e9_consistency_sweep.py" --runs 2 --seed 1

echo "[4/8] E10 spot (10 runs)..."
$PY "$XAIR/experiments/run_e10_toctou.py" --runs 10 --seed 1

echo "[5/8] E12 spot..."
$PY "$XAIR/experiments/run_e12_scaling.py" --trials 10 --producers 1 --context-kb 1

echo "[6/8] E13..."
$PY "$XAIR/experiments/run_e13_faults.py"

echo "[7/8] E15 spot (5 runs)..."
$PY "$XAIR/experiments/run_e15_opcua_hil.py" --runs 5

echo "[8/8] Aggregate smoke..."
$PY "$XAIR/experiments/aggregate_experiment_results.py" >/dev/null

echo "=== Reproduction verifier PASSED (canonical CSV restored) ==="
