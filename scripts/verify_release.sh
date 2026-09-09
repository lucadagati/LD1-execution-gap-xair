#!/usr/bin/env bash
# Verify GitHub release tag, COMMIT.txt, runtime, and frozen data layout.
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"

TAG="v0.2.2-tii-resubmit"
COMMIT_FILE="$REPO_ROOT/COMMIT.txt"
DATA="$REPO_ROOT/data/execution-gap"
FAIL=0

echo "=== Release verification (GitHub bundle) ==="

if ! git -C "$REPO_ROOT" rev-parse "$TAG^{commit}" >/dev/null 2>&1; then
  echo "FAIL: tag $TAG missing" >&2
  FAIL=1
else
  TAG_COMMIT="$(git -C "$REPO_ROOT" rev-parse "$TAG^{commit}")"
  echo "OK: tag $TAG -> $TAG_COMMIT"
fi

if [ ! -f "$COMMIT_FILE" ]; then
  echo "FAIL: missing $COMMIT_FILE" >&2
  FAIL=1
else
  DECLARED="$(grep '^commit=' "$COMMIT_FILE" | cut -d= -f2)"
  if [ -n "${TAG_COMMIT:-}" ] && [ "$DECLARED" != "$TAG_COMMIT" ]; then
    if git -C "$REPO_ROOT" merge-base --is-ancestor "$DECLARED" "$TAG_COMMIT" 2>/dev/null; then
      echo "OK: COMMIT.txt commit=$DECLARED (release ancestor of tag $TAG_COMMIT)"
    else
      echo "FAIL: COMMIT.txt commit=$DECLARED vs tag $TAG_COMMIT" >&2
      FAIL=1
    fi
  else
    echo "OK: COMMIT.txt commit=$DECLARED"
  fi
fi

if [ ! -d "$XAIR_ROOT/xair/core" ] || [ ! -d "$XAIR_ROOT/xair/adapters" ]; then
  echo "FAIL: incomplete XAIR_Runtime/xair package" >&2
  FAIL=1
else
  echo "OK: XAIR_Runtime/xair package complete"
fi

if [ ! -f "$DATA/e10_toctou.csv" ] || [ ! -f "$DATA/e4_load_http.csv" ]; then
  echo "FAIL: missing frozen campaign CSVs under data/execution-gap/" >&2
  FAIL=1
else
  echo "OK: data/execution-gap/ campaign CSVs present"
fi

if [ "$FAIL" -ne 0 ]; then
  echo "=== Release verification FAILED ===" >&2
  exit 1
fi

"$SCRIPTS/verify_artifact.sh"
TRACKED="$(git -C "$REPO_ROOT" ls-files "$XAIR_ROOT/xair/core" | wc -l)"
if [ "$TRACKED" -lt 5 ]; then
  echo "FAIL: XAIR_Runtime/xair/core not tracked in git ($TRACKED files)" >&2
  exit 1
fi
echo "OK: xair/core tracked ($TRACKED files)"
echo "=== Release verification PASSED ==="
