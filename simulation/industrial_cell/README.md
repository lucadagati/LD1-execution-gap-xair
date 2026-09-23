# Industrial Cell — Gazebo Harmonic (Headless)

Manufacturing cell simulation for E8 motion proof: conveyor + TCP slide + gripper, controlled via the same ROS topics as the AdaptiX adapter.

## Layout

```
industrial_cell/
├── world/conveyor_cell.sdf      # Gazebo Harmonic world (floor, conveyor frame)
├── urdf/cell.urdf.xacro         # arm_slide, conveyor, gripper + gz_ros2_control
├── ros2_control/cell_controllers.yaml
├── launch/cell_headless.launch.py
└── nodes/
    ├── command_subscriber.py    # /UE_TCP_position → joint commands
    ├── gazebo_motion_tracker.py # /joint_states → e8_motion_state.json
    └── cell_simulator.py        # fallback tracker (no Gazebo)
```

## Prerequisites

Ubuntu 24.04 + ROS 2 Jazzy + Gazebo Harmonic:

```bash
./scripts/setup_gazebo.sh
```

## Quick start (full E8-Gazebo)

```bash
./scripts/run_e8_gazebo_full.sh 30 1   # 30 runs per baseline, campaign tag "1"
```

The wrapper starts the HTTP/ROS stack, the headless cell
(`scripts/start_gazebo_cell.sh`) and the joint-state motion tracker, then runs
`experiments/run_e8_gazebo_cell.py`. Each campaign writes its own file
(`experiments/results/e8_gazebo_campaign<tag>.csv`), so repeated campaigns are
never overwritten.

## Fallback (ROS topics only, no Gazebo)

```bash
python3 simulation/industrial_cell/nodes/cell_simulator.py &
.venv/bin/python experiments/run_e8_gazebo_cell.py --runs 30 --campaign fallback
```

## Context loop

1. Adapter publishes `/UE_TCP_position` and `/UE_Gripper_angles` on EXECUTE.
2. `command_subscriber` maps pose → `cell_position_controller` (arm, conveyor, gripper).
3. `gazebo_motion_tracker` increments `motion_count` when joints move.
4. E8 records two witnesses per trial: the ROS audit subscriber (message seen on the actuator topic) and the joint-motion delta. Only the first is a release witness; motion is a noisier physical-effect proxy.

## Verify

```bash
source /opt/ros/jazzy/setup.bash
ros2 topic list | grep -E 'joint_states|UE_TCP'
ros2 topic echo /joint_states --once
cat experiments/results/e8_motion_state.json
```
