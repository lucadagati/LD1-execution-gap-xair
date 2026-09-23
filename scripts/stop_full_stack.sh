#!/usr/bin/env bash
# Stop the processes started by start_full_stack.sh (PID files only; other services are left alone).
set -uo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"

echo "=== Stop stack ==="
for name in adapter xair ros_audit rosbridge gazebo_cell motion_tracker; do
  pidfile="$RUN_DIR/$name.pid"
  [ -f "$pidfile" ] || continue
  pid="$(cat "$pidfile")"
  # Services run in their own session (setsid), so their process group never
  # includes the caller; kill it so wrapper children (run_with_ros.sh -> python) go too.
  pgid="$(ps -o pgid= "$pid" 2>/dev/null | tr -d ' ')"
  if [ -n "$pgid" ] && [ "$pgid" != "$(ps -o pgid= $$ | tr -d ' ')" ]; then
    kill -- "-$pgid" 2>/dev/null || true
  else
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
  echo "  stopped $name (pid $pid)"
done
pkill -f "$SCRIPTS/adaptix_quest_adapter.py" 2>/dev/null || true
pkill -f "uvicorn xair.adapters.http_server:app --host 127.0.0.1 --port $XAIR_PORT" 2>/dev/null || true
if [ "${STOP_REDIS:-0}" = "1" ] && command -v docker &>/dev/null; then
  docker compose -f "$REPO_ROOT/docker-compose.yml" stop redis 2>/dev/null || true
fi
echo "Done."
