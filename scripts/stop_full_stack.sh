#!/usr/bin/env bash
# Ferma stack completo AdaptiX + XAIR + Redis
set -e
ADAPTIX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_DIR="$ADAPTIX_ROOT/.run"

echo "=== Stop full stack ==="

for name in adapter xair rosbridge; do
  if [ -f "$PID_DIR/$name.pid" ]; then
    pid=$(cat "$PID_DIR/$name.pid")
    kill "$pid" 2>/dev/null || true
    rm -f "$PID_DIR/$name.pid"
    echo "  Stopped $name (pid $pid)"
  fi
done

pkill -f "adaptix_quest_adapter.py" 2>/dev/null || true
pkill -f "uvicorn xair.adapters.http_server" 2>/dev/null || true
pkill -f "rosbridge_websocket" 2>/dev/null || true

if command -v fuser &>/dev/null; then
  for port in 8080 9090 9091 9092; do
    fuser -k "$port/tcp" 2>/dev/null || true
  done
fi

if command -v docker &>/dev/null && [ -f "$ADAPTIX_ROOT/XAIR_Runtime/docker-compose.yml" ]; then
  docker compose -f "$ADAPTIX_ROOT/XAIR_Runtime/docker-compose.yml" stop redis 2>/dev/null || true
fi

echo "Fatto."
