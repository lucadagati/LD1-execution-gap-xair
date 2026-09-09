#!/usr/bin/env bash
# Sources the ROS 2 environment, then execs the given command.
#
# IMPORTANT: do not pass `env PYTHONPATH=...` as part of the wrapped command —
# that replaces (not extends) the PYTHONPATH that `setup.bash` just populated
# with rclpy's site-packages, silently breaking `import rclpy` in the child
# process (it falls back to "ROS unavailable" without raising any error, so
# ros_published/witness metrics go quietly wrong). To add extra import paths
# for the wrapped command, set PYTHONPATH_PREPEND *before* invoking this
# script; it is merged with the ROS-provided PYTHONPATH below.
set -euo pipefail
if [ -f /opt/ros/jazzy/setup.bash ]; then
  set +u
  # shellcheck disable=SC1091
  source /opt/ros/jazzy/setup.bash
  set -u
fi
if [ -n "${PYTHONPATH_PREPEND:-}" ]; then
  export PYTHONPATH="${PYTHONPATH_PREPEND}:${PYTHONPATH:-}"
fi
exec "$@"
