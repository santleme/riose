from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from riose.products.ear_tag.mvp3.visualization import create_viewer
from riose.products.ear_tag.mvp3.visualization.live_dashboard import LiveDashboard


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _experiment(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "gazebo_imu.csv").write_text(
        "timestamp_s,x_g,y_g,z_g\n0,0,0,1\n1,1,0,0\n", encoding="utf-8")
    _write_jsonl(path / "recording.jsonl", [
        {"simulation_timestamp_s": 0.01, "pose_sample_timestamp_s": 0.0,
         "tag_pose": {"position": {"x": 1, "y": 2, "z": 3}}},
        {"simulation_timestamp_s": 1.01, "pose_sample_timestamp_s": 1.0,
         "tag_pose": {"position": {"x": 2, "y": 3, "z": 4}}},
    ])
    _write_jsonl(path / "firmware_trace.jsonl", [
        {"schema_version": "riose.firmware.trace/v1", "status": "SIMULATED",
         "timestamp_us": 100_000, "event": "BOOT"},
        {"schema_version": "riose.firmware.trace/v1", "status": "SIMULATED",
         "timestamp_us": 200_000, "event": "TX_START"},
    ])
    _write_jsonl(path / "rf_events.jsonl", [
        {"event_type": "LOGICAL_RF_EVENT", "status": "SIMULATED",
         "timestamp_us": 200_000, "completion_timestamp_us": 230_000, "sequence": 7},
    ])
    _write_jsonl(path / "anchor_events.jsonl", [
        {"event_type": "ANCHOR_LOGICAL_RF_EVENT_ACCEPTED", "accepted": True,
         "timestamp_us": 200_000, "sequence": 7},
    ])
    (path / "power").mkdir()
    _write_jsonl(path / "power" / "schedule.jsonl", [
        {"status": "SIMULATED", "timestamp_s": 0.2, "duration_s": 0.03,
         "event": "TX", "component": "sx1262", "duration_source": "TRACE_TIMESTAMP",
         "trace_provenance": {"schema_version": "riose.firmware.trace/v1"}},
    ])
    (path / "manifest.json").write_text(json.dumps({"scenario": "unit-test"}), encoding="utf-8")


def _write_clock_mapping(path: Path, *, alignment: dict | None = None, status: str = "SIMULATED") -> None:
    replay = {
        "clock_mapping": {
            "schema_version": "riose.mvp3.clock_mapping/v1",
            "status": status,
            "source_clock": "RENODE_VIRTUAL_TIME",
            "target_clock": "GAZEBO_SIMULATION_TIME",
            "scale": 1.0,
            "offset_s": -0.25,
            "method": "AFFINE_OFFLINE_RESD_REPLAY",
            "live_lockstep": False,
        }
    }
    if alignment is not None:
        replay["clock_mapping"]["alignment_check"] = alignment
    (path / "firmware_replay.json").write_text(json.dumps(replay), encoding="utf-8")


def test_offline_view_renders_timestamped_streams_without_cross_clock_join(tmp_path: Path) -> None:
    _experiment(tmp_path)
    path = create_viewer(tmp_path)
    document = path.read_text(encoding="utf-8")

    assert "Gazebo simulation time" in document
    assert "Firmware-clock event lanes" in document
    assert "No valid clock_mapping is recorded" in document
    assert "firmware_trace.jsonl" in document
    assert "rf_events.jsonl" in document
    assert "anchor_events.jsonl" in document
    assert "power/schedule.jsonl" in document
    assert "logical acceptance" in document
    assert "not physical RF reception" in document
    assert "Top · X/Y" in document and "Side · X/Z" in document and "Front · Y/Z" in document
    assert '"timestamp_s":0.0' in document
    assert path.with_suffix(".svg").is_file()
    # There is no nearest-event/pose-derived data structure or pose attached to an event.
    assert '"pose":' not in document


def test_missing_event_streams_are_omitted_without_blocking_pose_imu_view(tmp_path: Path) -> None:
    _experiment(tmp_path)
    for name in ("firmware_trace.jsonl", "rf_events.jsonl", "anchor_events.jsonl"):
        (tmp_path / name).unlink()
    (tmp_path / "power" / "schedule.jsonl").unlink()
    document = create_viewer(tmp_path).read_text(encoding="utf-8")
    assert "No timestamped firmware, logical RF, anchor, or power records" in document
    assert "Ear-tag position" in document


@pytest.mark.parametrize("mutate,match", [
    (lambda rows: rows[0].update(timestamp_us="100"), "timestamp_us"),
    (lambda rows: rows[0].update(status="MEASURED"), "provenance"),
])
def test_invalid_firmware_event_evidence_fails_closed(tmp_path: Path, mutate, match: str) -> None:
    _experiment(tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "firmware_trace.jsonl").read_text().splitlines()]
    mutate(rows)
    _write_jsonl(tmp_path / "firmware_trace.jsonl", rows)
    with pytest.raises(ValueError, match=match):
        create_viewer(tmp_path)


