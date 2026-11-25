# ROS 2 Jazzy + Gazebo Harmonic image for E8-Gazebo (Ubuntu 24.04 base).
# Build:  docker build -t xair-ros-jazzy docker/ros-jazzy
# Run:    ./scripts/run_e8_docker.sh 30 <campaign-tag>
FROM osrf/ros:jazzy-desktop

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ros-jazzy-ros-gz ros-jazzy-gz-ros2-control ros-jazzy-robot-state-publisher \
      ros-jazzy-xacro ros-jazzy-joint-state-publisher ros-jazzy-ros2-control \
      ros-jazzy-ros2-controllers ros-jazzy-rosbridge-suite \
      python3-pip curl procps \
    && rm -rf /var/lib/apt/lists/*

# XAIR runtime dependencies for the system python used by rclpy (3.12 on 24.04).
RUN pip3 install --no-cache-dir --break-system-packages \
      "fastapi>=0.110" "uvicorn[standard]>=0.27" "pydantic>=2" "jsonschema>=4.21" \
      "redis>=5" "websockets>=12"

WORKDIR /repo
