#!/usr/bin/env python3
"""Capture one real OGRE2 Gazebo camera image from Gazebo Transport."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic")
    parser.add_argument("output", type=Path)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    try:
        import cv2
        import numpy as np
        from gz.transport13 import Node
        from gz.msgs10.image_pb2 import Image
    except ImportError as exc:
        print(f"Gazebo image capture dependency missing: {exc}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    node = Node()
    captured: list[bool] = []

    def receive(message) -> None:
        if captured:
            return
        channels = 4 if message.step >= message.width * 4 else 3
        pixels = np.frombuffer(message.data, dtype=np.uint8).reshape(message.height, message.step)
        pixels = pixels[:, :message.width * channels].reshape(message.height, message.width, channels)
        if channels == 4:
            pixels = cv2.cvtColor(pixels, cv2.COLOR_RGBA2BGR)
        else:
            pixels = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
        if not cv2.imwrite(str(args.output), pixels):
            print(f"could not write camera frame: {args.output}", file=sys.stderr, flush=True)
            captured.append(False)
            return
        print(f"Captured {message.width}x{message.height} from {args.topic}", flush=True)
        captured.append(True)

    node.subscribe(Image, args.topic, receive)
    deadline = time.monotonic() + args.timeout
    while not captured and time.monotonic() < deadline:
        time.sleep(0.05)
    # Gazebo Transport's native worker pool can segfault during interpreter
    # teardown on some ROS Jazzy builds; exit after the synchronous PNG flush.
    os._exit(0 if captured and captured[0] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
