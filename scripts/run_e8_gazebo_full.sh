#!/usr/bin/env bash
# E8-Gazebo: stack + headless Gazebo cell + motion tracker, then the E8 suite.
# Usage: run_e8_gazebo_full.sh [runs-per-baseline] [campaign-tag]
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"

set +u
# shellcheck disable=SC1091
source /opt/ros/jazzy/setup.bash
set -u
"$SCRIPTS/start_full_stack.sh"
"$SCRIPTS/start_gazebo_cell.sh"

if ! pgrep -f "gazebo_motion_tracker.py" >/dev/null; then
  nohup env XAIR_RESULTS_DIR="$XAIR_RESULTS_DIR" python3 "$REPO_ROOT/simulation/industrial_cell/nodes/gazebo_motion_tracker.py" \
    > "$RUN_DIR/motion_tracker.log" 2>&1 &
  echo $! > "$RUN_DIR/motion_tracker.pid"
  sleep 3
fi

"$PY" "$REPO_ROOT/experiments/run_e8_gazebo_cell.py" --runs "${1:-30}" --campaign "${2:-1}"
