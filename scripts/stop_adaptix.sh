#!/usr/bin/env bash
# Ferma i servizi AdaptiX (adapter Quest e, opzionalmente, rosbridge).
# Uso: ./stop_adaptix.sh [--rosbridge]
#   Senza argomenti: ferma solo adaptix_quest_adapter (porte 9091, 9092).
#   Con --rosbridge: ferma anche rosbridge (processi rosbridge_websocket, rosapi).

set -e
STOP_ROSBRIDGE=false
for arg in "$@"; do
  [ "$arg" = "--rosbridge" ] && STOP_ROSBRIDGE=true
done

echo "=== AdaptiX - Stop servizi ==="

# Adapter (Python)
if pgrep -f "adaptix_quest_adapter.py" >/dev/null; then
  pkill -f "adaptix_quest_adapter.py" || true
  sleep 1
fi
# Libera le porte se ancora occupate (root)
if command -v fuser &>/dev/null; then
  for port in 9091 9092; do
    if fuser "$port/tcp" &>/dev/null; then
      sudo fuser -k "$port/tcp" 2>/dev/null || true
      echo "  Porta $port liberata."
    fi
  done
fi
echo "  Adapter (9091/9092) fermato."

if [ "$STOP_ROSBRIDGE" = true ]; then
  pkill -f "rosbridge_websocket" 2>/dev/null || true
  pkill -f "rosapi_node" 2>/dev/null || true
  echo "  ROSBridge fermato."
fi

echo "Fatto."
