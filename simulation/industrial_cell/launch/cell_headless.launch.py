#!/usr/bin/env python3
"""Headless Gazebo Harmonic launch — ros_gz bridge (no gz_ros2_control)."""

from __future__ import annotations

import os
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription, SetEnvironmentVariable
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution

from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def _sim_root() -> Path:
    return Path(__file__).resolve().parents[1]


def generate_launch_description():
    sim_root = _sim_root()
    world = sim_root / "world" / "conveyor_cell.sdf"
    models_dir = sim_root / "models"
    nodes_dir = sim_root / "nodes"

    default_gz = f"-s -r -v 1 {world}"
    gz_args = LaunchConfiguration("gz_args", default=default_gz)

    bridge_args = [
        "/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock",
        "/world/conveyor_cell/model/industrial_cell/joint_state@sensor_msgs/msg/JointState[gz.msgs.Model",
        "/model/industrial_cell/joint/arm_slide_joint/cmd_vel@std_msgs/msg/Float64]gz.msgs.Double",
        "/model/industrial_cell/joint/conveyor_joint/cmd_vel@std_msgs/msg/Float64]gz.msgs.Double",
        "/model/industrial_cell/joint/gripper_joint/cmd_vel@std_msgs/msg/Float64]gz.msgs.Double",
    ]

    return LaunchDescription(
        [
            SetEnvironmentVariable("GZ_SIM_RESOURCE_PATH", str(models_dir)),
            DeclareLaunchArgument("gz_args", default_value=default_gz),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    [PathJoinSubstitution([FindPackageShare("ros_gz_sim"), "launch", "gz_sim.launch.py"])]
                ),
                launch_arguments=[("gz_args", gz_args)],
            ),
            Node(
                package="ros_gz_bridge",
                executable="parameter_bridge",
                arguments=bridge_args,
                output="screen",
            ),
            ExecuteProcess(cmd=["python3", str(nodes_dir / "command_subscriber.py"), "--gz-bridge"], output="screen"),
            ExecuteProcess(cmd=["python3", str(nodes_dir / "gazebo_motion_tracker.py")], output="screen"),
        ]
    )
