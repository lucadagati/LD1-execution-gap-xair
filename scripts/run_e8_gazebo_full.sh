#!/usr/bin/env bash
# E8 with optional Gazebo joint-motion witness (requires Gazebo cell + motion tracker).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$ROOT/scripts"
XAIR="$ROOT/XAIR_Runtime"
PY="$XAIR/.venv/bin/python"

source /opt/ros/jazzy/setup.bash
"$SCRIPTS/start_full_stack.sh"
"$SCRIPTS/start_gazebo_cell.sh" || true

# Motion tracker for arm_delta / sim_motion columns
if ! pgrep -f "gazebo_motion_tracker.py" >/dev/null; then
  nohup python3 "$XAIR/simulation/industrial_cell/nodes/gazebo_motion_tracker.py" \
    > "$ROOT/.run/motion_tracker.log" 2>&1 &
  sleep 3
fi

"$PY" "$XAIR/experiments/run_e8_gazebo_cell.py" --runs "${1:-30}" --use-gazebo
"$PY" "$XAIR/experiments/aggregate_experiment_results.py"
"$PY" "$XAIR/experiments/plot_results.py"
"$SCRIPTS/sync_paper_artifact.sh"
