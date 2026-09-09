#!/usr/bin/env python3
"""
Nodo ROS 2 che sottoscrive /UE_TCP_position e /UE_Gripper_angles (Step 5 opzionale).
Utile per: log su file, simulazione, o analisi senza robot Kinova.
Uso: source /opt/ros/jazzy/setup.bash && python3 adaptix_topic_logger.py [--log]
  --log: stampa ogni messaggio su stdout (default: solo contatori ogni 5s).
"""

import argparse
import sys

def main():
    parser = argparse.ArgumentParser(description="AdaptiX topic logger/sim subscriber")
    parser.add_argument("--log", action="store_true", help="Log ogni messaggio")
    args = parser.parse_args()

    try:
        import rclpy
        from rclpy.node import Node
        from geometry_msgs.msg import Pose, Point
    except ImportError:
        print("ROS 2 non trovato. Esegui: source /opt/ros/jazzy/setup.bash")
        sys.exit(1)

    class LoggerNode(Node):
        def __init__(self, log_every_msg):
            super().__init__("adaptix_topic_logger")
            self.log_every = log_every_msg
            self.count_pose = 0
            self.count_gripper = 0
            self.sub_pose = self.create_subscription(
                Pose, "/UE_TCP_position", self.cb_pose, 10
            )
            self.sub_gripper = self.create_subscription(
                Point, "/UE_Gripper_angles", self.cb_gripper, 10
            )
            self.timer = self.create_timer(5.0, self.print_stats)
            self.get_logger().info("Sottoscritto a /UE_TCP_position e /UE_Gripper_angles")

        def cb_pose(self, msg):
            self.count_pose += 1
            if self.log_every:
                self.get_logger().info(
                    "pose: pos=(%.3f,%.3f,%.3f)" % (
                        msg.position.x, msg.position.y, msg.position.z
                    )
                )

        def cb_gripper(self, msg):
            self.count_gripper += 1
            if self.log_every:
                self.get_logger().info("gripper: (%.3f,%.3f,%.3f)" % (msg.x, msg.y, msg.z))

        def print_stats(self):
            self.get_logger().info(
                "Messaggi: pose=%d, gripper=%d" % (self.count_pose, self.count_gripper)
            )

    rclpy.init()
    node = LoggerNode(args.log)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.shutdown()

if __name__ == "__main__":
    main()
