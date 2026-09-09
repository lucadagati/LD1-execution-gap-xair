#!/usr/bin/env bash
# Canonical IEEE TII paper experiment campaign — writes to XAIR experiments/results/.
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
XAIR="$XAIR_ROOT"
export XAIR_URL="${XAIR_URL:-http://127.0.0.1:8080}"

echo "=== Paper campaign (canonical CSV, AIS/XAIR v0.2) ==="

if [ ! -x "$XAIR/.venv/bin/python" ]; then
  echo "[preflight] Creating XAIR venv (required by start_full_stack.sh before it can launch uvicorn)..."
  python3 -m venv "$XAIR/.venv"
  "$XAIR/.venv/bin/pip" install -e "$XAIR[dev]" -q
fi
PY="$XAIR/.venv/bin/python"

"$SCRIPTS/start_full_stack.sh"

mkdir -p "$XAIR/experiments/results"

echo "[preflight] Clearing stale experiment outputs..."
find "$XAIR/experiments/results" -maxdepth 1 -type f \( -name '*.csv' -o -name '*.json' \) ! -name 'environment.txt' -delete

{
  date -u +"utc=%Y-%m-%dT%H:%M:%SZ"
  uname -a
  command -v lscpu >/dev/null && lscpu
  command -v free >/dev/null && free -h
  "$PY" --version
  command -v ros2 >/dev/null && ros2 doctor --report || true
} > "$XAIR/experiments/results/environment.txt"

curl -sf "$XAIR_URL/v1/metrics" >/dev/null
curl -sf http://127.0.0.1:9092/health >/dev/null

echo "[preflight] Contract/runtime unit tests..."
PYTHONPATH="$XAIR" $PY -m unittest discover -s "$XAIR/tests" -p 'test_contract*.py' -v

echo "[1/13] E0 lifecycle..."
$PY "$XAIR/experiments/run_e0_lifecycle.py"

echo "[2/13] E1 baselines (100 runs)..."
$PY "$XAIR/experiments/run_e1_baselines.py" --runs 100 --seed 42 \
  --baselines direct naive local local_stale xair

echo "[3/13] E1 valid-intent FPR (100 completed runs)..."
$PY "$XAIR/experiments/run_e1_fpr.py" --runs 100

echo "[4/13] E4 HTTP load (10000 intents)..."
$PY "$XAIR/experiments/run_e4_http_load.py" --intents 10000

echo "[5/13] E8 gazebo cell (30 runs)..."
$PY "$XAIR/experiments/run_e8_gazebo_cell.py" --runs 30

echo "[6/13] E9 shared context (30 runs)..."
$PY "$XAIR/experiments/run_e9_shared_context.py" --runs 30

echo "[7/13] E9 consistency sweep (10 runs/cell)..."
$PY "$XAIR/experiments/run_e9_consistency_sweep.py" --runs 10 --seed 42

echo "[8/13] E10 TOCTOU (200 runs, delay grid 0/1/3/10/30 ms)..."
$PY "$XAIR/experiments/run_e10_toctou.py" --runs-per-delay 40 --seed 42

echo "[9/13] E11 stratified (100 runs)..."
$PY "$XAIR/experiments/run_e11_stratified.py" --runs 100 --seed 42

echo "[10/13] E12 scaling (100 scored trials/config + warm-up)..."
$PY "$XAIR/experiments/run_e12_scaling.py" --trials 100 --warmup 20 --repetitions 5 --producers 1 10 50 --context-kb 1 64

echo "[11/13] E13 faults..."
$PY "$XAIR/experiments/run_e13_faults.py"

echo "[12/13] E14 variants (100 runs per scenario/baseline)..."
$PY "$XAIR/experiments/run_e14_variants.py" --runs 100 --baselines direct xair local

echo "[13/13] E15 OPC UA HIL (30 runs)..."
$PY "$XAIR/experiments/run_e15_opcua_hil.py" --runs 30

echo "Aggregating metrics and figures..."
$PY "$XAIR/experiments/aggregate_experiment_results.py"
$PY "$XAIR/experiments/plot_results.py"
"$SCRIPTS/sync_paper_outputs.sh"
"$SCRIPTS/clean_runtime.sh"
"$SCRIPTS/verify_artifact.sh"

echo "=== Paper campaign complete ==="
