#!/usr/bin/env bash
# Generate evaluation plots from experiment CSVs.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
cd "$XAIR_ROOT"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -e ".[dev]" -q
fi
.venv/bin/python experiments/aggregate_experiment_results.py
.venv/bin/python experiments/plot_results.py --out experiments/plots
echo "Figures written to $XAIR_ROOT/experiments/plots/"
