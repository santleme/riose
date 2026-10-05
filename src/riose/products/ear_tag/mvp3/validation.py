"""Run MVP3 schema/unit checks and an optional live Gazebo headless scenario."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .cli import run_experiment

REPO_ROOT = Path(__file__).resolve().parents[5]


def run_validation(*, live: bool = True) -> dict[str, Any]:
    """Validate local contracts; optionally launch Gazebo and inspect its output."""
    tests = [
        "tests/ear_tag/test_mvp3_core.py",
        "tests/ear_tag/test_mvp3_validation.py",
        "tests/ear_tag/test_mvp3_rf_telemetry.py",
        "tests/ear_tag/test_mvp3_rf_propagation.py",
        "tests/ear_tag/test_mvp3_power.py",
        "tests/ear_tag/test_mvp3_reporting.py",
        "tests/ear_tag/test_mvp3_trace_integration.py",
        "tests/ear_tag/test_mvp3_kinematics.py",
        "tests/ear_tag/test_mvp3_bridge_replay.py",
        "tests/ear_tag/test_mvp3_renode_trace.py",
    ]
    command = [sys.executable, "-m", "pytest", "-q", *tests]
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    result: dict[str, Any] = {
        "schema_version": "riose.mvp3.validation/v1",
        "status": "SIMULATED", "unit_tests": {
            "status": "PASS" if completed.returncode == 0 else "FAIL",
            "command": command, "exit_code": completed.returncode,
            "output": completed.stdout[-12000:],
        },
        "live_gazebo": {"status": "NOT_RUN"},
        "firmware_wakeup_from_movement": "NOT_VALIDATED",
        "physical_rf": "ANTENNA_MODEL_UNVALIDATED",
    }
    if completed.returncode:
        raise RuntimeError(f"MVP3 targeted tests failed (exit {completed.returncode}):\n{completed.stdout[-6000:]}")
    if live:
        output = Path(tempfile.mkdtemp(prefix="riose-mvp3-validation-")) / "standing"
        experiment = run_experiment("standing", visual=False, output=output)
        manifest = json.loads((experiment / "manifest.json").read_text(encoding="utf-8"))
        report = json.loads((experiment / "summary.json").read_text(encoding="utf-8")) if (experiment / "summary.json").is_file() else None
        live_checks = {
            "gazebo_exit_code": manifest.get("gazebo_exit_code") == 0,
            "imu_samples": (experiment / "gazebo_imu.csv").is_file() and (experiment / "gazebo_imu.resd").is_file(),
            "attachment": manifest.get("ear_attachment_validated") is True,
            "recording": (experiment / "recording.jsonl").is_file(),
            "server_log_has_no_error": "[Err]" not in (experiment / "server.log").read_text(encoding="utf-8"),
        }
        result["live_gazebo"] = {
            "status": "PASS" if all(live_checks.values()) else "FAIL",
            "experiment": str(experiment), "checks": live_checks,
            "imu_samples": json.loads((experiment / "recording_manifest.json").read_text())["sample_count"],
            "attachment_metrics": json.loads((experiment / "attachment_metrics.json").read_text()),
            "server_log": str(experiment / "server.log"),
        }
        if not all(live_checks.values()):
            raise RuntimeError(f"MVP3 live Gazebo validation failed: {live_checks}")
    result["status"] = "PASS" if live and completed.returncode == 0 else "PARTIAL"
    return result
