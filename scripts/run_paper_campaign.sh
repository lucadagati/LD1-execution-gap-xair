#!/usr/bin/env bash
# Canonical HTTP campaign behind the paper's tables (~25 min on an idle host).
# E6 (tc netem), E8-Gazebo and E15 need extra infrastructure and are run
# separately (see experiments/EVALUATION.md).
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
[ -x "$REPO_ROOT/.venv/bin/python" ] || "$SCRIPTS/ensure_venv.sh"
source "$SCRIPTS/_resolve_layout.sh"
X="$REPO_ROOT/experiments"
export XAIR_URL ADAPTER_URL XAIR_RESULTS_DIR

echo "=== Paper campaign -> $XAIR_RESULTS_DIR ==="
"$SCRIPTS/start_full_stack.sh"
mkdir -p "$XAIR_RESULTS_DIR"
find "$XAIR_RESULTS_DIR" -maxdepth 1 -type f \( -name '*.csv' -o -name '*.json' -o -name '*.txt' \) -delete

{
  date -u +"utc=%Y-%m-%dT%H:%M:%SZ"
  uname -a
  command -v lscpu >/dev/null && lscpu
  command -v free >/dev/null && free -h
  "$PY" --version
  "$PY" -c "import importlib.metadata as m; print(' '.join(f'{p}=={m.version(p)}' for p in ['fastapi','uvicorn','pydantic','jsonschema','redis']))"
  echo "redis_url=${REDIS_URL-redis://127.0.0.1:6379/0}"
  command -v ros2 >/dev/null && ros2 doctor --report || echo "ros2=absent"
} > "$XAIR_RESULTS_DIR/environment.txt"

echo "[preflight] unit tests"
(cd "$REPO_ROOT" && "$PY" -m pytest -q tests)

step() { echo "[$1] $2"; }
step 1/14 "E0 lifecycle";               "$PY" "$X/run_e0_lifecycle.py" >/dev/null
step 2/14 "E1 baselines";               "$PY" "$X/run_e1_baselines.py" --runs 100 --seed 42
step 3/14 "E1 valid-intent FPR";        "$PY" "$X/run_e1_fpr.py" --runs 100
step 4/14 "E3 conflict";                "$PY" "$X/run_e3_http_stack.py" --runs 100
step 5/14 "E4 HTTP load";               "$PY" "$X/run_e4_http_load.py" --intents 10000
step 6/14 "E9 consistency sweep";       "$PY" "$X/run_e9_consistency_sweep.py" --runs 10
step 7/14 "E10 TOCTOU (0-30 ms)";       "$PY" "$X/run_e10_toctou.py" --runs-per-delay 40 --seed 42
step 8/14 "E10 boundary (30-100 ms)";   "$PY" "$X/run_e10_toctou.py" --runs-per-delay 40 --seed 43 \
                                          --offsets-ms 30 40 45 50 55 60 100 --out "$XAIR_RESULTS_DIR/e10_toctou_boundary.csv"
for s in 42 7 123; do
  step 9/14 "E11 stratified seed $s";   "$PY" "$X/run_e11_stratified.py" --runs 100 --seed "$s"
done
step 10/14 "E12 scaling";               "$PY" "$X/run_e12_scaling.py" --trials 100 --warmup 20 --repetitions 5 --producers 1 10 50 --context-kb 1 64
step 11/14 "E13 faults";                "$PY" "$X/run_e13_faults.py"
step 12/14 "E14 action classes";        "$PY" "$X/run_e14_variants.py" --runs 100
step 13/14 "aggregate";                 "$PY" "$X/aggregate_experiment_results.py" >/dev/null
step 14/14 "figures";                   "$PY" "$X/plot_results.py"

echo "=== Campaign complete. Freeze it with scripts/sync_paper_outputs.sh ==="
