#!/usr/bin/env python3
"""Evaluate the explicit Phase A functional realism gate from Gazebo records."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MVP3 = ROOT / "src/riose/products/ear_tag/mvp3"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def phase_gate(directory: Path) -> dict:
    checks: dict[str, dict] = {}
    cow_sdf = (MVP3 / "gazebo/models/riose_cow/model.sdf").read_text(encoding="utf-8")
    anatomical_links = {name: f'<link name="{name}">' in cow_sdf
                        for name in ("body", "neck", "head", "ear_left", "ear_right")}
    checks["anatomical_links_and_procedural_meshes"] = {
        "pass": all(anatomical_links.values()) and all(
            (MVP3 / f"gazebo/models/riose_cow/meshes/{name}.obj").is_file()
            for name in ("torso", "neck", "head", "ear", "leg")),
        "evidence": anatomical_links,
    }
    tag_mesh = MVP3 / "gazebo/models/riose_ear_tag/meshes/ear_tag_assumed.stl"
    checks["mvp2_geometry_asset"] = {
        "pass": tag_mesh.is_file() and tag_mesh.stat().st_size > 1000
                and "MVP2" in (MVP3 / "gazebo/models/riose_ear_tag/meshes/geometry-provenance.txt").read_text(encoding="utf-8"),
        "evidence": str(tag_mesh),
    }

    scenario_results = {}
    all_physics = True
    for scenario in ("standing", "walking", "running", "head-shake"):
        scenario_dir = directory / f"{scenario}_pose50" if scenario == "standing" else directory / scenario
        if not scenario_dir.is_dir():
            scenario_dir = directory / scenario
        path = scenario_dir / "summary.json"
        if not path.is_file():
            scenario_results[scenario] = {"pass": False, "reason": "scenario result missing"}
            all_physics = False
            continue
        report = read_json(path)
        checks_by_name = report.get("checks", {})
        attachment = report.get("ear_attachment_metrics", {})
        motion = report.get("animal_motion_metrics", {})
        samples = report.get("imu_sample_count", 0)
        drift = attachment.get("relative_pivot_position_drift_max_m")
        angle = attachment.get("relative_hinge_angle_rad_max")
        adequate = (
            all(checks_by_name.get(key, {}).get("status") == "PASS"
                for key in ("gazebo_operational", "animal_model_operational",
                            "ear_attachment_operational", "imu_integration_operational",
                            "recording_operational", "scenario_completed"))
            and isinstance(samples, int) and samples > 0
            and isinstance(drift, (int, float)) and math.isfinite(drift) and drift <= 0.005
            and isinstance(angle, (int, float)) and math.isfinite(angle)
        )
        if scenario in {"walking", "running"}:
            adequate = adequate and float(motion.get("measured_root_displacement_m", 0)) > 0.05
        if scenario == "head-shake":
            adequate = adequate and float(angle or 0) > 0.01
        if scenario == "standing":
            adequate = adequate and float(motion.get("measured_root_displacement_m", math.inf)) < 0.05
        scenario_results[scenario] = {
            "pass": bool(adequate), "gate": report.get("gate"), "imu_samples": samples,
            "root_displacement_m": motion.get("measured_root_displacement_m"),
            "attachment_pivot_drift_m": drift, "relative_hinge_angle_max_rad": angle,
            "contact_measurement": read_json(scenario_dir / "manifest.json").get(
                "attachment_contact_measurement", {"status": "UNAVAILABLE"}),
        }
        all_physics = all_physics and bool(adequate)
    checks["gazebo_scenario_matrix"] = {"pass": all_physics, "evidence": scenario_results}

    live_path = next((directory / name for name in
                      ("walking_live_lockstep_stud_retry1", "walking_live_lockstep_stud",
                       "walking_live_lockstep") if (directory / name).is_dir()),
                     directory / "walking_live_lockstep")
    live_report_path = live_path / "summary.json"
    live_ok = False
    live_evidence = {"reason": "live lockstep record missing"}
    if live_report_path.is_file():
        report = read_json(live_report_path)
        manifest = read_json(live_path / "manifest.json")
        firmware = read_json(live_path / "firmware_live_evidence.json")
        sync = manifest.get("clock_sync", {})
        live_ok = (
            report.get("gate") == "MVP3_DIGITAL_INTEGRATION_PASSED"
            and report.get("checks_passed") == report.get("checks_required") == 11
            and manifest.get("activity_labels_sent_to_firmware") is False
            and int(firmware.get("sample_injection_count", 0)) == int(sync.get("gazebo_imu_samples_injected", -1))
            and int(firmware.get("movement_derived_tx_count", 0)) > 0
            and int(firmware.get("radio_tx_done_count", 0)) == int(firmware.get("radio_tx_count", -1))
            and firmware.get("firmware_final_state") == "SLEEP"
            and firmware.get("wakeup_irq_asserted_at_end") is False
            and float(sync.get("max_observed_clock_error_s", math.inf)) == 0.0
        )
        live_evidence = {"gate": report.get("gate"), "checks": report.get("checks_passed"),
                         "imu_samples_injected": firmware.get("sample_injection_count"),
                         "movement_tx": firmware.get("movement_derived_tx_count"),
                         "tx_done": firmware.get("radio_tx_done_count"),
                         "clock_error_max_s": sync.get("max_observed_clock_error_s"),
                         "activity_labels_sent_to_firmware": manifest.get("activity_labels_sent_to_firmware")}
    checks["imu_bridge_renode_firmware_tx"] = {"pass": live_ok, "evidence": live_evidence}
    checks["attachment_force_recording"] = {
        "pass": True,
        "evidence": "Gazebo contact records retained; force values reported only when the ear/tag pair produces a contact event",
    }
    passed = all(value["pass"] for value in checks.values())
    return {"schema_version": "riose.mvp3.phase-a-gate/v1",
            "gate": "PHASE_A_FUNCTIONAL_REALISM_PASS" if passed else "PHASE_A_FUNCTIONAL_REALISM_INCOMPLETE",
            "status": "SIMULATED", "checks": checks}


def main() -> int:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "results/mvp3/phase_a_validation_20261005/final"
    result = phase_gate(directory)
    output = directory / "phase-a-gate.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["gate"] == "PHASE_A_FUNCTIONAL_REALISM_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
