"""Offline Gazebo-to-Renode trace bridge."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import re
import csv
from pathlib import Path
from typing import Any

from .renode_monitor import RenodeMonitorClient, RenodeMonitorError


def _clock_mapping(experiment: Path, tx_trace: Path, wake_evidence: dict[str, Any] | None) -> dict[str, Any] | None:
    """Describe the offline RESD/Renode alignment used by the WU Robot case."""
    if not wake_evidence or not tx_trace.is_file():
        return None
    try:
        trace = json.loads(tx_trace.read_text(encoding="utf-8"))
        records = trace["records"]
        movement_tx = next(record for record in records if int(record["tx_index"]) > 1)
        renode_tx_s = int(movement_tx["tx_start_ns"]) / 1_000_000_000
    except (OSError, ValueError, KeyError, TypeError, StopIteration, json.JSONDecodeError):
        return None

    mapping: dict[str, Any] = {
        "schema_version": "riose.mvp3.clock_mapping/v1",
        "status": "SIMULATED",
        "source_clock": "RENODE_VIRTUAL_TIME",
        "target_clock": "GAZEBO_SIMULATION_TIME",
        "scale": 1.0,
        "offset_s": -0.25,
        "method": "AFFINE_OFFLINE_RESD_REPLAY",
        "live_lockstep": False,
        "alignment_basis": "Robot advances Renode 0.25 s, then offsets first RESD sample to that virtual time",
    }
    csv_path = experiment / "gazebo_imu.csv"
    if not csv_path.is_file():
        mapping["alignment_check"] = {"status": "UNAVAILABLE", "reason": "gazebo_imu.csv is missing"}
        return mapping
    try:
        with csv_path.open(newline="", encoding="utf-8") as stream:
            samples = [float(row["timestamp_s"]) for row in csv.DictReader(stream)]
        if not samples:
            raise ValueError("gazebo_imu.csv has no samples")
    except (OSError, ValueError, KeyError) as exc:
        mapping["alignment_check"] = {"status": "UNAVAILABLE", "reason": str(exc)}
        return mapping
    mapped_s = round(mapping["scale"] * renode_tx_s + mapping["offset_s"], 12)
    nearest_s = min(samples, key=lambda timestamp: abs(timestamp - mapped_s))
    mapping["alignment_check"] = {
        "status": "RECOMPUTED_FROM_RECORDS",
        "renode_tx_start_s": round(renode_tx_s, 12),
        "mapped_gazebo_time_s": mapped_s,
        "nearest_imu_sample_time_s": nearest_s,
        "absolute_error_s": round(abs(nearest_s - mapped_s), 12),
    }
    return mapping


def replay_in_renode(experiment: str | Path) -> dict:
    """Replay Gazebo RESD samples through the Renode-profile firmware test.

    This confirms Gazebo-derived samples trigger the configured LIS2DW12 WU
    route, wake Zephyr from SLEEP, and produce a completed logical TX. The
    comparator uses a documented first-order high-pass approximation.
    """
    experiment = Path(experiment).resolve()
    elf = os.environ.get("RIOSE_ZEPHYR_ELF")
    renode_test = shutil.which("renode-test")
    resd = experiment / "gazebo_imu.resd"
    if not elf or not Path(elf).is_file():
        raise RuntimeError("set RIOSE_ZEPHYR_ELF to a built Renode-profile Zephyr ELF before replay")
    if not renode_test:
        raise RuntimeError("renode-test is not available on PATH")
    if not resd.is_file():
        raise RuntimeError(f"missing Gazebo RESD input: {resd}; run an MVP3 scenario first")

    repo_root = Path(__file__).resolve().parents[6]
    result_dir = experiment / "renode"
    result_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["RIOSE_LIS2DW12_GAZEBO_RESD"] = str(resd)
    configured_tx_trace = os.environ.get("RIOSE_RENODE_TX_TRACE")
    tx_trace = (Path(configured_tx_trace).expanduser().resolve()
                if configured_tx_trace else experiment / "sx1262_tx_trace.json")
    env["RIOSE_RENODE_TX_TRACE"] = str(tx_trace)
    command = [renode_test, "-r", str(result_dir),
               str(repo_root / "hardware" / "renode" / "tests" / "gazebo-bridge.robot")]
    completed = subprocess.run(command, cwd=repo_root, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=180)
    log = result_dir / "renode-test.log"
    log.write_text(completed.stdout, encoding="utf-8")
    clean_output = re.sub(r"\x1b\[[0-9;]*m", "", completed.stdout)
    required_cases = (
        "Firmware Reads Gazebo RESD Samples",
        "Firmware Startup TX Payload Is Captured",
        "LIS2DW12 Accepts Individual Gazebo Acceleration Samples",
        "Firmware Wakes From Gazebo Motion Comparator And Transmits",
    )
    case_statuses: dict[str, str] = {}
    for case in required_cases:
        match = re.search(
            rf"Finished test 'gazebo-bridge\.{re.escape(case)}'[^\r\n]*?with status (OK|FAIL|SKIP(?:PED)?)",
            clean_output,
        )
        if match:
            case_statuses[case] = match.group(1)
    passed_tests = sum(status == "OK" for status in case_statuses.values())
    skipped_tests = sum(status.startswith("SKIP") for status in case_statuses.values())
    failed_tests = len(required_cases) - passed_tests - skipped_tests
    wake_log_match = re.search(
        r"movement WU evidence: tx=(0x[0-9a-fA-F]+) completed=(0x[0-9a-fA-F]+) "
        r"wake_events=(0x[0-9a-fA-F]+) source_reads=(0x[0-9a-fA-F]+) "
        r"samples_read=(0x[0-9a-fA-F]+) behavior=(\d+) "
        r"imu_mg=\((-?\d+),(-?\d+),(-?\d+)\) WU_SRC=(\d+) "
        r"WU_IRQ=(True|False) CRC=(True|False) final_state=(SLEEP|OTHER)", clean_output)
    movement_wake_evidence = None
    if wake_log_match and case_statuses.get(required_cases[3]) == "OK":
        (tx_total, tx_done, wake_events, source_reads, sample_reads, behavior,
         x_mg, y_mg, z_mg, wake_source, wake_irq, crc_valid,
         final_state) = wake_log_match.groups()
        movement_wake_evidence = {
            "status": "SIMULATED_APPROXIMATE_HIGH_PASS_MODEL",
            "total_tx_count_including_boot_tx": int(tx_total, 0),
            "total_tx_done_count_including_boot_tx": int(tx_done, 0),
            "post_boot_sample_read_count": int(sample_reads, 0),
            "post_boot_classifier_behavior": int(behavior),
            "last_payload_axes_mg": [int(x_mg), int(y_mg), int(z_mg)],
            "wake_up_source_register": f"0x{int(wake_source):02X}",
            "wake_events_generated": int(wake_events, 0),
            "wake_events_read_by_firmware": int(source_reads, 0),
            "adapter_wakeup_irq_asserted_after_source_read": wake_irq == "True",
            "last_payload_crc_valid": crc_valid == "True",
            "post_boot_tx_count": max(0, int(tx_total, 0) - 1),
            "pre_motion_firmware_state": "SLEEP",
            "post_motion_firmware_state": final_state,
            "event_source": "Gazebo-derived acceleration through LIS2DW12 WU comparator model",
            "filter_model": "first-order high-pass approximation; not silicon transfer-function validation",
        }
    passed = (completed.returncode == 0 and passed_tests == len(required_cases)
              and failed_tests == 0 and skipped_tests == 0
              and movement_wake_evidence is not None
              and movement_wake_evidence["last_payload_crc_valid"] is True
              and movement_wake_evidence["wake_events_generated"] > 0
              and movement_wake_evidence["wake_events_read_by_firmware"] > 0
              and not movement_wake_evidence["adapter_wakeup_irq_asserted_after_source_read"]
              and int(movement_wake_evidence["wake_up_source_register"], 0) & 0x08
              and int(movement_wake_evidence["wake_up_source_register"], 0) & 0x07
              and movement_wake_evidence["post_boot_classifier_behavior"] == 3
              and movement_wake_evidence["post_motion_firmware_state"] == "SLEEP"
              and movement_wake_evidence["post_boot_tx_count"] > 0)
    automatic_trace_integration: dict[str, Any] = {
        "status": "NOT_RUN",
        "source_trace": str(tx_trace),
        "canonical_firmware_trace": str(experiment / "firmware_trace.jsonl"),
        "limitations": [
            "No pose pairing or MCU state is present in the SX1262 trace.",
            "The LIS2DW12 high-pass comparator is a first-order behavioral approximation, not validated silicon filter dynamics.",
            "No physical RF reception or RSSI is modeled by this conversion.",
        ],
    }
    if passed:
        canonical_trace = experiment / "firmware_trace.jsonl"
        if canonical_trace.exists():
            automatic_trace_integration.update({
                "status": "SKIPPED_EXISTING_FIRMWARE_TRACE",
                "reason": "preserved existing root firmware_trace.jsonl and its outputs",
            })
        elif not tx_trace.is_file():
            automatic_trace_integration.update({
                "status": "FAILED",
                "reason": "Renode Robot suite passed but did not export the configured SX1262 trace",
            })
        else:
            converted_trace = result_dir / "firmware_trace.converted.jsonl"
            try:
                from ..rf.renode_trace import convert_sx1262_trace_file
                convert_sx1262_trace_file(tx_trace, converted_trace)
                # Import lazily to avoid coupling bridge module initialization
                # to the top-level CLI and its Gazebo dependencies.
                from ..cli import _ingest_firmware_trace
                integration = _ingest_firmware_trace(converted_trace, experiment)
                integration = dict(integration)
                integration["source_scope"] = (
                    "Renode SX1262 model trace; no Gazebo time, pose, MCU state, "
                    "production wake, or physical RF correlation is established")
                manifest_path = experiment / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
                manifest.update({
                    "firmware_trace": integration,
                    "anchor_receive_count": integration["anchor_logical_accept_count"],
                    "automatic_firmware_trace_integration": "SIMULATED",
                })
                manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                         encoding="utf-8")
                automatic_trace_integration.update({
                    "status": "SIMULATED",
                    "converted_trace": str(converted_trace),
                    "integration": integration,
                })
            except (OSError, ValueError, RuntimeError) as exc:
                automatic_trace_integration.update({"status": "FAILED", "reason": str(exc)})
    clock_mapping = _clock_mapping(experiment, tx_trace, movement_wake_evidence)
    summary = {
        "schema_version": "riose.mvp3.renode-replay/v1",
        "status": "SIMULATED", "result": "PASS" if passed else "FAILED",
        "validation_scope": ("firmware reads Gazebo-derived LIS2DW12 samples, captures boot TX, "
                             "and wakes through the configured WU route using a behavioral high-pass model"),
        "boot_tx_payload_validated": case_statuses.get(required_cases[1]) == "OK",
        "boot_tx_count": 1 if case_statuses.get(required_cases[1]) == "OK" else 0,
        "movement_derived_wakeup": "VALIDATED" if movement_wake_evidence else "NOT_VALIDATED",
        "movement_derived_tx_count": movement_wake_evidence["post_boot_tx_count"] if movement_wake_evidence else 0,
        "radio_transmission_from_movement": (
            "GAZEBO_DERIVED_WU_COMPARATOR_PATH_VALIDATED" if movement_wake_evidence
            and movement_wake_evidence["last_payload_crc_valid"] else "NOT_VALIDATED"),
        "movement_wake_evidence": movement_wake_evidence,
        "clock_mapping": clock_mapping,
        "automatic_firmware_trace_integration": automatic_trace_integration,
        "elf": str(Path(elf).resolve()), "resd": str(resd),
        "command": command, "exit_code": completed.returncode,
        "tests": {"passed": passed_tests, "failed": failed_tests, "skipped": skipped_tests},
        "test_cases": case_statuses,
        "log": str(log),
    }
    if passed:
        manifest_path = experiment / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        firmware_trace = manifest.get("firmware_trace")
        if isinstance(firmware_trace, dict) and clock_mapping:
            firmware_trace["source_scope"] = (
                "Renode SX1262 model trace; Robot evidence validates the post-boot TX as a movement-derived wake/TX "
                "through the approximate WU comparator; offline affine mapping permits Gazebo-time event alignment. "
                "The source trace itself contains no pose or MCU state, and this is not live lockstep or physical RF."
            )
        manifest.update({
            "firmware_wake_tx_validated": True,
            "movement_derived_tx_count": summary["movement_derived_tx_count"],
            "firmware_wake_model": "SIMULATED_APPROXIMATE_HIGH_PASS_WU",
            "movement_wake_evidence": movement_wake_evidence,
            "clock_mapping": clock_mapping,
        })
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (experiment / "firmware_replay.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if automatic_trace_integration["status"] == "SIMULATED":
        from ..reporting import report_experiment
        report_experiment(experiment)
    if not passed:
        raise RuntimeError(f"Renode Gazebo bridge test did not pass; see {log}")
    return summary


__all__ = ["RenodeMonitorClient", "RenodeMonitorError", "replay_in_renode"]
