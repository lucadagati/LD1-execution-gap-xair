#!/usr/bin/env bash
# Start the evaluation stack: Redis (docker, optional) + XAIR + actuator gateway (+ ROS witness/rosbridge if ROS 2 is installed).
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
mkdir -p "$RUN_DIR" "$XAIR_RESULTS_DIR"

# Empty REDIS_URL selects the in-memory store (single XAIR process only).
export REDIS_URL="${REDIS_URL-redis://127.0.0.1:6379/0}"
export ROS_AUDIT_FILE="${ROS_AUDIT_FILE:-$XAIR_RESULTS_DIR/ros_audit_state.json}"

echo "=== XAIR evaluation stack ==="
echo "  repo:    $REPO_ROOT"
echo "  XAIR:    $XAIR_URL   gateway: $ADAPTER_URL   redis: ${REDIS_URL:-<in-memory>}"

if [ -n "$REDIS_URL" ] && command -v docker &>/dev/null && [ -f "$REPO_ROOT/docker-compose.yml" ]; then
  if ! docker compose -f "$REPO_ROOT/docker-compose.yml" ps redis 2>/dev/null | grep -q -i "up\|running"; then
    echo "Starting Redis (docker compose)..."
    docker compose -f "$REPO_ROOT/docker-compose.yml" up -d redis 2>/dev/null || true
  fi
fi

wait_http() {  # url label
  for _ in $(seq 1 25); do
    if curl -sf "$1" >/dev/null; then echo "[OK] $2"; return 0; fi
    sleep 0.2
  done
  echo "[FAIL] $2 not reachable at $1" >&2
  return 1
}

if curl -sf "$XAIR_URL/v1/metrics" >/dev/null 2>&1; then
  echo "[OK] XAIR already running at $XAIR_URL"
else
  echo "Starting XAIR on :$XAIR_PORT..."
  # exec: the background job *is* the server (its PID is recorded and no
  # subshell keeps the caller's stdout open, so piping this script works).
  (cd "$REPO_ROOT" && exec setsid nohup env REDIS_URL="$REDIS_URL" PYTHONPATH="$REPO_ROOT" \
    "$PY" -m uvicorn xair.adapters.http_server:app --host 127.0.0.1 --port "$XAIR_PORT" \
    > "$RUN_DIR/xair.log" 2>&1 < /dev/null) &
  echo $! > "$RUN_DIR/xair.pid"
  wait_http "$XAIR_URL/v1/metrics" "XAIR /v1/metrics"
fi

# The gateway always starts. Without ROS 2 it runs HTTP-only (ros_published is
# false; gateway_released, the measured endpoint, is unaffected).
if curl -sf "$ADAPTER_URL/health" >/dev/null 2>&1; then
  echo "[OK] gateway already running at $ADAPTER_URL"
else
  echo "Starting actuator gateway on :$ADAPTER_HTTP_PORT (ws :$ADAPTER_WS_PORT)..."
  # PYTHONPATH_PREPEND (not PYTHONPATH) so run_with_ros.sh merges it with
  # rclpy's site-packages instead of clobbering them.
  setsid nohup env XAIR_URL="$XAIR_URL" PYTHONPATH_PREPEND="$REPO_ROOT:$SCRIPTS" \
    "$SCRIPTS/run_with_ros.sh" "$PY" "$SCRIPTS/adaptix_quest_adapter.py" "$ADAPTER_WS_PORT" "$ADAPTER_HTTP_PORT" \
    > "$RUN_DIR/adapter.log" 2>&1 < /dev/null &
  echo $! > "$RUN_DIR/adapter.pid"
  wait_http "$ADAPTER_URL/health" "gateway /health"
fi

if [ -f /opt/ros/jazzy/setup.bash ]; then
  set +u
  # shellcheck disable=SC1091
  source /opt/ros/jazzy/setup.bash
  set -u
  if ! pgrep -f "$SCRIPTS/ros_audit_subscriber.py" >/dev/null; then
    echo "Starting ROS audit witness..."
    setsid nohup env ROS_AUDIT_FILE="$ROS_AUDIT_FILE" PYTHONPATH_PREPEND="$REPO_ROOT:$SCRIPTS" \
      "$SCRIPTS/run_with_ros.sh" python3 "$SCRIPTS/ros_audit_subscriber.py" \
      > "$RUN_DIR/ros_audit.log" 2>&1 < /dev/null &
    echo $! > "$RUN_DIR/ros_audit.pid"
  fi
  if ! pgrep -f "rosbridge_websocket" >/dev/null; then
    echo "Starting rosbridge :${ROSBRIDGE_PORT:-9090}..."
    setsid nohup "$SCRIPTS/run_with_ros.sh" ros2 launch rosbridge_server rosbridge_websocket_launch.xml \
      port:="${ROSBRIDGE_PORT:-9090}" address:=0.0.0.0 > "$RUN_DIR/rosbridge.log" 2>&1 < /dev/null &
    echo $! > "$RUN_DIR/rosbridge.pid"
  fi
else
  echo "[INFO] ROS 2 Jazzy not installed: HTTP-only gateway, no ROS witness"
fi

echo "Stack ready. Logs: $RUN_DIR/   E2E check: $SCRIPTS/verify_e2e.sh"
