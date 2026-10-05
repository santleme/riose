"""Fail-closed evidence inventory and MVP3 integration gate."""
from __future__ import annotations

import csv
import gzip
import json
import math
from pathlib import Path
from typing import Any


GATE_REQUIREMENTS = (
    "gazebo_operational", "animal_model_operational", "ear_attachment_operational",
    "physical_movement_generated", "imu_integration_operational", "renode_bridge_operational",
    "firmware_wake_tx_operational", "rf_anchor_operational", "power_integration_operational",
    "recording_operational", "scenario_completed",
)


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _recording_is_valid(recording_manifest_path: Path, recording_path: Path,
                        imu_count: int) -> bool:
    """Check the joined stream contents, count, cadence, and pose timestamp age."""
    try:
        recording_manifest = json.loads(recording_manifest_path.read_text(encoding="utf-8"))
        if recording_manifest.get("schema_version") != "riose.mvp3.recording/v1":
            return False
        required_streams = {"animal_pose", "ear_pose", "tag_pose", "imu_acceleration_g"}
        if not required_streams.issubset(recording_manifest.get("streams", [])):
            return False
        sample_count = recording_manifest.get("sample_count")
        sample_rate_hz = recording_manifest.get("sample_rate_hz")
        if (not isinstance(sample_count, int) or isinstance(sample_count, bool)
                or sample_count <= 0 or sample_count != imu_count
                or not _finite_number(sample_rate_hz) or sample_rate_hz <= 0):
            return False

        opener = gzip.open if recording_path.suffix == ".gz" else open
        count = 0
        previous_time = None
        first_time = None
        join_tolerance = max(0.025, 0.5 / float(sample_rate_hz))
        with opener(recording_path, "rt", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    return False
                sim_time = record.get("simulation_timestamp_s")
                pose_time = record.get("pose_sample_timestamp_s")
                capture_time = record.get("time_from_capture_start_s")
                if not all(_finite_number(value) for value in (sim_time, pose_time, capture_time)):
                    return False
                sim_time, pose_time, capture_time = map(float, (sim_time, pose_time, capture_time))
                if (record.get("pose_join_status") != "NEAREST_GAZEBO_POSE"
                        or abs(sim_time - pose_time) > join_tolerance):
                    return False
                if previous_time is not None and sim_time <= previous_time:
                    return False
                if first_time is None:
                    first_time = sim_time
                    if abs(capture_time) > 1e-6:
                        return False
                elif abs((sim_time - first_time) - capture_time) > 1e-6:
                    return False
                previous_time = sim_time

                animal = record.get("animal_pose")
                ear = record.get("ear_pose")
                tag = record.get("tag_pose")
                acceleration = record.get("imu_acceleration_g")
                if (not isinstance(animal, dict) or animal.get("name") != "riose_cow"
                        or not isinstance(ear, dict) or ear.get("name") != "ear_left"
                        or not isinstance(tag, dict) or tag.get("name") != "riose_ear_tag"
                        or not isinstance(acceleration, dict)
                        or not all(_finite_number(acceleration.get(axis)) for axis in "xyz")):
                    return False
                for pose in (ear, tag):
                    position = pose.get("position")
                    if (not isinstance(position, dict)
                            or not any(axis in position for axis in "xyz")
                            or any(not _finite_number(value) for value in position.values())):
                        return False
                count += 1
        return count == sample_count
    except (OSError, StopIteration, json.JSONDecodeError, TypeError, ValueError):
        return False


def _attachment_is_valid(manifest: dict[str, Any], metrics: dict[str, Any]) -> bool:
    drift = metrics.get("relative_pivot_position_drift_max_m")
    tolerance = metrics.get("validation_tolerance_m")
    samples = metrics.get("samples")
    return (
        manifest.get("ear_attachment_validated") is True
        and metrics.get("status") == "SIMULATED"
        and metrics.get("result") == "PASS"
        and _finite_number(drift) and _finite_number(tolerance)
        and float(drift) <= float(tolerance)
        and isinstance(samples, int) and not isinstance(samples, bool) and samples > 0
    )


def _power_integration_is_valid(experiment: Path) -> bool:
    summary_path = experiment / "power" / "power_summary.json"
    if not summary_path.is_file():
        summary_path = experiment / "power_summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        schedule = Path(summary["outputs"]["schedule"])
        native_summary = Path(summary["native_summary"])
        native = json.loads(native_summary.read_text(encoding="utf-8"))
        schedule_rows = [json.loads(line) for line in schedule.read_text(encoding="utf-8").splitlines()
                         if line.strip()]
        return (
            summary.get("schema_version") == "riose.mvp3.power_summary/v1"
            and summary.get("status") == "COMPLETED"
            and summary.get("result_class") == "SIMULATED"
            and summary.get("assumptions", {}).get("load_profile_status") == "ASSUMED"
            and summary.get("trace", {}).get("status") == "SIMULATED"
            and native.get("status") == "SIMULATED"
            and bool(schedule_rows)
            and all(isinstance(row, dict) and row.get("status") == "SIMULATED"
                    and isinstance(row.get("event"), str) for row in schedule_rows)
        )
    except (OSError, KeyError, json.JSONDecodeError, TypeError):
        return False


def _anchor_events_are_valid(experiment: Path, manifest: dict[str, Any]) -> tuple[bool, int]:
    path = experiment / "anchor_events.jsonl"
    if not path.is_file():
        return False, 0
    count = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            physical = event.get("physical_rf_result", {})
            if (event.get("event_type") != "ANCHOR_LOGICAL_RF_EVENT_ACCEPTED"
                    or event.get("provenance") != "LOGICAL_RF_EVENT"
                    or event.get("accepted") is not True
                    or physical.get("status") != "ANTENNA_MODEL_UNVALIDATED"
                    or event.get("rssi_dbm") is not None):
                return False, count
            count += 1
    except (OSError, json.JSONDecodeError, AttributeError):
        return False, count
    expected = manifest.get("anchor_receive_count")
    return count > 0 and (expected is None or expected == count), count


def _movement_is_observed(recording_path: Path, motion_metrics: dict[str, Any]) -> tuple[bool, str]:
    expected = motion_metrics.get("expected_forward_displacement_m")
    measured = motion_metrics.get("measured_root_displacement_m")
    if _finite_number(expected) and float(expected) > 0:
        passed = _finite_number(measured) and float(measured) >= 0.05
        return passed, (f"cow root moved {float(measured):.3f} m; requested forward displacement "
                        f"{float(expected):.3f} m" if _finite_number(measured)
                        else "cow root displacement was not recorded")

    if not recording_path.is_file():
        return False, "no joined pose recording available to verify articulated motion"
    peak_angular_speed = 0.0
    peak_linear_speed = 0.0
    try:
        opener = gzip.open if recording_path.suffix == ".gz" else open
        with opener(recording_path, "rt", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                entities = record.get("pose_kinematics", {}).get("entities", {})
                for name in ("ear_left", "riose_ear_tag"):
                    entity = entities.get(name, {})
                    for field, accumulator in (("angular_velocity_rad_s", "angular"),
                                               ("linear_velocity_m_s", "linear")):
                        vector = entity.get(field, {})
                        if not isinstance(vector, dict):
                            continue
                        magnitude = math.sqrt(sum(float(vector.get(axis, 0.0)) ** 2 for axis in "xyz"))
                        if accumulator == "angular":
                            peak_angular_speed = max(peak_angular_speed, magnitude)
                        else:
                            peak_linear_speed = max(peak_linear_speed, magnitude)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False, "pose kinematics could not be read"
    passed = peak_angular_speed >= 0.05 or peak_linear_speed >= 0.02
    return passed, (f"peak ear/tag angular speed {peak_angular_speed:.3f} rad/s; "
                    f"linear speed {peak_linear_speed:.3f} m/s")


def _firmware_wake_tx_is_valid(experiment: Path, manifest: dict[str, Any]) -> bool:
    live = manifest.get("firmware_live_evidence")
    if isinstance(live, dict):
        try:
            common_valid = (
                live.get("schema_version") == "riose.mvp3.live_firmware_evidence/v1"
                and live.get("status") == "SIMULATED_APPROXIMATE_HIGH_PASS_WU"
                and manifest.get("clock_sync", {}).get("live_lockstep") is True
                and _finite_number(live.get("sample_injection_count"))
                and live["sample_injection_count"] > 0
                and live.get("firmware_final_state") == "SLEEP"
                and live.get("wakeup_irq_asserted_at_end") is False
                and _finite_number(live.get("radio_tx_count"))
                and live.get("radio_tx_done_count") == live.get("radio_tx_count")
            )
            if not common_valid:
                return False
            # A deliberate standing run should validate quiescence: physical
            # IMU data reached the live Zephyr session, but it caused no motion
            # wake or post-boot packet. Boot TX must still complete normally.
            if manifest.get("scenario") in {"standing", "01_standing"}:
                return (
                    live.get("wakeup_generated_event_count") == 0
                    and live.get("wakeup_event_read_count") == 0
                    and live.get("movement_derived_tx_count") == 0
                    and live.get("radio_tx_count") == 1
                )
            return (
                common_valid
                and live.get("firmware_wake_tx_validated") is True
                and _finite_number(live.get("wakeup_generated_event_count"))
                and live["wakeup_generated_event_count"] > 0
                and _finite_number(live.get("wakeup_event_read_count"))
                and live["wakeup_event_read_count"] > 0
                and _finite_number(live.get("movement_derived_tx_count"))
                and live["movement_derived_tx_count"] > 0
                and live.get("firmware_final_state") == "SLEEP"
                and live.get("wakeup_irq_asserted_at_end") is False
            )
        except (KeyError, TypeError, AttributeError):
            return False
    replay_path = experiment / "firmware_replay.json"
    if not replay_path.is_file() or manifest.get("firmware_wake_tx_validated") is not True:
        return False
    try:
        replay = json.loads(replay_path.read_text(encoding="utf-8"))
        return (
            replay.get("result") == "PASS"
            and replay.get("movement_derived_wakeup") == "VALIDATED"
            and isinstance(replay.get("movement_derived_tx_count"), int)
            and not isinstance(replay.get("movement_derived_tx_count"), bool)
            and replay["movement_derived_tx_count"] > 0
            and replay.get("tests", {}).get("failed") == 0
            and replay.get("tests", {}).get("skipped") == 0
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def report_experiment(experiment: Path) -> dict[str, Any]:
    """Summarize artifacts without inferring successful integration from files alone."""
    experiment = experiment.resolve()
    manifest_path = experiment / "manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    imu_path = experiment / "gazebo_imu.csv"
    imu_count = 0
    if imu_path.is_file():
        with imu_path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
            imu_count = len(rows)
    peak_acceleration_g = {}
    if imu_count:
        peak_acceleration_g = {
            axis: max(abs(float(row[f"{axis}_g"])) for row in rows)
            for axis in "xyz"
        }
    attachment_metrics_path = experiment / "attachment_metrics.json"
    attachment_metrics = json.loads(attachment_metrics_path.read_text(encoding="utf-8")) if attachment_metrics_path.is_file() else {}
    power_analysis = {}
    power_summary_path = experiment / "power" / "power_summary.json"
    if power_summary_path.is_file():
        try:
            source_power = json.loads(power_summary_path.read_text(encoding="utf-8"))
            power_analysis = {
                "status": source_power.get("status"),
                "provenance": source_power.get("result_class"),
                "modeled_window_s": source_power.get("modeled_window_s"),
                "modeled_charge_uah": source_power.get("modeled_charge_uah"),
                "charge_status": source_power.get("charge_status"),
                "electrical_metrics_status": source_power.get("electrical_metrics_status"),
            }
        except (OSError, json.JSONDecodeError, TypeError):
            power_analysis = {"status": "INVALID_POWER_SUMMARY"}
    motion_metrics = manifest.get("animal_motion_metrics", {})
    recording_path = experiment / "recording.jsonl"
    compressed_recording_path = experiment / "recording.jsonl.gz"
    recording_manifest_path = experiment / "recording_manifest.json"
    existing_recording = recording_path if recording_path.is_file() else compressed_recording_path
    recording_ok = (existing_recording.is_file() and existing_recording.stat().st_size > 0
                    and recording_manifest_path.is_file()
                    and _recording_is_valid(recording_manifest_path, existing_recording,
                                            imu_count))
    attachment_ok = _attachment_is_valid(manifest, attachment_metrics)
    anchor_ok, anchor_count = _anchor_events_are_valid(experiment, manifest)
    power_ok = _power_integration_is_valid(experiment)
    movement_ok, movement_evidence = _movement_is_observed(existing_recording, motion_metrics)
    live = manifest.get("firmware_live_evidence")
    standing_quiet = (manifest.get("scenario") in {"standing", "01_standing"}
                      and isinstance(live, dict)
                      and _firmware_wake_tx_is_valid(experiment, manifest))
    renode_ok = False
    live = manifest.get("firmware_live_evidence")
    if isinstance(live, dict):
        renode_ok = (
            manifest.get("clock_sync", {}).get("live_lockstep") is True
            and live.get("schema_version") == "riose.mvp3.live_firmware_evidence/v1"
            and live.get("status") == "SIMULATED_APPROXIMATE_HIGH_PASS_WU"
            and _finite_number(live.get("sample_injection_count"))
            and live["sample_injection_count"] == manifest.get("firmware_clock_sync", {}).get("sample_injection_count")
            and _finite_number(live.get("radio_tx_count"))
            and live["radio_tx_count"] > 0
            and live.get("radio_tx_done_count") == live.get("radio_tx_count")
        )
    replay_path = experiment / "firmware_replay.json"
    if replay_path.is_file():
        try:
            renode_ok = json.loads(replay_path.read_text(encoding="utf-8")).get("result") == "PASS"
        except (OSError, json.JSONDecodeError, TypeError):
            renode_ok = False
    checks = {
        "gazebo_operational": (manifest.get("gazebo_exit_code") == 0, "Gazebo process exited successfully"),
        "animal_model_operational": (manifest.get("animal_model_loaded") is True, "Gazebo model-load evidence"),
        "ear_attachment_operational": (attachment_ok,
                                       f"max relative pivot drift {attachment_metrics.get('relative_pivot_position_drift_max_m')} m"),
        "physical_movement_generated": (movement_ok, movement_evidence),
        "imu_integration_operational": (imu_count > 0, f"{imu_count} Gazebo IMU samples"),
        "renode_bridge_operational": (renode_ok,
            ("Live Gazebo IMU samples reached the Renode LIS2DW12; Zephyr and SX1262 ran in the same lockstep session"
             if isinstance(live, dict) else
             "Renode reads Gazebo samples, validates boot TX, and replays a movement-derived wake/TX through the configured WU route")),
        "firmware_wake_tx_operational": (_firmware_wake_tx_is_valid(experiment, manifest),
            ("standing remained quiet: no WU or post-boot TX; boot TX completed, final SLEEP, IRQ clear"
             if standing_quiet else
             "movement-derived IRQ, firmware wake and follow-up TX evidence")),
        "rf_anchor_operational": (anchor_ok,
                                   "logical radio receiver evidence"),
        "power_integration_operational": (power_ok, "validated SIMULATED power schedule and native analysis output"),
        "recording_operational": (recording_ok and imu_count > 0,
                                   "validated IMU/pose sample count, monotonic simulation timestamps, finite sensor values, and nearest-pose time alignment"),
        "scenario_completed": (manifest.get("scenario_complete") is True
                                and manifest.get("gazebo_exit_code") == 0
                                and imu_count > 0 and recording_ok,
                                "successful Gazebo exit with IMU and validated recording artifacts"),
    }
    statuses = {name: {"status": "PASS" if passed else "NOT_VALIDATED", "evidence": evidence}
                for name, (passed, evidence) in checks.items()}
    passed_count = sum(item["status"] == "PASS" for item in statuses.values())
    gate = "MVP3_DIGITAL_INTEGRATION_PASSED" if passed_count == len(GATE_REQUIREMENTS) else (
        "PARTIAL" if passed_count else "NOT_READY")
    result = {
        "schema_version": "riose.mvp3.report/v1", "status": "SIMULATED",
        "experiment": str(experiment), "scenario": manifest.get("scenario"),
        "provenance": "SIMULATED", "physical_validation": "NOT_PERFORMED",
        "gate": gate, "checks": statuses, "checks_passed": passed_count,
        "checks_required": len(GATE_REQUIREMENTS), "imu_sample_count": imu_count,
        "peak_abs_acceleration_g_by_axis": peak_acceleration_g,
        "ear_attachment_metrics": attachment_metrics,
        "logical_anchor_accept_count": anchor_count,
        "animal_motion_metrics": motion_metrics,
        "power_analysis": power_analysis,
        "physical_rf_result": "ANTENNA_MODEL_UNVALIDATED",
        "rssi_dbm": None,
    }
    (experiment / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [f"# MVP3 experiment report: {manifest.get('scenario', experiment.name)}", "",
             f"- Gate: `{gate}` ({passed_count}/{len(GATE_REQUIREMENTS)} checks)",
             "- Evidence: `SIMULATED`; no physical measurements or animal data.",
             "- Physical RF result: `ANTENNA_MODEL_UNVALIDATED`; RSSI is not estimated.", "", "## Gate checks", "",
             "| Requirement | Status | Evidence |", "|---|---|---|"]
    lines.extend(f"| {key} | {item['status']} | {item['evidence']} |" for key, item in statuses.items())
    lines.extend(["", "## Recorded acceleration", "", f"Gazebo IMU samples: {imu_count}.",
                  "Firmware receives acceleration samples only; scenario labels remain on the orchestration side.", ""])
    if isinstance(motion_metrics, dict) and "measured_root_displacement_m" in motion_metrics:
        lines.extend(["## Animal root displacement", "",
                      f"Recorded cow root displacement: {motion_metrics['measured_root_displacement_m']:.3f} m; "
                      f"requested forward displacement: {motion_metrics['expected_forward_displacement_m']:.3f} m.", ""])
    if power_analysis:
        lines.extend(["## Power analysis", "",
                      f"Modeled window: {power_analysis.get('modeled_window_s')} s; "
                      f"simulated charge: {power_analysis.get('modeled_charge_uah')} µAh; "
                      f"charge status: `{power_analysis.get('charge_status')}`; "
                      f"ngspice: `{power_analysis.get('electrical_metrics_status')}`.",
                      "Load currents retain their ASSUMED/DATASHEET provenance; these are not battery measurements.", ""])
    (experiment / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return result
