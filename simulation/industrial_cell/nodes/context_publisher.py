#!/usr/bin/env python3
"""
Publish /cell/state from XAIR context snapshot (optional telemetry loop).
"""

from __future__ import annotations

import json
import urllib.request

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

XAIR_URL = "http://127.0.0.1:8080/v1/context/snapshot"


class ContextPublisher(Node):
    def __init__(self) -> None:
        super().__init__("cell_context_publisher")
        self._pub = self.create_publisher(String, "/cell/state", 10)
        self.create_timer(0.5, self._tick)
        self.get_logger().info("Publishing /cell/state from XAIR context")

    def _tick(self) -> None:
        try:
            with urllib.request.urlopen(XAIR_URL, timeout=2) as resp:
                ctx = json.loads(resp.read().decode()).get("context", {})
            msg = String()
            msg.data = json.dumps(ctx)
            self._pub.publish(msg)
        except Exception as exc:
            self.get_logger().debug(f"context fetch failed: {exc}")


def main() -> None:
    rclpy.init()
    node = ContextPublisher()
    rclpy.spin(node)


if __name__ == "__main__":
    main()