def test_pose_without_explicit_timestamps_is_rejected(tmp_path: Path) -> None:
    _experiment(tmp_path)
    _write_jsonl(tmp_path / "recording.jsonl", [{"tag_pose": {"position": {"x": 1, "y": 2, "z": 3}}}])
    with pytest.raises(ValueError, match="simulation_timestamp_s"):
        create_viewer(tmp_path)


def test_logical_anchor_row_must_be_accepted_and_timestamped(tmp_path: Path) -> None:
    _experiment(tmp_path)
    _write_jsonl(tmp_path / "anchor_events.jsonl", [{
        "event_type": "ANCHOR_LOGICAL_RF_EVENT_ACCEPTED", "accepted": False, "timestamp_us": 200_000,
    }])
    with pytest.raises(ValueError, match="accepted timestamp-backed"):
        create_viewer(tmp_path)


def test_valid_offline_clock_mapping_overlays_events_and_recomputes_alignment(tmp_path: Path) -> None:
    _experiment(tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "firmware_trace.jsonl").read_text().splitlines()]
    rows[1]["timestamp_us"] = 300_000
    _write_jsonl(tmp_path / "firmware_trace.jsonl", rows)
    check = {
        "status": "RECOMPUTED_FROM_RECORDS",
        "renode_tx_start_s": 0.3,
        "mapped_gazebo_time_s": 0.05,
        "nearest_imu_sample_time_s": 0.0,
        "absolute_error_s": 0.05,
    }
    _write_clock_mapping(tmp_path, alignment=check)

    document = create_viewer(tmp_path).read_text(encoding="utf-8")
    assert "Valid SIMULATED affine offline replay mapping" in document
    assert "not live lockstep" in document
    assert "Gazebo simulation-time event lanes" in document
    assert "0.300000 s Renode → 0.050000 s Gazebo" in document
    assert "nearest IMU sample 0.000000000 s (absolute error 50.000 ms)" in document
    # The mapped TX_START marker uses the same 0–1 s domain as the Gazebo data.
    assert '<circle cx="255.50" cy="42"' in document


def test_live_lockstep_mapping_uses_validated_barrier_evidence(tmp_path: Path) -> None:
    _experiment(tmp_path)
    evidence = tmp_path / "live_clock_evidence.jsonl"
    _write_jsonl(evidence, [
        {"event": "CLOCK_BARRIER", "simulation_timestamp_s": "0.02",
         "renode_timestamp_s": "0.02", "clock_error_s": "0.000"},
        {"event": "CLOCK_BARRIER", "simulation_timestamp_s": "0.04",
         "renode_timestamp_s": "0.04", "clock_error_s": "0.000"},
    ])
    (tmp_path / "manifest.json").write_text(json.dumps({
        "scenario": "walking",
        "clock_mapping": {
            "schema_version": "riose.mvp3.clock_mapping/v1",
            "status": "SIMULATED",
            "source_clock": "RENODE_VIRTUAL_TIME",
            "target_clock": "GAZEBO_SIMULATION_TIME",
            "scale": 1.0,
            "offset_s": 0.0,
            "method": "LIVE_GAZEBO_MASTER_LOCKSTEP",
            "live_lockstep": True,
            "barrier_evidence": str(evidence),
        },
    }), encoding="utf-8")

    document = create_viewer(tmp_path).read_text(encoding="utf-8")

    assert "Valid SIMULATED live lockstep mapping" in document
    assert "Gazebo simulation-time event lanes" in document
    assert "0.200000 s Renode → 0.200000 s Gazebo" in document


@pytest.mark.parametrize("mapping_changes", [
    {"status": "MEASURED"},
    {"offset_s": "-0.25"},
    {"live_lockstep": True},
    {"schema_version": "unknown/v1"},
])
def test_invalid_clock_mapping_does_not_overlay_events(tmp_path: Path, mapping_changes: dict) -> None:
    _experiment(tmp_path)
    replay = {
        "clock_mapping": {
            "schema_version": "riose.mvp3.clock_mapping/v1",
            "status": "SIMULATED",
            "source_clock": "RENODE_VIRTUAL_TIME",
            "target_clock": "GAZEBO_SIMULATION_TIME",
            "scale": 1.0,
            "offset_s": -0.25,
            "method": "AFFINE_OFFLINE_RESD_REPLAY",
            "live_lockstep": False,
        }
    }
    replay["clock_mapping"].update(mapping_changes)
    (tmp_path / "firmware_replay.json").write_text(json.dumps(replay), encoding="utf-8")
    document = create_viewer(tmp_path).read_text(encoding="utf-8")
    assert "Clock mapping was not applied" in document
    assert "Firmware-clock event lanes" in document
    assert "Gazebo simulation-time event lanes" not in document


