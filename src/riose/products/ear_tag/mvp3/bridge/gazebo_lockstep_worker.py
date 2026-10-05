"""Persistent Gazebo Transport endpoint for the live lockstep controller.

Run with the ROS-provisioned system Python so the Harmonic transport bindings
and their shared libraries are available. The stdio protocol is JSON Lines.
"""
from __future__ import annotations

import json
import sys
import threading

from gz.msgs10.boolean_pb2 import Boolean
from gz.msgs10.clock_pb2 import Clock
from gz.msgs10.imu_pb2 import IMU
from gz.msgs10.world_control_pb2 import WorldControl
from gz.transport13 import Node


def main() -> int:
    node = Node()
    output_lock = threading.Lock()

    def emit(message: dict) -> None:
        with output_lock:
            sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    def on_imu(message: IMU) -> None:
        stamp = message.header.stamp
        accel = message.linear_acceleration
        orientation = message.orientation
        emit({"kind": "imu", "timestamp_s": stamp.sec + stamp.nsec / 1_000_000_000,
              "linear_acceleration": {"x": accel.x, "y": accel.y, "z": accel.z},
              "orientation": {"x": orientation.x, "y": orientation.y,
                              "z": orientation.z, "w": orientation.w}})

    def on_clock(message: Clock) -> None:
        stamp = message.sim
        emit({"kind": "clock", "timestamp_s": stamp.sec + stamp.nsec / 1_000_000_000})

    topic = sys.argv[1]
    control_topic = sys.argv[2]
    clock_topic = sys.argv[3]
    node.subscribe(IMU, topic, on_imu)
    node.subscribe(Clock, clock_topic, on_clock)
    emit({"kind": "ready"})
    for raw in sys.stdin:
        try:
            command = json.loads(raw)
            request = WorldControl()
            request.pause = True
            if command.get("op") == "step":
                request.multi_step = int(command["steps"])
            elif command.get("op") == "reset":
                request.reset.all = True
            elif command.get("op") == "close":
                return 0
            else:
                raise ValueError("op must be step, reset, or close")
            sent, response = node.request(control_topic, request, WorldControl, Boolean, 250)
            emit({"kind": "response", "id": command.get("id"),
                  "success": bool(sent and response.data), "sent": bool(sent),
                  "data": bool(response.data)})
        except Exception as exc:
            emit({"kind": "response", "id": command.get("id") if isinstance(command, dict) else None,
                  "success": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
