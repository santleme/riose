from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from riose.products.ear_tag.mvp3.cli import _read_pose_samples
from riose.products.ear_tag.mvp3.motion import JOINTS, to_gz_joint_trajectory, trajectory_points
from riose.products.ear_tag.mvp3.scenarios import SCENARIOS, get_scenario


def test_required_mvp3_scenarios_are_present() -> None:
    required = {
        "01_standing", "02_walking", "03_running", "04_head_shake",
        "05_mixed_activity", "06_heavy_tag", "07_attachment_variation",
        "08_radio_event", "09_long_simulation",
    }
    assert required <= SCENARIOS.keys()


def test_alias_and_scenario_duration() -> None:
    assert get_scenario("walking").name == "02_walking"
    assert get_scenario("09_long_simulation").duration_s == 3600.0


def test_motion_plan_has_increasing_time_and_configured_joint_order() -> None:
    scenario = get_scenario("04_head_shake")
    points = trajectory_points(scenario, sample_period_s=0.1)
    assert points[0].time_s == 0
    assert all(right.time_s > left.time_s for left, right in zip(points, points[1:]))
    assert all(len(point.positions) == len(JOINTS) for point in points)
    assert points[-1].time_s == scenario.duration_s


def test_gazebo_trajectory_contains_only_physical_joint_targets() -> None:
    message = to_gz_joint_trajectory(get_scenario("walking"), sample_period_s=0.2)
    for joint in JOINTS:
        assert f'joint_names: "{joint}"' in message
    assert "time_from_start" in message
    assert "WALKING" not in message
    assert "positions:" in message


def test_pose_reader_streams_compact_focus_entities_at_requested_rate(tmp_path: Path) -> None:
    path = tmp_path / "poses.jsonl"
    rows = []
    for stamp in (0.0, 0.01, 0.04, 0.08, 0.09):
        seconds = int(stamp)
        nanos = int(round((stamp - seconds) * 1e9))
        rows.append({"header": {"stamp": {"sec": seconds, "nsec": nanos}},
                     "pose": [
                         {"name": "riose_cow", "position": {"x": stamp},
                          "orientation": {"w": 1}},
                         {"name": "riose_ear_tag", "position": {"x": stamp + 1},
                          "orientation": {"w": 1}},
                         {"name": "ground", "position": {}, "orientation": {"w": 1}},
                     ]})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    parsed = _read_pose_samples(path, max_rate_hz=25.0,
                                focus_names={"riose_cow", "riose_ear_tag"})

    assert [row[0] for row in parsed] == pytest.approx([0.0, 0.04, 0.08, 0.09])
    assert all(set(poses) == {"riose_cow", "riose_ear_tag"} for _, poses in parsed)
    assert set(parsed[0][1]["riose_cow"]) == {"name", "position", "orientation"}
    assert parsed[0][1]["riose_cow"]["name"] == "riose_cow"


def test_scenario_trajectory_is_repeatable_for_same_seed_and_independent_of_trace_labels() -> None:
    scenario = get_scenario("scenario_random_activity")
    first = to_gz_joint_trajectory(scenario, sample_period_s=0.1)
    replay = to_gz_joint_trajectory(scenario, sample_period_s=0.1)
    changed_seed = to_gz_joint_trajectory(replace(scenario, seed=scenario.seed + 1), sample_period_s=0.1)

    assert first == replay == changed_seed
    assert "STANDING" not in first
    assert "WALKING" not in first
