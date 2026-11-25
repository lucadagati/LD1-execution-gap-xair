#!/usr/bin/env bash
# Clean-clone audit: fresh checkout -> venv -> tests -> smoke reproduction.
# The clone runs in an isolated environment: none of this checkout's interpreter,
# endpoints, or directories are inherited, and the stack uses dedicated ports and
# a dedicated, fresh Redis container (override with AUDIT_XAIR_PORT, AUDIT_ADAPTER_PORT,
# AUDIT_WS_PORT, AUDIT_REDIS_PORT, or AUDIT_REDIS_URL for an existing server).
#   ./scripts/clean_clone_audit.sh [audit-dir]   (REF=<branch or commit> to audit another ref)
set -euo pipefail
AUDIT_DIR="${1:-$(mktemp -d -t xair-clean-audit-XXXX)}"
REPO_URL="${REPO_URL:-https://github.com/ProSoDiAC/LD1-execution-gap-xair.git}"
REF="${REF:-main}"
AP="${AUDIT_XAIR_PORT:-18080}"; GP="${AUDIT_ADAPTER_PORT:-19092}"; WP="${AUDIT_WS_PORT:-19091}"
# Redis: a fresh, dedicated container (empty database, removed on exit) unless AUDIT_REDIS_URL
# is given, so the audit never writes into a Redis that other services use, and no state
# left by an earlier run can leak into the tests.
AUDIT_REDIS_PORT="${AUDIT_REDIS_PORT:-16379}"
REDIS_IMAGE="redis:7-alpine@sha256:520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7"
if [ -z "${AUDIT_REDIS_URL:-}" ] && command -v docker >/dev/null 2>&1; then
  RC="xair-audit-redis-$$"
  docker run -d --rm --name "$RC" -p "127.0.0.1:$AUDIT_REDIS_PORT:6379" "$REDIS_IMAGE" \
    redis-server --save "" --appendonly no >/dev/null
  trap 'docker rm -f "$RC" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 50); do docker exec "$RC" redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 0.1; done
  RU="redis://127.0.0.1:$AUDIT_REDIS_PORT/0"
else
  RU="${AUDIT_REDIS_URL:-redis://127.0.0.1:6379/1}"
fi

echo "=== Clean-clone audit ($REPO_URL @ $REF) -> $AUDIT_DIR ==="
git clone --quiet "$REPO_URL" "$AUDIT_DIR/repo"
git -C "$AUDIT_DIR/repo" checkout --quiet "$REF"
[ ! -e "$AUDIT_DIR/repo/journal" ] || { echo "FAIL: manuscript present in the public clone" >&2; exit 1; }

clone_env=(env -u PY -u REPO_ROOT -u SCRIPTS -u XAIR_URL -u ADAPTER_URL -u XAIR_RESULTS_DIR -u RUN_DIR
           -u VIRTUAL_ENV -u PYTHONPATH
           XAIR_PORT="$AP" ADAPTER_HTTP_PORT="$GP" ADAPTER_WS_PORT="$WP" REDIS_URL="$RU")
"${clone_env[@]}" "$AUDIT_DIR/repo/scripts/ensure_venv.sh"
CLONE_PY="$AUDIT_DIR/repo/.venv/bin/python"
[ -x "$CLONE_PY" ] || { echo "FAIL: the clone has no virtual environment" >&2; exit 1; }
echo "clone interpreter: $CLONE_PY ($("$CLONE_PY" --version))"
for step in verify_artifact.sh verify_reproduction.sh stop_full_stack.sh; do
  "${clone_env[@]}" PY="$CLONE_PY" "$AUDIT_DIR/repo/scripts/$step"
done
echo "=== Clean-clone audit PASSED ==="
