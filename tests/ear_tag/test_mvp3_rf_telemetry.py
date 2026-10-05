from __future__ import annotations

import binascii
import json

import pytest

from riose.products.ear_tag.mvp3.rf import (
    ANCHOR_LOGICAL_RF_EVENT_ACCEPTED,
    ANTENNA_MODEL_UNVALIDATED,
    LOGICAL_RF_EVENT,
    AnchorReceiver,
    FirmwareTraceError,
    logical_events_from_firmware_trace,
    read_firmware_trace,
)


TAG_ID = 0x54524143


def _packet(*, sequence: int = 7, timestamp_ms: int = 1) -> str:
    packet = bytearray(24)
    packet[0] = 1
    packet[1] = 1  # NORMAL behavior encoded by firmware
    packet[2:6] = TAG_ID.to_bytes(4, "little")
    packet[6:10] = sequence.to_bytes(4, "little")
    packet[10:14] = timestamp_ms.to_bytes(4, "little")
    packet[22:24] = binascii.crc_hqx(packet[:22], 0xFFFF).to_bytes(2, "little")
    return packet.hex()


def _trace(*, include_completion: bool = True) -> list[dict[str, object]]:
    records: list[dict[str, object]] = [
        {
            "schema_version": "riose.firmware.trace/v1", "sequence": 0,
            "status": "SIMULATED", "timestamp_us": 0, "event": "BOOT",
            "source": "FIRMWARE", "value0": TAG_ID, "value1": 915_000_000,
            "value2": 0, "result": 0, "packet_hex": "",
        },
        {
            "schema_version": "riose.firmware.trace/v1", "sequence": 1,
            "status": "SIMULATED", "timestamp_us": 1_000, "event": "PACKET_CREATED",
            "source": "FIRMWARE", "value0": 24, "value1": 7, "value2": 1,
            "result": 0, "packet_hex": _packet(),
        },
        {
            "schema_version": "riose.firmware.trace/v1", "sequence": 2,
            "status": "SIMULATED", "timestamp_us": 1_000, "event": "TX_START",
            "source": "SX1262", "value0": 24, "value1": 10,
            "value2": 915_000_000, "result": 0, "packet_hex": "",
        },
    ]
    if include_completion:
        records.append({
            "schema_version": "riose.firmware.trace/v1", "sequence": 3,
            "status": "SIMULATED", "timestamp_us": 31_000, "event": "TX_DONE",
            "source": "SX1262", "value0": 1, "value1": 24,
            "value2": 0, "result": 0, "packet_hex": "",
        })
    return records


def test_only_completed_trace_tx_becomes_logical_rf_event() -> None:
    event, = logical_events_from_firmware_trace(
        _trace(), poses_by_sequence={7: {"x_m": 1.25, "y_m": -0.5, "z_m": 0.2}}
    )

    assert event.event_type == LOGICAL_RF_EVENT
    assert event.timestamp_us == 1_000
    assert event.completion_timestamp_us == 31_000
    assert event.tag_id == TAG_ID
    assert event.pose == {"x_m": 1.25, "y_m": -0.5, "z_m": 0.2}
    assert event.frequency_hz == 915_000_000
    assert event.tx_power_dbm == 10
    assert event.payload_hex == _packet()
    assert event.sequence == 7
    assert event.provenance == "FIRMWARE_TRACE"


def test_no_tx_done_means_no_event_and_no_invented_packet() -> None:
    assert logical_events_from_firmware_trace(_trace(include_completion=False)) == []


def test_packet_trace_requires_actual_payload_bytes_and_valid_crc() -> None:
    trace = _trace()
    trace[1]["packet_hex"] = ""
    with pytest.raises(FirmwareTraceError, match="exact 24-byte packet_hex"):
        logical_events_from_firmware_trace(trace)

    trace = _trace()
    trace[1]["packet_hex"] = _packet()[:-4] + "0000"
    with pytest.raises(FirmwareTraceError, match="CRC is invalid"):
        logical_events_from_firmware_trace(trace)


def test_failed_tx_start_cannot_be_promoted_to_a_logical_event() -> None:
    trace = _trace()
    trace[2]["result"] = -1
    with pytest.raises(FirmwareTraceError, match="successful SX1262 event"):
        logical_events_from_firmware_trace(trace)


def test_tx_power_decodes_the_firmware_signed_uint32_field() -> None:
    trace = _trace()
    trace[2]["value1"] = 0xFFFF_FFFF  # C trace representation of -1 dBm.
    event, = logical_events_from_firmware_trace(trace)
    assert event.tx_power_dbm == -1


def test_anchor_logs_logical_acceptance_without_physical_rssi() -> None:
    event, = logical_events_from_firmware_trace(_trace())
    anchor = AnchorReceiver("anchor-east")

    accepted = anchor.accept_logical_event(event)
    assert accepted is not None
    assert accepted.event_type == ANCHOR_LOGICAL_RF_EVENT_ACCEPTED
    assert accepted.provenance == LOGICAL_RF_EVENT
    assert accepted.accepted is True
    assert accepted.anchor_id == "anchor-east"
    assert accepted.rssi_dbm is None
    assert accepted.physical_rf_result.result_type == "PHYSICAL_RF_RESULT"
    assert accepted.physical_rf_result.status == ANTENNA_MODEL_UNVALIDATED
    assert accepted.physical_rf_result.antenna_gain_dbi is None
    assert len(anchor.log) == 1

    # Replays are de-duplicated by the firmware identity and sequence.
    assert anchor.accept_logical_event(event) is None
    assert len(anchor.log) == 1


def test_firmware_trace_reader_loads_jsonl_without_rewriting_it(tmp_path) -> None:
    path = tmp_path / "firmware.jsonl"
    original = _trace()
    path.write_text("\n".join(json.dumps(row) for row in original) + "\n", encoding="utf-8")

    assert read_firmware_trace(path) == original
    assert logical_events_from_firmware_trace(read_firmware_trace(path))[0].pose is None
