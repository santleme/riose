from __future__ import annotations

import math

from riose.products.ear_tag.mvp3.cli import _pose_kinematics


def test_pose_kinematics_derives_world_linear_motion() -> None:
    samples = [
        (0.0, {"tag": {"position": {"x": 0, "y": 0, "z": 0},
                      "orientation": {"w": 1}}}),
        (1.0, {"tag": {"position": {"x": 2, "y": 0, "z": 0},
                      "orientation": {"w": 1}}}),
        (2.0, {"tag": {"position": {"x": 4, "y": 0, "z": 0},
                      "orientation": {"w": 1}}}),
    ]

    result = _pose_kinematics(samples)

    assert result[1]["provenance"] == "DERIVED_FINITE_DIFFERENCE"
    assert result[1]["entities"]["tag"]["linear_velocity_m_s"] == {
        "x": 2.0, "y": 0.0, "z": 0.0}
    assert result[1]["entities"]["tag"]["linear_acceleration_m_s2"] == {
        "x": 0.0, "y": 0.0, "z": 0.0}


def test_pose_kinematics_derives_world_angular_velocity() -> None:
    samples = []
    for timestamp, angle in ((0.0, 0.0), (1.0, 0.5), (2.0, 1.0)):
        samples.append((timestamp, {"tag": {
            "position": {"x": 0, "y": 0, "z": 0},
            "orientation": {"x": 0, "y": 0,
                            "z": math.sin(angle / 2), "w": math.cos(angle / 2)},
        }}))

    result = _pose_kinematics(samples)

    angular = result[1]["entities"]["tag"]["angular_velocity_rad_s"]
    assert angular["x"] == 0.0
    assert angular["y"] == 0.0
    assert math.isclose(angular["z"], 0.5, abs_tol=1e-9)


def test_pose_kinematics_marks_missing_derivative_when_time_does_not_advance() -> None:
    samples = [
        (1.0, {"tag": {"position": {"x": 0}, "orientation": {"w": 1}}}),
        (1.0, {"tag": {"position": {"x": 1}, "orientation": {"w": 1}}}),
    ]

    result = _pose_kinematics(samples)

    assert result[0]["entities"]["tag"]["linear_velocity_m_s"] is None
    assert result[1]["entities"]["tag"]["angular_velocity_rad_s"] is None
