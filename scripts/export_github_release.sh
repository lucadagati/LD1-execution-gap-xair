#!/usr/bin/env bash
# Build a GitHub release tarball: code + data + repro scripts, NO paper LaTeX.
set -euo pipefail

TAG="${1:-v0.2.2-tii-resubmit}"
OUT="${2:-execution-gap-github-${TAG}.tar.gz}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

git archive "$TAG" \
  XAIR_Runtime \
  scripts \
  data \
  COMMIT.txt \
  REPRODUCE_EXECUTION_GAP.md \
  LICENSE \
  AdaptiX-Quest \
  | gzip -9 > "$OUT"

echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"
echo "Excluded: ResearchTrack/execution-gap-paper/ (LaTeX manuscript — submit separately)"
