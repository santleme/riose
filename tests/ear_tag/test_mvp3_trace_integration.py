from __future__ import annotations

import binascii
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from riose.products.ear_tag.mvp3.cli import _ingest_firmware_trace
from riose.products.ear_tag.mvp3.reporting import report_experiment


TAG_ID = 0x54524143


def _packet() -> str:
    packet = bytearray(24)
    packet[0] = 1
    packet[1] = 1
    packet[2:6] = TAG_ID.to_bytes(4, "little")
    packet[6:10] = (7).to_bytes(4, "little")
    packet[10:14] = (1).to_bytes(4, "little")
    packet[14:16] = (100).to_bytes(2, "little", signed=True)
    packet[22:24] = binascii.crc_hqx(packet[:22], 0xFFFF).to_bytes(2, "little")
    return packet.hex()


def _firmware_trace() -> list[dict]:
    return [
        {"schema_version": "riose.firmware.trace/v1", "sequence": 0,
         "status": "SIMULATED", "timestamp_us": 0, "event": "BOOT",
         "source": "FIRMWARE", "value0": TAG_ID, "value1": 915_000_000,
         "value2": 0, "result": 0},
        {"schema_version": "riose.firmware.trace/v1", "sequence": 1,
         "status": "SIMULATED", "timestamp_us": 1_000, "event": "PACKET_CREATED",
         "source": "FIRMWARE", "value0": 24, "value1": 7, "value2": 1,
         "result": 0, "packet_hex": _packet()},
        {"schema_version": "riose.firmware.trace/v1", "sequence": 2,
         "status": "SIMULATED", "timestamp_us": 1_000, "event": "TX_START",
         "source": "SX1262", "value0": 24, "value1": 10,
         "value2": 915_000_000, "result": 0},
        {"schema_version": "riose.firmware.trace/v1", "sequence": 3,
         "status": "SIMULATED", "timestamp_us": 31_000, "event": "TX_DONE",
         "source": "SX1262", "value0": 1, "value1": 24,
         "value2": 0, "result": 0},
    ]


def _power_stub(trace_path: Path, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    schedule = output_dir / "schedule.jsonl"
    schedule.write_text('{"status":"SIMULATED","current_status":"ASSUMED","event":"RF_TX"}\n')
    native = output_dir / "mvp2/summary.json"
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_text('{"status":"SIMULATED"}')
    (output_dir / "power_summary.json").write_text(json.dumps({
        "schema_version": "riose.mvp3.power_summary/v1",
        "status": "COMPLETED", "result_class": "SIMULATED",
        "trace": {"status": "SIMULATED"},
        "assumptions": {"load_profile_status": "ASSUMED"},
        "outputs": {"schedule": str(schedule)},
        "native_summary": str(native),
    }))
    return {"status": "COMPLETED"}


def test_simulated_firmware_trace_integrates_power_and_logical_anchor_evidence(tmp_path: Path) -> None:
    trace_path = tmp_path / "firmware.jsonl"
    trace_path.write_text("".join(json.dumps(row) + "\n" for row in _firmware_trace()))
    experiment = tmp_path / "experiment"
    experiment.mkdir()

    with patch("riose.products.ear_tag.mvp3.power.run_power_analysis", side_effect=_power_stub):
        integration = _ingest_firmware_trace(trace_path, experiment)

    event, = [json.loads(line) for line in (experiment / "rf_events.jsonl").read_text().splitlines()]
    anchor, = [json.loads(line) for line in (experiment / "anchor_events.jsonl").read_text().splitlines()]
    assert integration["logical_rf_event_count"] == 1
    assert integration["anchor_logical_accept_count"] == 1
    assert event["event_type"] == "LOGICAL_RF_EVENT"
    assert event["provenance"] == "FIRMWARE_TRACE"
    assert anchor["event_type"] == "ANCHOR_LOGICAL_RF_EVENT_ACCEPTED"
    assert anchor["accepted"] is True
    assert anchor["physical_rf_result"]["status"] == "ANTENNA_MODEL_UNVALIDATED"
    assert anchor["rssi_dbm"] is None
    assert integration["source_scope"].startswith("caller-provided SIMULATED")

    manifest = {"scenario": "radio-event", "anchor_receive_count": 1}
    (experiment / "manifest.json").write_text(json.dumps(manifest))
    report = report_experiment(experiment)
    assert report["checks"]["rf_anchor_operational"]["status"] == "PASS"
    assert report["checks"]["power_integration_operational"]["status"] == "PASS"
    assert report["physical_rf_result"] == "ANTENNA_MODEL_UNVALIDATED"
    assert report["rssi_dbm"] is None


def test_trace_without_payload_or_completion_fails_integration_closed(tmp_path: Path) -> None:
    rows = _firmware_trace()
    rows[1].pop("packet_hex")
    trace_path = tmp_path / "invalid.jsonl"
    trace_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    experiment = tmp_path / "experiment"
    experiment.mkdir()

    with patch("riose.products.ear_tag.mvp3.power.run_power_analysis", side_effect=_power_stub):
        with pytest.raises(RuntimeError, match="firmware trace integration failed"):
            _ingest_firmware_trace(trace_path, experiment)
    assert not (experiment / "anchor_events.jsonl").exists()

    rows = _firmware_trace()[:-1]
    trace_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    incomplete_experiment = tmp_path / "without-completion"
    incomplete_experiment.mkdir()
    with patch("riose.products.ear_tag.mvp3.power.run_power_analysis", side_effect=_power_stub):
        integration = _ingest_firmware_trace(trace_path, incomplete_experiment)
    assert integration["logical_rf_event_count"] == 0
    assert integration["anchor_logical_accept_count"] == 0
