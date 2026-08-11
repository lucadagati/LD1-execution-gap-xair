#!/usr/bin/env python3
"""
Bridge AdaptiX adapter ROS topics to Gazebo joint velocity commands.

Subscribes:
  /UE_TCP_position   -> arm_slide + conveyor velocity pulse
  /UE_Gripper_angles -> gripper velocity
"""

from __future__ import annotations

import argparse
import math
import threading
import time

import rclpy
from geometry_msgs.msg import Point, Pose
from rclpy.node import Node
from std_msgs.msg import Float64


class CommandSubscriber(Node):
    PULSE_SEC = 0.8
    ARM_VEL = 0.4

    def __init__(self, use_gz_bridge: bool) -> None:
        super().__init__("cell_command_subscriber")
        self._use_gz_bridge = use_gz_bridge
        self._target_arm = 0.2
        self._current_arm = 0.2
        self._lock = threading.Lock()

        if use_gz_bridge:
            self._pub_arm = self.create_publisher(Float64, "/model/industrial_cell/joint/arm_slide_joint/cmd_vel", 10)
            self._pub_conv = self.create_publisher(Float64, "/model/industrial_cell/joint/conveyor_joint/cmd_vel", 10)
            self._pub_grip = self.create_publisher(Float64, "/model/industrial_cell/joint/gripper_joint/cmd_vel", 10)
            self.create_timer(0.05, self._velocity_loop)
            from rclpy.qos import QoSProfile, ReliabilityPolicy
            from sensor_msgs.msg import JointState

            qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)
            self.create_subscription(
                JointState,
                "/world/conveyor_cell/model/industrial_cell/joint_state",
                self._on_joint_state,
                qos,
            )
            mode = "Gazebo joint velocity (gz-bridge)"
        else:
            from std_msgs.msg import Float64MultiArray

            self._pub_multi = self.create_publisher(Float64MultiArray, "/cell_position_controller/commands", 10)
            mode = "ros2_control ForwardCommandController"

        self.create_subscription(Pose, "/UE_TCP_position", self._on_pose, 10)
        self.create_subscription(Point, "/UE_Gripper_angles", self._on_gripper, 10)
        self.get_logger().info(f"Bridging /UE_TCP_position -> {mode}")

    def _on_joint_state(self, msg) -> None:
        for name, pos in zip(msg.name, msg.position):
            if name == "arm_slide_joint":
                with self._lock:
                    self._current_arm = float(pos)
                break

    def _publish_vel(self, arm: float, conv: float, grip: float) -> None:
        for pub, val in ((self._pub_arm, arm), (self._pub_conv, conv), (self._pub_grip, grip)):
            msg = Float64()
            msg.data = val
            pub.publish(msg)

    def _velocity_loop(self) -> None:
        with self._lock:
            target = self._target_arm
            current = self._current_arm
        err = target - current
        vel = max(-self.ARM_VEL, min(self.ARM_VEL, err * 2.0))
        if abs(vel) > 0.01:
            self._publish_vel(vel, 0.5 if abs(err) > 0.05 else 0.0, 0.0)

    def _on_pose(self, msg: Pose) -> None:
        if not self._use_gz_bridge:
            from std_msgs.msg import Float64MultiArray

            arm = max(0.0, min(1.0, float(msg.position.x)))
            out = Float64MultiArray()
            out.data = [arm, 0.35, 0.04]
            self._pub_multi.publish(out)
            return
        with self._lock:
            self._target_arm = max(0.0, min(1.0, float(msg.position.x)))
        with self._lock:
            err = self._target_arm - self._current_arm
        arm_vel = max(-self.ARM_VEL, min(self.ARM_VEL, err * 2.0)) if abs(err) > 0.02 else 0.0
        self._publish_vel(arm_vel, 0.6, 0.0)
        threading.Timer(self.PULSE_SEC, lambda: self._publish_vel(0.0, 0.0, 0.0)).start()

    def _on_gripper(self, msg: Point) -> None:
        if not self._use_gz_bridge:
            return
        g = max(-0.2, min(0.2, (float(msg.x) - 0.5) * 0.4))
        self._publish_vel(0.0, 0.0, g)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gz-bridge", action="store_true")
    args, _ = parser.parse_known_args()

    rclpy.init()
    node = CommandSubscriber(use_gz_bridge=args.gz_bridge)
    rclpy.spin(node)


if __name__ == "__main__":
    main()
