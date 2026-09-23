#!/usr/bin/env bash
# E8-Gazebo inside the ROS 2 Jazzy + Gazebo Harmonic container (docker/ros-jazzy),
# for hosts that cannot install Jazzy natively. The container has its own
# network namespace, so its ports never collide with host services.
# Usage: ./scripts/run_e8_docker.sh [runs-per-baseline] [campaign-tag]
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
IMAGE="${XAIR_ROS_IMAGE:-xair-ros-jazzy}"
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -t "$IMAGE" "$REPO_ROOT/docker/ros-jazzy"

docker run --rm --name "xair-e8-${2:-1}" -v "$REPO_ROOT:/repo" -w /repo \
  -e PY=python3 -e REDIS_URL= -e RUN_DIR=/tmp/xair-run -e XAIR_RESULTS_DIR=/repo/experiments/results \
  -e ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}" \
  "$IMAGE" bash -c "source /opt/ros/jazzy/setup.bash && ./scripts/run_e8_gazebo_full.sh '${1:-30}' '${2:-1}'"
