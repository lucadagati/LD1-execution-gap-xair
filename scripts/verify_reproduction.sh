#!/usr/bin/env bash
# Fast smoke reproduction into a scratch directory; never touches experiments/results or data/.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
export XAIR_RESULTS_DIR="${VERIFY_RESULTS_DIR:-$(mktemp -d -t xair-verify-XXXX)}"
X="$REPO_ROOT/experiments"
echo "=== Smoke reproduction -> $XAIR_RESULTS_DIR ==="
"$SCRIPTS/start_full_stack.sh"
"$PY" "$X/run_e0_lifecycle.py" >/dev/null
"$PY" "$X/run_e1_baselines.py" --runs 5 --baselines direct xair
"$PY" "$X/run_e9_consistency_sweep.py" --runs 2
"$PY" "$X/run_e10_toctou.py" --runs-per-delay 8
"$PY" "$X/run_e12_scaling.py" --trials 10 --warmup 2 --repetitions 1 --producers 1 --context-kb 1
"$PY" "$X/run_e13_faults.py" --runs-per-fault 3 --malformed-count 10
"$PY" "$X/aggregate_experiment_results.py" --results "$XAIR_RESULTS_DIR" --out "$XAIR_RESULTS_DIR/summary.json" >/dev/null
echo "=== Smoke reproduction PASSED ($XAIR_RESULTS_DIR) ==="
