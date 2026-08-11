#!/usr/bin/env bash
# Avvia stack completo: Redis (optional) + XAIR + AdaptiX adapter + ROSBridge
set -euo pipefail

# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
PID_DIR="$ADAPTIX_ROOT/.run"
mkdir -p "$PID_DIR"

export XAIR_URL="${XAIR_URL:-http://127.0.0.1:8080}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"

echo "=== AdaptiX + XAIR full stack ==="
echo "  layout: ADAPTIX_ROOT=$ADAPTIX_ROOT"
echo "  XAIR:   $XAIR_ROOT"

# Redis (optional via docker)
if command -v docker &>/dev/null && [ -f "$XAIR_ROOT/docker-compose.yml" ]; then
  if docker compose -f "$XAIR_ROOT/docker-compose.yml" ps redis 2>/dev/null | grep -q running; then
    echo "[OK] Redis già in esecuzione"
  else
    echo "Avvio Redis (docker)..."
    docker compose -f "$XAIR_ROOT/docker-compose.yml" up -d redis 2>/dev/null || true
  fi
fi

# XAIR FastAPI
if pgrep -f "uvicorn xair.adapters.http_server" >/dev/null; then
  echo "[OK] XAIR uvicorn già in esecuzione"
else
  if [ -f "$XAIR_ROOT/.venv/bin/uvicorn" ]; then
    UVICORN="$XAIR_ROOT/.venv/bin/uvicorn"
  else
    UVICORN=uvicorn
  fi
  echo "Avvio XAIR su :8080..."
  cd "$XAIR_ROOT"
  nohup env REDIS_URL="$REDIS_URL" "$UVICORN" xair.adapters.http_server:app \
    --host 0.0.0.0 --port 8080 > "$PID_DIR/xair.log" 2>&1 &
  echo $! > "$PID_DIR/xair.pid"
  sleep 2
fi

# Health XAIR
for i in 1 2 3 4 5; do
  if curl -sf "$XAIR_URL/v1/metrics" >/dev/null; then
    echo "[OK] XAIR metrics endpoint"
    break
  fi
  sleep 1
done

# HTTP adapter (always — ROS is optional for publish witness only)
if ! pgrep -f "adaptix_quest_adapter.py" >/dev/null; then
  echo "Avvio adaptix_quest_adapter (9091/9092)..."
  if [ -f "$XAIR_ROOT/.venv/bin/python" ]; then
    ADAPTER_PY="$XAIR_ROOT/.venv/bin/python"
  else
    ADAPTER_PY=python3
  fi
  nohup env XAIR_URL="$XAIR_URL" XAIR_ADAPTER_WEBSOCKET=0 \
    "$ADAPTER_PY" "$SCRIPTS/adaptix_quest_adapter.py" \
    > "$PID_DIR/adapter.log" 2>&1 &
  echo $! > "$PID_DIR/adapter.pid"
  for i in 1 2 3 4 5; do
    if curl -sf http://127.0.0.1:9092/health >/dev/null; then
      echo "[OK] HTTP adapter :9092"
      break
    fi
    sleep 1
  done
fi

# ROS 2 + rosbridge (optional)
if [ -f /opt/ros/jazzy/setup.bash ]; then
  set +u
  # shellcheck disable=SC1091
  source /opt/ros/jazzy/setup.bash
  set -u
  if ! pgrep -f "rosbridge_websocket" >/dev/null; then
    echo "Avvio rosbridge :9090..."
    nohup ros2 launch rosbridge_server rosbridge_websocket_launch.xml \
      port:=9090 address:=0.0.0.0 > "$PID_DIR/rosbridge.log" 2>&1 &
    echo $! > "$PID_DIR/rosbridge.pid"
  fi
  if ! pgrep -f "ros_audit_subscriber.py" >/dev/null; then
    echo "Avvio ROS audit witness..."
    nohup python3 "$SCRIPTS/ros_audit_subscriber.py" \
      > "$PID_DIR/ros_audit.log" 2>&1 &
    echo $! > "$PID_DIR/ros_audit.pid"
  fi
else
  echo "[WARN] ROS 2 Jazzy non installato — adapter HTTP attivo, witness ROS disabilitato"
fi

echo ""
echo "Stack pronto:"
echo "  XAIR API:    $XAIR_URL"
echo "  Adapter:     http://0.0.0.0:9092 (/command /intent /context)"
echo "  ROSBridge:   ws://0.0.0.0:9090"
echo "  Verifica:    $SCRIPTS/verify_e2e.sh"
echo "  Log:         $PID_DIR/"
