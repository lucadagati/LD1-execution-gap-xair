#!/usr/bin/env bash
# Clean-clone audit: fresh checkout -> venv -> tests -> smoke reproduction.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
AUDIT_DIR="${1:-$(mktemp -d -t xair-clean-audit-XXXX)}"
REPO_URL="${REPO_URL:-https://github.com/lucadagati/LD1-execution-gap-xair.git}"
REF="${REF:-main}"

echo "=== Clean-clone audit ($REPO_URL @ $REF) -> $AUDIT_DIR ==="
git clone --quiet "$REPO_URL" "$AUDIT_DIR/repo"
git -C "$AUDIT_DIR/repo" checkout --quiet "$REF"
[ ! -e "$AUDIT_DIR/repo/journal" ] || { echo "FAIL: manuscript present in the public clone" >&2; exit 1; }
"$AUDIT_DIR/repo/scripts/ensure_venv.sh"
"$AUDIT_DIR/repo/scripts/verify_artifact.sh"
"$AUDIT_DIR/repo/scripts/verify_reproduction.sh"
"$AUDIT_DIR/repo/scripts/stop_full_stack.sh"
echo "=== Clean-clone audit PASSED ==="
