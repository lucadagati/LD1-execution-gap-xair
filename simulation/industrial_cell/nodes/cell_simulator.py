#!/usr/bin/env python3
"""
Headless industrial cell — ROS 2 motion tracker.
Subscribes to /UE_TCP_position; increments motion_count on each publish.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MOTION_FILE = Path(__file__).resolve().parents[3] / "experiments" / "results" / "e8_motion_state.json"


def write_state(state: dict) -> None:
    MOTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    MOTION_FILE.write_text(json.dumps(state, indent=2))


def main():
    state = {"motion_count": 0, "last_pose": None, "ros": False}
    write_state(state)

    try:
        import rclpy
        from geometry_msgs.msg import Pose
    except ImportError:
        print("rclpy not available")
        return

    rclpy.init()
    node = rclpy.create_node("industrial_cell_sim")

    def on_pose(msg: Pose):
        state["motion_count"] += 1
        state["last_pose"] = {"x": msg.position.x, "y": msg.position.y, "z": msg.position.z}
        state["ros"] = True
        write_state(state)

    node.create_subscription(Pose, "/UE_TCP_position", on_pose, 10)
    write_state(state)
    print(f"Cell simulator listening on /UE_TCP_position → {MOTION_FILE}")
    rclpy.spin(node)


if __name__ == "__main__":
    main()
