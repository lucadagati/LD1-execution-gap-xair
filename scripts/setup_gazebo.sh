#!/usr/bin/env bash
# Install Gazebo Harmonic + ros_gz for AdaptiX industrial cell (Ubuntu 24.04 / ROS 2 Jazzy)
set -e

echo "=== AdaptiX — Gazebo Harmonic setup ==="

if [ ! -f /opt/ros/jazzy/setup.bash ]; then
  echo "ROS 2 Jazzy required. Run scripts/setup_server_ros.sh first."
  exit 1
fi

sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ros-jazzy-ros-gz \
  ros-jazzy-gz-sim-vendor \
  ros-jazzy-gz-tools-vendor \
  ros-jazzy-gz-ros2-control \
  ros-jazzy-robot-state-publisher \
  ros-jazzy-xacro \
  ros-jazzy-joint-state-publisher \
  ros-jazzy-ros2-control \
  ros-jazzy-ros2-controllers

source /opt/ros/jazzy/setup.bash
if gz sim --help >/dev/null 2>&1; then
  echo "[OK] gz sim (Gazebo Harmonic)"
else
  echo "[FAIL] gz sim not found"
  exit 1
fi

SIM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/simulation/industrial_cell"
echo ""
echo "Gazebo cell assets: $SIM_ROOT"
echo "Run E8-Gazebo:  ./scripts/run_e8_gazebo_full.sh 30 1"