def test_claimed_alignment_check_mismatch_disables_valid_mapping(tmp_path: Path) -> None:
    _experiment(tmp_path)
    _write_clock_mapping(tmp_path, alignment={
        "status": "RECOMPUTED_FROM_RECORDS", "renode_tx_start_s": 0.2,
        "mapped_gazebo_time_s": 0.9, "nearest_imu_sample_time_s": 1.0,
        "absolute_error_s": 0.1,
    })
    document = create_viewer(tmp_path).read_text(encoding="utf-8")
    assert "alignment_check values do not match" in document
    assert "Firmware-clock event lanes" in document
    assert "Gazebo simulation-time event lanes" not in document


def test_live_dashboard_serves_loopback_simulated_status(tmp_path: Path) -> None:
    dashboard = LiveDashboard(tmp_path)
    try:
        dashboard.update({"firmware_state": "SLEEP", "simulation_time_s": 1.25})
        with urllib.request.urlopen(dashboard.url + "api/status", timeout=2) as response:
            status = json.loads(response.read())
            assert response.headers["Cache-Control"] == "no-store"
        assert status["provenance"] == "SIMULATED"
        assert status["firmware_state"] == "SLEEP"
        assert status["simulation_time_s"] == 1.25
        assert dashboard._server.server_address[0] == "127.0.0.1"
        html = Path(dashboard.html_path).read_text(encoding="utf-8")
        assert "not an RF wave" in html
        assert "not modeled" in html
    finally:
        dashboard.close()
import json
from pathlib import Path

from riose.products.ear_tag.mvp3.visualization.cinematic import _camera_request, _quaternion


MVP3 = Path(__file__).parents[2] / "src" / "riose" / "products" / "ear_tag" / "mvp3"


def test_camera_presets_cover_named_visual_views_and_valid_targets():
    config = json.loads((MVP3 / "visualization" / "camera_presets.json").read_text())
    presets = config["presets"]
    required = {
        "CAM_TAG_HERO", "CAM_TAG_MACRO", "CAM_TAG_3Q_FRONT", "CAM_TAG_SIDE",
        "CAM_TAG_3Q_REAR", "CAM_TAG_REAR", "CAM_CATTLE_TAG_CLOSE",
        "CAM_CATTLE_MEDIUM", "CAM_FIELD_WIDE", "CAM_GATEWAY", "CAM_TECHNICAL",
    }
    assert required <= presets.keys()
    for preset in presets.values():
        assert len(preset["position"]) == len(preset["look_at"]) == 3
        assert preset["fov_deg"] > 0 and preset["duration_s"] > 0
        assert preset["position"] != preset["look_at"]


def test_camera_quaternion_points_local_forward_axis_at_target():
    position = [2.0, -1.0, 3.0]
    target = [-2.0, 5.0, 0.5]
    x, y, z, w = _quaternion(position, target)
    # Rotate camera-local +X by the camera quaternion, then normalize target ray.
    forward = (1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w))
    ray = [target[i] - position[i] for i in range(3)]
    norm = sum(value * value for value in ray) ** 0.5
    ray = [value / norm for value in ray]
    assert all(abs(forward[i] - ray[i]) < 1e-9 for i in range(3))
    request = _camera_request({"position": position, "look_at": target})
    assert "orientation" in request and "position" in request


def test_visual_sequences_are_bounded_and_reference_camera_presets():
    cameras = json.loads((MVP3 / "visualization" / "camera_presets.json").read_text())["presets"]
    sequences = json.loads((MVP3 / "visualization" / "sequences.json").read_text())["sequences"]
    assert set(sequences) == {"pitch_short", "product_demo", "engineering"}
    assert 20 <= sum(segment["duration_s"] for segment in sequences["pitch_short"]) <= 30
    assert 45 <= sum(segment["duration_s"] for segment in sequences["product_demo"]) <= 60
    for sequence in sequences.values():
        assert all(segment["camera"] in cameras for segment in sequence)


def test_visual_lighting_presets_exist_and_keep_three_distinct_rig_profiles():
    presets = json.loads((MVP3 / "visualization" / "lighting_presets.json").read_text())["presets"]
    assert set(presets) == {"DAY", "OVERCAST_TECH", "GOLDEN_HOUR"}
    for preset in presets.values():
        assert set(preset) == {"ambient", "background", "sun_diffuse", "sun_specular", "sun_direction"}
        assert all(len(value) == 3 for value in preset.values())
