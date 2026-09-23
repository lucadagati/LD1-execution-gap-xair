#!/usr/bin/env bash
# Verify a release checkout: tag present, frozen dataset complete, runtime tests green.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"

TAG="${1:-}"
DATA="$REPO_ROOT/data/execution-gap"
FAIL=0
echo "=== Release verification ==="
if [ -n "$TAG" ]; then
  if git -C "$REPO_ROOT" rev-parse "$TAG^{commit}" >/dev/null 2>&1; then
    echo "OK: tag $TAG -> $(git -C "$REPO_ROOT" rev-parse "$TAG^{commit}")"
  else
    echo "FAIL: tag $TAG missing" >&2; FAIL=1
  fi
fi
for f in environment.txt e1_baselines.csv e1_fpr.csv e9_consistency_sweep.csv e10_toctou.csv e12_scaling.csv \
         e13_faults.csv e14_variants.csv e15_opcua_hil.csv e6_network.csv e6_network_fresh10s.csv paper_metrics_summary.json; do
  [ -f "$DATA/$f" ] || { echo "FAIL: missing $DATA/$f" >&2; FAIL=1; }
done
[ "$FAIL" -eq 0 ] && echo "OK: frozen dataset complete"
if [ -d "$REPO_ROOT/journal" ] && git -C "$REPO_ROOT" ls-files --error-unmatch journal >/dev/null 2>&1; then
  echo "FAIL: journal/ is tracked by git (manuscript must stay local)" >&2; FAIL=1
fi
"$SCRIPTS/verify_artifact.sh" || FAIL=1
[ "$FAIL" -eq 0 ] && echo "=== Release verification PASSED ===" || { echo "=== Release verification FAILED ===" >&2; exit 1; }
