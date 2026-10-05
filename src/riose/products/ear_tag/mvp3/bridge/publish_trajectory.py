"""Publish a Gazebo trajectory textproto without OS argument-size limits."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from google.protobuf import text_format
from gz.msgs10.joint_trajectory_pb2 import JointTrajectory
from gz.transport13 import Node


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: publish_trajectory.py TOPIC TEXT_PROTO_FILE", file=sys.stderr)
        return 2
    topic, source = sys.argv[1], Path(sys.argv[2])
    message = JointTrajectory()
    text_format.Parse(source.read_text(encoding="utf-8"), message)
    node = Node()
    publisher = node.advertise(topic, JointTrajectory)
    deadline = time.monotonic() + 10.0
    while not publisher.has_connections() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not publisher.has_connections():
        print(f"no Gazebo subscriber connected to {topic}", file=sys.stderr)
        return 3
    if not publisher.publish(message):
        print(f"Gazebo Transport rejected trajectory on {topic}", file=sys.stderr)
        return 4
    print(f"published {len(message.points)} trajectory points to {topic}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
