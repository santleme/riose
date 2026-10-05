#!/usr/bin/env python3
"""Record synchronized Gazebo camera topics as timestamped MP4 clips."""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--fps", type=float, default=8)
    args = parser.parse_args()
    try:
        import cv2
        import numpy as np
        from gz.transport13 import Node
        from gz.msgs10.image_pb2 import Image
    except ImportError as exc:
        print(f"Gazebo video dependency missing: {exc}", file=sys.stderr)
        return 2

    names = ("overview", "follow", "head", "ear_tag_macro", "anchor", "ground_low")
    args.output.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    writers = {}
    timestamps = {}
    frame_counts = {name: 0 for name in names}

    def receive(name: str, message) -> None:
        with lock:
            if name not in writers:
                width, height = int(message.width), int(message.height)
                writer = cv2.VideoWriter(str(args.output / f"{name}.mp4"),
                                         cv2.VideoWriter_fourcc(*"mp4v"), args.fps,
                                         (width, height))
                if not writer.isOpened():
                    raise RuntimeError(f"OpenCV could not create {name}.mp4")
                writers[name] = writer
                timestamps[name] = (args.output / f"{name}_timestamps.csv").open(
                    "w", newline="", encoding="utf-8")
                timestamps[name].write("frame,sim_time_s\n")
            channels = 4 if message.step >= message.width * 4 else 3
            pixels = np.frombuffer(message.data, dtype=np.uint8).reshape(message.height, message.step)
            pixels = pixels[:, :message.width * channels].reshape(message.height, message.width, channels)
            if channels == 4:
                frame = cv2.cvtColor(pixels, cv2.COLOR_RGBA2BGR)
            else:
                frame = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
            writers[name].write(frame)
            count = frame_counts[name]
            stamp = message.header.stamp
            sim_time = float(stamp.sec) + float(stamp.nsec) * 1e-9
            timestamps[name].write(f"{count},{sim_time:.9f}\n")
            if count == 0:
                cv2.imwrite(str(args.output / f"{name}.png"), frame)
            frame_counts[name] += 1

    node = Node()
    for name in names:
        node.subscribe(Image, f"/riose/mvp3/camera/{name}",
                       lambda message, camera=name: receive(camera, message))

    def finish(_signum=None, _frame=None) -> None:
        with lock:
            for writer in writers.values():
                writer.release()
            for stream in timestamps.values():
                stream.flush()
                stream.close()
        summary = {"schema_version": "riose.mvp3.camera_capture/v1",
                   "provenance": "GAZEBO_OGRE2_IMAGE_TOPICS",
                   "timestamps": "Gazebo simulation time from gz.msgs.Image header",
                   "fps_encoding": args.fps,
                   "frames_by_camera": frame_counts,
                   "clips": {name: str(args.output / f"{name}.mp4")
                             for name, count in frame_counts.items() if count}}
        (args.output / "capture_manifest.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sys.stdout.flush()
        os._exit(0)

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    print(f"Recording Gazebo OGRE2 cameras into {args.output}", flush=True)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
