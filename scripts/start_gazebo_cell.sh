#!/usr/bin/env bash
# Start Gazebo Harmonic industrial cell (headless) for E8
set -e

ADAPTIX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIM_LAUNCH="$ADAPTIX_ROOT/XAIR_Runtime/simulation/industrial_cell/launch/cell_headless.launch.py"
PID_DIR="$ADAPTIX_ROOT/.run"
mkdir -p "$PID_DIR"

if [ ! -f /opt/ros/jazzy/setup.bash ]; then
  echo "ROS 2 Jazzy not installed."
  exit 1
fi
source /opt/ros/jazzy/setup.bash

if pgrep -f "cell_headless.launch.py" >/dev/null; then
  echo "[OK] Gazebo cell già in esecuzione"
  exit 0
fi

echo "Avvio Gazebo Harmonic (headless) + ros2_control..."
nohup ros2 launch "$SIM_LAUNCH" > "$PID_DIR/gazebo_cell.log" 2>&1 &
echo $! > "$PID_DIR/gazebo_cell.pid"
sleep 8

if ros2 topic list 2>/dev/null | grep -q joint_states; then
  echo "[OK] /joint_states attivo"
else
  echo "[WARN] Gazebo in avvio — controllare $PID_DIR/gazebo_cell.log"
fi
