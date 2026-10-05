"""Create deterministic Gazebo joint trajectories from scenario phases."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .scenarios import Scenario


JOINTS = (
    "body_translation_joint",
    "head_pitch_joint",
    "ear_left_joint",
    "front_left_leg_joint",
    "front_right_leg_joint",
    "rear_left_leg_joint",
    "rear_right_leg_joint",
)


@dataclass(frozen=True)
class JointPoint:
    time_s: float
    positions: tuple[float, ...]


def trajectory_points(scenario: Scenario, *, sample_period_s: float = 0.05) -> list[JointPoint]:
    """Create smooth, time-based body/head/ear/leg commands.

    Gazebo's JointTrajectoryController interpolates between these setpoints;
    the animal is not repositioned by teleporting. Long runs use fewer control
    knots to keep the protobuf trajectory below OS command-line limits.
    """
    if not math.isfinite(sample_period_s) or sample_period_s <= 0:
        raise ValueError("sample_period_s must be finite and positive")
    if scenario.duration_s > 600:
        sample_period_s = max(sample_period_s, 0.25)
    points: list[JointPoint] = []
    elapsed = 0.0
    x_m = 0.0
    for phase in scenario.phases:
        if phase.duration_s <= 0:
            raise ValueError("phase duration must be positive")
        count = max(1, math.ceil(phase.duration_s / sample_period_s))
        start_x = x_m
        for index in range(count + 1):
            local_t = min(phase.duration_s, index * phase.duration_s / count)
            t = elapsed + local_t
            angle = 2 * math.pi * phase.leg_frequency_hz * local_t
            leg = 0.34 if phase.behavior == "WALKING" else 0.62
            stride = leg * math.sin(angle)
            positions = (
                start_x + phase.forward_speed_m_s * local_t,
                phase.head_amplitude_rad * math.sin(2 * math.pi * (1.8 if phase.behavior == "HEAD_SHAKE" else 0.45) * local_t),
                phase.ear_amplitude_rad * math.sin(2 * math.pi * max(phase.leg_frequency_hz, 1.8) * local_t + 0.4),
                stride,
                -stride,
                -stride,
                stride,
            )
            # Avoid duplicate timestamps at phase boundaries; the later phase
            # owns the boundary so a transition remains continuous in time.
            if points and abs(points[-1].time_s - t) < 1e-10:
                points[-1] = JointPoint(t, positions)
            else:
                points.append(JointPoint(t, positions))
        x_m = start_x + phase.forward_speed_m_s * phase.duration_s
        elapsed += phase.duration_s
    if len(points) < 2 or points[-1].time_s <= 0:
        raise ValueError("trajectory must contain at least two increasing-time points")
    return points


def to_gz_joint_trajectory(scenario: Scenario, *, sample_period_s: float = 0.05) -> str:
    """Serialize positions as a ``gz.msgs.JointTrajectory`` text message."""
    points = trajectory_points(scenario, sample_period_s=sample_period_s)
    lines = [*(f'joint_names: "{name}"' for name in JOINTS)]
    for point in points:
        seconds = int(point.time_s)
        nanos = round((point.time_s - seconds) * 1_000_000_000)
        if nanos >= 1_000_000_000:
            seconds += 1
            nanos -= 1_000_000_000
        lines.append("points {")
        lines.extend(f"  positions: {value:.9f}" for value in point.positions)
        lines.append("  time_from_start {")
        lines.append(f"    sec: {seconds}")
        lines.append(f"    nsec: {nanos}")
        lines.append("  }")
        lines.append("}")
    return "\n".join(lines)
