from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from riose.products.ear_tag.mvp3.power import PowerAnalysisError, run_power_analysis


def _trace(path: Path) -> Path:
    records = [
        {"schema_version": "riose.firmware.trace/v1", "sequence": 0,
         "status": "SIMULATED", "timestamp_us": 0, "event": "BOOT", "state": "SLEEP"},
        {"schema_version": "riose.firmware.trace/v1", "sequence": 1,
         "status": "SIMULATED", "timestamp_us": 0, "event": "STATE", "state": "SLEEP"},
        {"schema_version": "riose.firmware.trace/v1", "sequence": 2,
         "status": "SIMULATED", "timestamp_us": 1_000_000, "event": "STATE", "state": "RF_TX"},
        {"schema_version": "riose.firmware.trace/v1", "sequence": 3,
         "status": "SIMULATED", "timestamp_us": 1_000_000, "event": "TX_START", "state": "RF_TX"},
        {"schema_version": "riose.firmware.trace/v1", "sequence": 4,
         "status": "SIMULATED", "timestamp_us": 1_030_000, "event": "TX_DONE", "state": "RF_TX"},
        {"schema_version": "riose.firmware.trace/v1", "sequence": 5,
         "status": "SIMULATED", "timestamp_us": 1_040_000, "event": "STATE", "state": "SLEEP"},
        {"schema_version": "riose.firmware.trace/v1", "sequence": 6,
         "status": "SIMULATED", "timestamp_us": 1_040_000, "event": "MCU_SLEEP",
         "state": "SLEEP", "value0": 1000},
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_mvp3_adapter_reuses_assumed_mvp2_loads_and_preserves_simulation_labels(tmp_path: Path) -> None:
    trace = _trace(tmp_path / "firmware.jsonl")
    output = tmp_path / "power"
    real_run = subprocess.run

    def fake_mvp2(command: list[str], **kwargs):
        native_dir = Path(command[command.index("--output") + 1])
        native_dir.mkdir(parents=True, exist_ok=True)
        (native_dir / "summary.json").write_text(json.dumps({
            "total_charge_mah_window": 0.002,
            "modeled_window_s": 2.04,
            "energy_status": "SIMULATED_AT_ASSUMED_REGULATOR_OUTPUT_VOLTAGE",
            "energy_by_event_mj": {"TX": 0.004},
            "trace_provenance": {"status": "SIMULATED", "sha256": "trace-hash"},
            "ngspice": {"status": "TOOL_UNAVAILABLE"},
        }))
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def call(command, **kwargs):
        if any(str(part).endswith("mvp2_power.py") for part in command):
            return fake_mvp2(command, **kwargs)
        return real_run(command, **kwargs)

    with patch("riose.products.ear_tag.mvp3.power.analysis.subprocess.run", side_effect=call):
        summary = run_power_analysis(trace, output, trace_duration_s=5.0)

    assert summary["status"] == "COMPLETED"
    assert summary["result_class"] == "SIMULATED"
    assert summary["assumptions"]["load_profile_status"] == "ASSUMED"
    assert summary["assumptions"]["current_measurements_available"] is False
    assert summary["modeled_charge_uah"] == pytest.approx(2.0)
    assert summary["electrical_metrics_status"] == "TOOL_UNAVAILABLE"
    assert (output / "power_summary.json").is_file()
    assert (output / "schedule.jsonl").is_file()
    schedule = [json.loads(line) for line in (output / "schedule.jsonl").read_text().splitlines()]
    assert any(row["event"] == "TX" and row["current_status"] == "ASSUMED" for row in schedule)
    assert all(row["trace_status"] == "SIMULATED" for row in schedule)
    assert {row["trace_window_end_s"] for row in schedule} == {5.0}


def test_bad_firmware_trace_fails_closed_without_success_summary(tmp_path: Path) -> None:
    trace = tmp_path / "bad.jsonl"
    trace.write_text('{"status":"MEASURED","timestamp_us":0,"event":"TX_START","state":"RF_TX"}\n')
    output = tmp_path / "power"

    with pytest.raises(PowerAnalysisError, match="trace adapter failed"):
        run_power_analysis(trace, output)

    assert not (output / "power_summary.json").exists()
