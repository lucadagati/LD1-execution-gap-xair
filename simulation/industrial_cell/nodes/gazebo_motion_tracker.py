#!/usr/bin/env python3
"""
Track Gazebo joint motion for E8 physical_motion proof.

Subscribes to /joint_states; increments motion_count when controlled joints move.
Writes experiments/results/e8_motion_state.json (source=gazebo).
"""

from __future__ import annotations

import json
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import JointState

MOTION_FILE = Path(__file__).resolve().parents[3] / "experiments" / "results" / "e8_motion_state.json"
JOINT_STATE_TOPIC = "/world/conveyor_cell/model/industrial_cell/joint_state"
TRACKED = ("arm_slide_joint", "conveyor_joint")
THRESHOLD = 0.02


def write_state(state: dict) -> None:
    MOTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    MOTION_FILE.write_text(json.dumps(state, indent=2))


class GazeboMotionTracker(Node):
    def __init__(self) -> None:
        super().__init__("gazebo_motion_tracker")
        self._last: dict[str, float] = {}
        state = {
            "motion_count": 0,
            "last_joints": {},
            "source": "gazebo",
            "ros": True,
        }
        write_state(state)
        self._state = state
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.create_subscription(JointState, JOINT_STATE_TOPIC, self._on_js, qos)
        self.get_logger().info(f"Gazebo motion tracker ({JOINT_STATE_TOPIC}) -> {MOTION_FILE}")

    def _on_js(self, msg: JointState) -> None:
        moved = False
        joints = {}
        for name, pos in zip(msg.name, msg.position):
            if name not in TRACKED:
                continue
            joints[name] = pos
            prev = self._last.get(name)
            if prev is not None and abs(pos - prev) > THRESHOLD:
                moved = True
            self._last[name] = pos
        if moved:
            self._state["motion_count"] += 1
        self._state["last_joints"] = joints
        if "arm_slide_joint" in joints:
            self._state["arm_position"] = joints["arm_slide_joint"]
        write_state(self._state)


def main() -> None:
    rclpy.init()
    node = GazeboMotionTracker()
    rclpy.spin(node)


if __name__ == "__main__":
    main()
