#!/usr/bin/env bash
# Rebuild the paper's summary, generated tables, macros and figures from the published per-trial CSVs in
# data/execution-gap/ into a separate directory. Read-only on data/ and journal/; nothing is frozen or deleted.
#   ./scripts/regenerate_from_data.sh [OUT_DIR]      (default: a new temp dir)
# The generated files are compared byte by byte with journal/generated/ (or, without journal/, with
# docs/detailed-results/generated/).
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
DATA="$REPO_ROOT/data/execution-gap"
OUT="${1:-$(mktemp -d -t xair-regen-XXXX)}"
mkdir -p "$OUT/generated" "$OUT/figures"
"$PY" "$REPO_ROOT/experiments/aggregate_experiment_results.py" --results "$DATA" --out "$OUT/paper_metrics_summary.json" >/dev/null
"$PY" "$REPO_ROOT/experiments/make_paper_tables.py" --summary "$OUT/paper_metrics_summary.json" --out "$OUT/generated" >/dev/null
"$PY" "$REPO_ROOT/experiments/analyze_review.py" --results "$DATA/review" --out "$OUT/generated" >/dev/null
"$PY" "$REPO_ROOT/experiments/plot_key_results.py" --summary "$OUT/paper_metrics_summary.json" --out "$OUT/figures/key_results.pdf" >/dev/null
"$PY" "$REPO_ROOT/experiments/plot_graphical_abstract.py" --summary "$OUT/paper_metrics_summary.json" --out "$OUT/figures/graphical_abstract.png" >/dev/null
echo "Regenerated from $DATA -> $OUT"
# reference: the manuscript's tables when journal/ is present, else the copies in docs/detailed-results/generated
REF="$REPO_ROOT/journal/generated"; [ -d "$REF" ] || REF="$REPO_ROOT/docs/detailed-results/generated"
total=0; same=0; differ=0; noref=0
for f in "$OUT"/generated/*; do
  total=$((total + 1)); r="$REF/$(basename "$f")"
  if [ ! -f "$r" ]; then noref=$((noref + 1)); echo "NO REFERENCE: $(basename "$f")"; continue; fi
  if cmp -s "$f" "$r"; then same=$((same + 1)); else differ=$((differ + 1)); echo "DIFFERS: $(basename "$f")"; fi
done
echo "$total generated, $((same + differ)) compared: $same identical, $differ different; $noref without reference (reference: ${REF#"$REPO_ROOT"/})"
echo "Figures are regenerated in $OUT/figures but not compared (PDF metadata carry the creation date)."
[ "$differ" -eq 0 ]
