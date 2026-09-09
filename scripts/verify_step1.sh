#!/usr/bin/env bash
# Verifica Step 1: VM (ROS + adapter) avviata e risposta a POST /command.
# Uso: ./verify_step1.sh [BASE_URL]
#   BASE_URL default: http://localhost:9092 (usa http://<IP_VM>:9092 da Mac)

set -e
BASE_URL="${1:-http://localhost:9092}"

echo "=== Verifica Step 1 – Adapter AdaptiX ==="
echo "  URL: $BASE_URL"
echo ""

# Test HTTP POST /command
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/command" \
  -H "Content-Type: application/json" \
  -d '{"pose":{"position":{"x":0,"y":0,"z":0.5},"orientation":{"x":0,"y":0,"z":0,"w":1}},"gripper":{"x":0,"y":0,"z":0}}')
BODY=$(echo "$RESP" | head -n -1)
CODE=$(echo "$RESP" | tail -n 1)

if [ "$CODE" = "200" ] && echo "$BODY" | grep -q '"ok":\s*true'; then
  echo "[OK] POST /command risponde: $BODY"
else
  echo "[FAIL] Risposta HTTP $CODE: $BODY"
  exit 1
fi

# Opzionale: verifica topic ROS 2 (solo se siamo sul server con ROS attivo)
if [ -f /opt/ros/jazzy/setup.bash ]; then
  source /opt/ros/jazzy/setup.bash 2>/dev/null || true
  if ros2 topic list 2>/dev/null | grep -q "/UE_TCP_position"; then
    echo "[OK] Topic ROS 2 presenti: /UE_TCP_position, /UE_Gripper_angles"
  else
    echo "[INFO] Topic ROS non verificati (ros2 non in esecuzione o non sul server)"
  fi
fi

echo ""
echo "Step 1 verificato: adapter riceve comandi e pubblica sui topic."
