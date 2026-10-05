from __future__ import annotations

import gzip
import json
from pathlib import Path

from riose.products.ear_tag.mvp3.reporting import report_experiment


def _record(index: int = 0, *, pose_age_s: float = 0.001,
            angular_speed_rad_s: float | None = None) -> dict:
    sim_time = 2.32 + index * 0.02
    record = {
        "simulation_timestamp_s": sim_time,
        "time_from_capture_start_s": index * 0.02,
        "pose_sample_timestamp_s": sim_time + pose_age_s,
        "pose_join_status": "NEAREST_GAZEBO_POSE",
        # Gazebo protobuf JSON omits zero-valued root coordinates.
        "animal_pose": {"name": "riose_cow", "position": {}},
        "ear_pose": {"name": "ear_left", "position": {"x": 1.0, "y": 0.2, "z": 1.3}},
        "tag_pose": {"name": "riose_ear_tag", "position": {"x": 1.0, "y": 0.25, "z": 1.28}},
        "imu_acceleration_g": {"x": 0.0, "y": 0.0, "z": 1.0},
    }
    if angular_speed_rad_s is not None:
        record["pose_kinematics"] = {"entities": {
            "ear_left": {"angular_velocity_rad_s": {"x": 0.0, "y": angular_speed_rad_s, "z": 0.0}},
            "riose_ear_tag": {"angular_velocity_rad_s": {"x": 0.0, "y": angular_speed_rad_s, "z": 0.0}},
        }}
    return record


def _experiment(tmp_path: Path, records: list[dict], *, compressed: bool = False) -> Path:
    (tmp_path / "manifest.json").write_text(json.dumps({"scenario": "walking"}))
    (tmp_path / "gazebo_imu.csv").write_text(
        "timestamp_s,x_g,y_g,z_g\n" + "".join(f"{i * .02:.3f},0,0,1\n" for i in range(len(records))),
        encoding="utf-8",
    )
    (tmp_path / "recording_manifest.json").write_text(json.dumps({
        "schema_version": "riose.mvp3.recording/v1",
        "sample_count": len(records), "sample_rate_hz": 50,
        "streams": ["animal_pose", "ear_pose", "tag_pose", "imu_acceleration_g"],
    }))
    path = tmp_path / ("recording.jsonl.gz" if compressed else "recording.jsonl")
    opener = gzip.open if compressed else open
    with opener(path, "wt", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")
    return tmp_path


def test_recording_gate_validates_joined_data_cadence_and_pose_age(tmp_path: Path) -> None:
    _experiment(tmp_path, [_record(0), _record(1)])

    result = report_experiment(tmp_path)

    assert result["checks"]["recording_operational"]["status"] == "PASS"


def test_recording_gate_accepts_compressed_jsonl(tmp_path: Path) -> None:
    _experiment(tmp_path, [_record()], compressed=True)

    result = report_experiment(tmp_path)

    assert result["checks"]["recording_operational"]["status"] == "PASS"


def test_recording_gate_rejects_empty_ear_or_tag_pose_and_pose_time_clamping(tmp_path: Path) -> None:
    invalid_pose = _record()
    invalid_pose["tag_pose"]["position"] = {}
    _experiment(tmp_path, [invalid_pose])
    result = report_experiment(tmp_path)
    assert result["checks"]["recording_operational"]["status"] == "NOT_VALIDATED"

    invalid_age = _record(pose_age_s=0.2)
    _experiment(tmp_path, [invalid_age])
    result = report_experiment(tmp_path)
    assert result["checks"]["recording_operational"]["status"] == "NOT_VALIDATED"


def test_recording_gate_rejects_manifest_count_and_nonmonotonic_timestamp_mismatches(tmp_path: Path) -> None:
    records = [_record(0), _record(1)]
    _experiment(tmp_path, records)
    manifest_path = tmp_path / "recording_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sample_count"] = 100
    manifest_path.write_text(json.dumps(manifest))
    assert report_experiment(tmp_path)["checks"]["recording_operational"]["status"] == "NOT_VALIDATED"

    records[1]["simulation_timestamp_s"] = records[0]["simulation_timestamp_s"]
    _experiment(tmp_path, records)
    assert report_experiment(tmp_path)["checks"]["recording_operational"]["status"] == "NOT_VALIDATED"


def test_attachment_gate_requires_consistent_numeric_drift_evidence(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({
        "scenario": "standing", "ear_attachment_validated": True,
    }))
    (tmp_path / "attachment_metrics.json").write_text(json.dumps({
        "status": "SIMULATED", "result": "FAIL",
        "relative_pivot_position_drift_max_m": 0.2,
        "validation_tolerance_m": 0.005, "samples": 50,
    }))

    result = report_experiment(tmp_path)

    assert result["checks"]["ear_attachment_operational"]["status"] == "NOT_VALIDATED"


def test_attachment_gate_accepts_measured_simulated_pivot_evidence(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({
        "scenario": "walking", "ear_attachment_validated": True,
    }))
    (tmp_path / "attachment_metrics.json").write_text(json.dumps({
        "status": "SIMULATED", "result": "PASS",
        "relative_pivot_position_drift_max_m": 0.0002,
        "validation_tolerance_m": 0.005, "samples": 100,
    }))

    result = report_experiment(tmp_path)

    assert result["checks"]["ear_attachment_operational"]["status"] == "PASS"


