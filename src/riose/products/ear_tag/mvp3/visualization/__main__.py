"""Generate an offline MVP3 IMU replay viewer."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import create_viewer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=Path, help="MVP3 experiment with gazebo_imu.csv")
    parser.add_argument("--output", type=Path, help="HTML destination (default: experiment/imu_viewer.html)")
    args = parser.parse_args()
    print(create_viewer(args.experiment, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
