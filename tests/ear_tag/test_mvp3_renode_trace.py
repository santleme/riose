from __future__ import annotations

import binascii
import json

import pytest

from riose.products.ear_tag.mvp3.rf.renode_trace import (
    RenodeTraceConversionError,
    convert_sx1262_trace,
    convert_sx1262_trace_file,
)
from riose.products.ear_tag.mvp3.rf.telemetry import logical_events_from_firmware_trace
from hardware.spice.trace_adapter import trace_to_schedule


def _packet(*, sequence: int, timestamp_ms: int) -> str:
    packet = bytearray(24)
    packet[0] = 1
    packet[1] = 3
    packet[2:6] = (1).to_bytes(4, "little")
    packet[6:10] = sequence.to_bytes(4, "little")
    packet[10:14] = timestamp_ms.to_bytes(4, "little")
    packet[14:16] = (-125).to_bytes(2, "little", signed=True)
    packet[16:18] = (250).to_bytes(2, "little", signed=True)
    packet[22:24] = binascii.crc_hqx(packet[:22], 0xFFFF).to_bytes(2, "little")
    return packet.hex()


def _source_trace() -> dict[str, object]:
    return {
        "schema_version": "riose.renode.sx1262_tx_trace/v1",
        "clock": "Machine.ElapsedVirtualTime.TimeElapsed",
        "time_unit": "ns",
        "provenance": "SIMULATED",
        "records": [
            {
                "tx_index": 1,
                "tx_start_ns": 8_158_370,
                "tx_done_ns": 38_158_370,
                "payload_length": 24,
                "payload_hex": _packet(sequence=0, timestamp_ms=0),
                "payload_captured": True,
                "rf_frequency_word": 0x39300000,
                "tx_power_dbm": 10,
                "status": "completed",
            },
            {
                "tx_index": 2,
                "tx_start_ns": 250_232_850,
                "tx_done_ns": 280_232_850,
                "payload_length": 24,
                "payload_hex": _packet(sequence=1, timestamp_ms=249),
                "payload_captured": True,
                "rf_frequency_word": 0x39300000,
                "tx_power_dbm": -1,
                "status": "completed",
            },
        ],
    }


def test_converted_trace_is_accepted_by_rf_and_power_adapters() -> None:
    records = convert_sx1262_trace(_source_trace())

    events = logical_events_from_firmware_trace(records)
    assert len(events) == 2
    assert [event.frequency_hz for event in events] == [915_000_000, 915_000_000]
    assert [event.tx_power_dbm for event in events] == [10, -1]
    assert [event.timestamp_us for event in events] == [8_158, 250_232]
    assert [event.completion_timestamp_us for event in events] == [38_158, 280_232]
    assert all(event.provenance == "FIRMWARE_TRACE" for event in events)
    assert all(event.pose is None for event in events)

    schedule = trace_to_schedule(records, {
        "pair:TX_START:TX_DONE": {
            "component": "radio",
            "load_current_ma": 28.0,
            "current_status": "ASSUMED",
            "source": "test profile",
        },
    })
    assert len(schedule) == 2
    assert [row["duration_s"] for row in schedule] == pytest.approx([0.03, 0.03])
    assert all(row["status"] == "SIMULATED" for row in schedule)
    assert all(row["duration_source"] == "TRACE_TIMESTAMP" for row in schedule)


@pytest.mark.parametrize("mutation, message", [
    (lambda trace: trace.update(provenance="MEASURED"), "provenance must be SIMULATED"),
    (lambda trace: trace["records"][0].update(status="in_progress"), "is not completed"),
    (lambda trace: trace["records"][0].update(rf_frequency_word=0), "rf_frequency_word"),
    (lambda trace: trace["records"][0].update(tx_power_dbm=256), "signed 8-bit"),
    (lambda trace: trace["records"][0].update(payload_captured=False), "payload_captured"),
    (lambda trace: trace["records"][0].update(payload_hex="00"), "payload must be exact"),
])
def test_invalid_or_incomplete_source_rows_fail_closed(mutation, message: str) -> None:
    trace = _source_trace()
    mutation(trace)
    with pytest.raises(RenodeTraceConversionError, match=message):
        convert_sx1262_trace(trace)


def test_bad_crc_and_timestamp_order_are_rejected() -> None:
    trace = _source_trace()
    trace["records"][0]["payload_hex"] = _packet(sequence=0, timestamp_ms=0)[:-4] + "0000"
    with pytest.raises(RenodeTraceConversionError, match="CRC is invalid"):
        convert_sx1262_trace(trace)

    trace = _source_trace()
    trace["records"][1]["payload_hex"] = _packet(sequence=1, timestamp_ms=0)
    trace["records"][1]["tx_start_ns"] = trace["records"][0]["tx_start_ns"]
    with pytest.raises(RenodeTraceConversionError, match="not monotonic"):
        convert_sx1262_trace(trace)


def test_generated_trace_uses_explicit_unknown_state_without_pose() -> None:
    records = convert_sx1262_trace(_source_trace())
    assert {record["state"] for record in records} == {"NOT_CAPTURED"}
    assert all(record["status"] == "SIMULATED" for record in records)
    assert all("pose" not in record and "position" not in record for record in records)
    assert records[2]["value1"] == 10
    assert records[5]["value1"] == 0xFFFF_FFFF


def test_file_converter_preserves_source_and_writes_jsonl(tmp_path) -> None:
    source_path = tmp_path / "sx1262.json"
    output_path = tmp_path / "nested" / "firmware.jsonl"
    original = json.dumps(_source_trace())
    source_path.write_text(original, encoding="utf-8")

    records = convert_sx1262_trace_file(source_path, output_path)

    assert source_path.read_text(encoding="utf-8") == original
    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(records) == 7
    assert [json.loads(line) for line in lines] == records
    with pytest.raises(RenodeTraceConversionError, match="must not overwrite"):
        convert_sx1262_trace_file(source_path, source_path)
