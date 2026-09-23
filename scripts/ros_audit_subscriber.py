#!/usr/bin/env python3
"""Independent ROS 2 audit witness for actuation-topic traffic.

Subscribes to the actuator command topics used by the AdaptiX adapter and
persists a monotonic message counter to a JSON file. Experiment runners read
the counter before and after each intent submission to verify middleware
publication independently of the adapter's self-reported `ros_published` flag.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RESULTS = Path(os.environ.get("XAIR_RESULTS_DIR", _REPO_ROOT / "experiments" / "results"))
AUDIT_FILE = Path(os.environ.get("ROS_AUDIT_FILE", _RESULTS / "ros_audit_state.json"))


def _atomic_write(payload: dict) -> None:
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(AUDIT_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fp:
            json.dump(payload, fp)
        os.replace(tmp, AUDIT_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    import rclpy
    from geometry_msgs.msg import Point, Pose

    rclpy.init()
    node = rclpy.create_node("xair_ros_audit_witness")

    state = {"pose_count": 0, "gripper_count": 0, "last_pose_ts": None, "started_at": time.time(), "alive_at": time.time()}

    def touch_alive() -> None:
        state["alive_at"] = time.time()
        _atomic_write(state)

    def on_pose(_msg) -> None:
        state["pose_count"] += 1
        state["last_pose_ts"] = time.time()
        state["alive_at"] = state["last_pose_ts"]
        _atomic_write(state)

    def on_gripper(_msg) -> None:
        state["gripper_count"] += 1
        state["alive_at"] = time.time()
        _atomic_write(state)

    node.create_subscription(Pose, "/UE_TCP_position", on_pose, 10)
    node.create_subscription(Point, "/UE_Gripper_angles", on_gripper, 10)
    node.create_timer(1.0, touch_alive)

    _atomic_write(state)
    print(f"ROS audit witness up; state -> {AUDIT_FILE}", flush=True)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