def test_standing_run_does_not_claim_movement_without_observed_kinematics(tmp_path: Path) -> None:
    _experiment(tmp_path, [_record()])
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "scenario": "standing", "motion_controller_completed": True,
        "animal_motion_metrics": {"expected_forward_displacement_m": 0,
                                   "measured_root_displacement_m": 0},
    }))

    result = report_experiment(tmp_path)

    assert result["checks"]["physical_movement_generated"]["status"] == "NOT_VALIDATED"


def test_articulated_motion_gate_requires_nonzero_recorded_pose_kinematics(tmp_path: Path) -> None:
    _experiment(tmp_path, [_record(0, angular_speed_rad_s=0.0),
                           _record(1, angular_speed_rad_s=0.5)])
    (tmp_path / "manifest.json").write_text(json.dumps({
        "scenario": "head_shake",
        "animal_motion_metrics": {"expected_forward_displacement_m": 0,
                                   "measured_root_displacement_m": 0},
    }))

    result = report_experiment(tmp_path)

    assert result["checks"]["physical_movement_generated"]["status"] == "PASS"


def test_boot_only_tx_does_not_pass_movement_wake_and_tx_gate(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({
        "scenario": "walking", "firmware_wake_tx_validated": True,
    }))
    (tmp_path / "firmware_replay.json").write_text(json.dumps({
        "result": "PASS", "boot_tx_count": 1,
        "movement_derived_wakeup": "NOT_VALIDATED",
        "radio_transmission_from_movement": "NOT_VALIDATED",
        "tests": {"failed": 0, "skipped": 0},
    }))

    result = report_experiment(tmp_path)

    assert result["checks"]["firmware_wake_tx_operational"]["status"] == "NOT_VALIDATED"


def test_live_lockstep_firmware_evidence_can_validate_movement_wake_tx(tmp_path: Path) -> None:
    live = {
        "schema_version": "riose.mvp3.live_firmware_evidence/v1",
        "status": "SIMULATED_APPROXIMATE_HIGH_PASS_WU",
        "sample_injection_count": 800,
        "wakeup_generated_event_count": 2,
        "wakeup_event_read_count": 2,
        "radio_tx_count": 3,
        "radio_tx_done_count": 3,
        "movement_derived_tx_count": 2,
        "firmware_final_state": "SLEEP",
        "wakeup_irq_asserted_at_end": False,
        "firmware_wake_tx_validated": True,
    }
    (tmp_path / "manifest.json").write_text(json.dumps({
        "scenario": "02_walking",
        "clock_sync": {"live_lockstep": True},
        "firmware_clock_sync": {"sample_injection_count": 800},
        "firmware_live_evidence": live,
    }))

    result = report_experiment(tmp_path)

    assert result["checks"]["renode_bridge_operational"]["status"] == "PASS"
    assert result["checks"]["firmware_wake_tx_operational"]["status"] == "PASS"


def test_live_standing_validates_quiet_sleep_and_boot_tx(tmp_path: Path) -> None:
    live = {
        "schema_version": "riose.mvp3.live_firmware_evidence/v1",
        "status": "SIMULATED_APPROXIMATE_HIGH_PASS_WU",
        "sample_injection_count": 501,
        "wakeup_generated_event_count": 0,
        "wakeup_event_read_count": 0,
        "radio_tx_count": 1,
        "radio_tx_done_count": 1,
        "movement_derived_tx_count": 0,
        "firmware_final_state": "SLEEP",
        "wakeup_irq_asserted_at_end": False,
        "firmware_wake_tx_validated": False,
    }
    (tmp_path / "manifest.json").write_text(json.dumps({
        "scenario": "01_standing",
        "clock_sync": {"live_lockstep": True},
        "firmware_clock_sync": {"sample_injection_count": 501},
        "firmware_live_evidence": live,
    }))

    result = report_experiment(tmp_path)

    check = result["checks"]["firmware_wake_tx_operational"]
    assert check["status"] == "PASS"
    assert "remained quiet" in check["evidence"]


def test_report_surfaces_power_window_charge_and_electrical_status(tmp_path: Path) -> None:
    _experiment(tmp_path, [_record()])
    power_dir = tmp_path / "power"
    power_dir.mkdir()
    (power_dir / "power_summary.json").write_text(json.dumps({
        "status": "COMPLETED", "result_class": "SIMULATED",
        "modeled_window_s": 3600.0, "modeled_charge_uah": 12.5,
        "charge_status": "SIMULATED_AT_ASSUMED_REGULATOR_OUTPUT_VOLTAGE",
        "electrical_metrics_status": "PASS",
    }))

    result = report_experiment(tmp_path)

    assert result["power_analysis"]["modeled_window_s"] == 3600.0
    assert result["power_analysis"]["modeled_charge_uah"] == 12.5
    assert result["power_analysis"]["electrical_metrics_status"] == "PASS"
    assert "Modeled window: 3600.0 s" in (tmp_path / "report.md").read_text()


def test_rf_and_power_gates_require_validated_artifacts(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({"scenario": "radio-event"}))
    (tmp_path / "anchor_events.jsonl").write_text(json.dumps({"event_type": "fake"}) + "\n")
    (tmp_path / "power_summary.json").write_text(json.dumps({"status": "COMPLETED"}))

    result = report_experiment(tmp_path)

    assert result["checks"]["rf_anchor_operational"]["status"] == "NOT_VALIDATED"
    assert result["checks"]["power_integration_operational"]["status"] == "NOT_VALIDATED"
