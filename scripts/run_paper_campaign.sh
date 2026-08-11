#!/usr/bin/env bash
# Canonical IEEE TII paper experiment campaign — writes to XAIR experiments/results/.
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
XAIR="$XAIR_ROOT"
export XAIR_URL="${XAIR_URL:-http://127.0.0.1:8080}"

echo "=== Paper campaign (canonical CSV) ==="
"$SCRIPTS/start_full_stack.sh"

if [ ! -x "$XAIR/.venv/bin/python" ]; then
  python3 -m venv "$XAIR/.venv"
  "$XAIR/.venv/bin/pip" install -e "$XAIR[dev]" -q
fi
PY="$XAIR/.venv/bin/python"

curl -sf "$XAIR_URL/v1/metrics" >/dev/null
curl -sf http://127.0.0.1:9092/health >/dev/null

echo "[1/12] E0 lifecycle..."
$PY "$XAIR/experiments/run_e0_lifecycle.py"

echo "[2/12] E1 baselines (100 runs)..."
$PY "$XAIR/experiments/run_e1_baselines.py" --runs 100 --seed 42 \
  --baselines direct naive local local_stale xair

echo "[3/12] E4 HTTP load (10000 intents)..."
$PY "$XAIR/experiments/run_e4_http_load.py" --intents 10000

echo "[4/12] E8 gazebo cell (30 runs)..."
$PY "$XAIR/experiments/run_e8_gazebo_cell.py" --runs 30

echo "[5/12] E9 shared context (30 runs)..."
$PY "$XAIR/experiments/run_e9_shared_context.py" --runs 30

echo "[6/12] E9 consistency sweep (10 runs/cell)..."
$PY "$XAIR/experiments/run_e9_consistency_sweep.py" --runs 10 --seed 42

echo "[7/12] E10 TOCTOU (50 runs)..."
$PY "$XAIR/experiments/run_e10_toctou.py" --runs 50 --seed 42

echo "[8/12] E11 stratified (100 runs)..."
$PY "$XAIR/experiments/run_e11_stratified.py" --runs 100 --seed 42

echo "[9/12] E12 scaling (100 trials/config)..."
$PY "$XAIR/experiments/run_e12_scaling.py" --trials 100 --producers 1 10 50 --context-kb 1 64

echo "[10/12] E13 faults..."
$PY "$XAIR/experiments/run_e13_faults.py"

echo "[11/12] E14 variants (30 runs)..."
$PY "$XAIR/experiments/run_e14_variants.py" --runs 30 --baselines direct xair local

echo "[12/12] E15 OPC UA HIL (30 runs)..."
$PY "$XAIR/experiments/run_e15_opcua_hil.py" --runs 30

echo "Aggregating metrics and figures..."
$PY "$XAIR/experiments/aggregate_experiment_results.py"
$PY "$XAIR/experiments/plot_results.py"
"$SCRIPTS/sync_paper_artifact.sh"

echo "=== Paper campaign complete ==="
