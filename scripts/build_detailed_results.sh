#!/usr/bin/env bash
# Builds docs/detailed-results/detailed_results.pdf: protocols, per-condition tables,
# and detailed results behind the paper's evaluation, regenerated from the frozen
# per-trial data in data/execution-gap/ (tables, figures, then pdflatex via latexmk).
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
DOC="$REPO_ROOT/docs/detailed-results"
SUMMARY="$REPO_ROOT/data/execution-gap/paper_metrics_summary.json"
mkdir -p "$DOC/generated" "$DOC/figures"
"$PY" "$REPO_ROOT/experiments/make_paper_tables.py" --summary "$SUMMARY" --out "$DOC/generated" >/dev/null
"$PY" "$REPO_ROOT/experiments/plot_results.py" --results "$REPO_ROOT/data/execution-gap" --out "$DOC/figures" >/dev/null
(cd "$DOC" && latexmk -pdf -interaction=nonstopmode -halt-on-error detailed_results.tex >/dev/null 2>&1 && latexmk -c >/dev/null 2>&1)
echo "Detailed results -> $DOC/detailed_results.pdf ($(pdfinfo "$DOC/detailed_results.pdf" | awk '/Pages/{print $2}') pages)"
