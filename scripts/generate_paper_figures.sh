#!/usr/bin/env bash
# Generate paper figures from experiment results
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
XAIR="$ROOT/XAIR_Runtime"
cd "$XAIR"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -e ".[dev]" -q
.venv/bin/python experiments/aggregate_experiment_results.py
.venv/bin/python experiments/plot_results.py
echo "Figures written to ResearchTrack/execution-gap-paper/figures/"
