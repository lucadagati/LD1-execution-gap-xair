#!/usr/bin/env bash
# Verify this repository's release tag, commit, and canonical result files.
# Run from within a checkout of the published (flat) repo, e.g.:
#   git clone https://github.com/lucadagati/XAIR_eXecution-time_Action_Intent_Runtime.git
#   cd XAIR_eXecution-time_Action_Intent_Runtime && ./scripts/verify_release.sh [tag]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-v0.2.3-tii-resubmit}"
RESULTS="$ROOT/experiments/results"
FAIL=0

echo "=== Release verification ($TAG) ==="

if ! git -C "$ROOT" rev-parse "$TAG^{commit}" >/dev/null 2>&1; then
  echo "FAIL: tag $TAG missing" >&2
  FAIL=1
else
  echo "OK: tag $TAG -> $(git -C "$ROOT" rev-parse "$TAG^{commit}")"
fi

if [ ! -d "$ROOT/xair/core" ] || [ ! -d "$ROOT/xair/adapters" ]; then
  echo "FAIL: incomplete xair/ package" >&2
  FAIL=1
else
  echo "OK: xair/ package complete"
fi

for f in e0_lifecycle.json e1_baselines.csv e4_load_http.csv e10_toctou.csv \
         e11_stratified.csv e12_scaling.csv e13_faults.csv e14_variants.csv \
         e15_opcua_hil.csv paper_metrics_summary.json; do
  if [ ! -f "$RESULTS/$f" ]; then
    echo "FAIL: missing canonical result $RESULTS/$f" >&2
    FAIL=1
  fi
done
[ "$FAIL" -eq 0 ] && echo "OK: canonical experiments/results/ present"

if [ "$FAIL" -ne 0 ]; then
  echo "=== Release verification FAILED ===" >&2
  exit 1
fi

"$ROOT/scripts/verify_artifact.sh"

TRACKED="$(git -C "$ROOT" ls-files xair/core | wc -l)"
if [ "$TRACKED" -lt 5 ]; then
  echo "FAIL: xair/core not tracked in git ($TRACKED files)" >&2
  exit 1
fi
echo "OK: xair/core tracked ($TRACKED files)"
echo "=== Release verification PASSED ==="
