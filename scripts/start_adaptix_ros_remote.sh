#!/usr/bin/env bash
# Avvia ROS 2 + ROSBridge sul server per accesso remoto AdaptiX.
# ROSBridge in ascolto su 0.0.0.0:9090 così i client possono connettersi da remoto.
# Requisiti: ROS 2 Jazzy (Ubuntu 24.04), ros-jazzy-rosbridge-suite.

set -e
ADAPTIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ADAPTIX_DIR"

# ROS 2 Jazzy
if [ -f /opt/ros/jazzy/setup.bash ]; then
  source /opt/ros/jazzy/setup.bash
  ROS_DISTRO=jazzy
else
  echo "ROS 2 non trovato. Eseguire prima: ./setup_server_ros.sh (o installare ros-jazzy-ros-base e ros-jazzy-rosbridge-suite)"
  exit 1
fi

echo "=== AdaptiX - ROS 2 remoto ==="
echo "  ROSBridge: ws://<IP_SERVER>:9090"
echo "  Adapter Quest: HTTP :9092 /command, WebSocket :9091"
echo ""

# Se le porte sono occupate, suggerire di fermare prima con stop_adaptix.sh
if command -v ss &>/dev/null; then
  if ss -tln | grep -q ':9092 '; then
    echo "Attenzione: porta 9092 già in uso. Esegui prima: $ADAPTIX_DIR/stop_adaptix.sh"
    exit 1
  fi
fi

# Adapter Quest (HTTP :9092, WebSocket :9091) in background
if [ -f "$ADAPTIX_DIR/adaptix_quest_adapter.py" ]; then
  echo "Avvio adaptix_quest_adapter (9091 WS, 9092 HTTP)..."
  python3 "$ADAPTIX_DIR/adaptix_quest_adapter.py" &
  sleep 2
fi

# ROSBridge in ascolto su tutte le interfacce (accesso remoto)
echo "Avvio rosbridge_websocket (address=0.0.0.0, port=9090)..."
ros2 launch rosbridge_server rosbridge_websocket_launch.xml port:=9090 address:=0.0.0.0
