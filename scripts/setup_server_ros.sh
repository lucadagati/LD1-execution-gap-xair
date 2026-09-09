#!/usr/bin/env bash
# Setup ROS / ROS 2 + ROSBridge sul server per AdaptiX (accesso remoto)
#   Ubuntu 18.04 -> ROS 1 Melodic
#   Ubuntu 20.04 -> ROS 1 Noetic
#   Ubuntu 24.04 -> ROS 2 Jazzy (consigliato per questo progetto)

set -e
ADAPTIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ADAPTIX_DIR"

echo "=== AdaptiX - Setup ROS lato server ==="

if [ -f /etc/os-release ]; then
  . /etc/os-release
  UBUNTU_VERSION="${VERSION_ID:-}"
else
  echo "Impossibile rilevare Ubuntu."
  exit 1
fi

# --- Ubuntu 24.04: ROS 2 Jazzy ---
if [ "$UBUNTU_VERSION" = "24.04" ]; then
  if [ ! -f /opt/ros/jazzy/setup.bash ]; then
    echo "Installazione ROS 2 Jazzy..."
    sudo apt-get update -qq
    sudo apt-get install -y software-properties-common curl
    sudo add-apt-repository -y universe
    ROS_APT_VER=$(curl -s https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F '"tag_name"' | awk -F'"' '{print $4}')
    curl -sL -o /tmp/ros2-apt-source.deb "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ROS_APT_VER}/ros2-apt-source_${ROS_APT_VER}.noble_all.deb"
    sudo dpkg -i /tmp/ros2-apt-source.deb
    sudo apt-get update -qq
    sudo apt-get install -y ros-jazzy-ros-base ros-jazzy-rosbridge-suite
    echo "ROS 2 Jazzy installato."
  fi
  if ! dpkg -l python3-websockets &>/dev/null; then
    echo "Installazione python3-websockets (opzionale, per adapter WebSocket)..."
    sudo apt-get install -y python3-websockets
  fi
  echo ""
  echo "Per Gazebo Harmonic (E8 cell sim): $ADAPTIX_DIR/setup_gazebo.sh"
  echo ""
  echo "Setup completato (ROS 2 Jazzy). Per avviare:"
  echo "  source /opt/ros/jazzy/setup.bash"
  echo "  $ADAPTIX_DIR/start_adaptix_ros_remote.sh"
  exit 0
fi

# --- Ubuntu 18.04 / 20.04: ROS 1 ---
case "$UBUNTU_VERSION" in
  18.04) ROS_DISTRO=melodic ;;
  20.04) ROS_DISTRO=noetic ;;
  *)
    echo "Ubuntu $UBUNTU_VERSION: per questo progetto è consigliato Ubuntu 24.04 + ROS 2 Jazzy."
    echo "Per 18.04: ROS Melodic. Per 20.04: ROS Noetic."
    exit 1
    ;;
esac

echo "ROS distro: $ROS_DISTRO"

if ! command -v roscore &>/dev/null; then
  echo "ROS non trovato. Installare manualmente:"
  echo "  https://wiki.ros.org/${ROS_DISTRO}/Installation/Ubuntu"
  exit 1
fi

source /opt/ros/$ROS_DISTRO/setup.bash 2>/dev/null || true
if ! ros pkg list 2>/dev/null | grep -q rosbridge; then
  echo "Installazione rosbridge_suite..."
  sudo apt-get update
  sudo apt-get install -y "ros-${ROS_DISTRO}-rosbridge-server" || true
fi

echo ""
echo "Setup completato. Per avviare: $ADAPTIX_DIR/start_adaptix_ros_remote.sh"
echo "Nota: lo script start_adaptix_ros_remote.sh è configurato per ROS 2 Jazzy; su 18.04/20.04 adattare manualmente (roscore + roslaunch)."
