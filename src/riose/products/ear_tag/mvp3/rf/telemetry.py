"""Logical RF telemetry backed by completed firmware trace records.

This module does not operate a radio or model a physical link. A logical TX
event exists only when a packet-bearing SX1262 ``TX_START`` in a versioned
firmware trace is followed by a successful matching ``TX_DONE``. Antenna
evidence in MVP2 is inconclusive, so physical reception metrics remain null.
"""
from __future__ import annotations

import binascii
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


TRACE_SCHEMA = "riose.firmware.trace/v1"
LOGICAL_RF_EVENT = "LOGICAL_RF_EVENT"
PHYSICAL_RF_RESULT = "PHYSICAL_RF_RESULT"
ANCHOR_LOGICAL_RF_EVENT_ACCEPTED = "ANCHOR_LOGICAL_RF_EVENT_ACCEPTED"
ANTENNA_MODEL_UNVALIDATED = "ANTENNA_MODEL_UNVALIDATED"
TELEMETRY_SIZE = 24
TX_DONE_IRQ = 0x0001
_TRACE_EVIDENCE_TOKEN = object()


class FirmwareTraceError(ValueError):
    """The supplied trace cannot safely support logical RF events."""


@dataclass(frozen=True)
class PhysicalRfResult:
    """Separate physical-link result; currently explicitly unvalidated."""

    result_type: str = PHYSICAL_RF_RESULT
    status: str = ANTENNA_MODEL_UNVALIDATED
    rssi_dbm: None = None
    antenna_gain_dbi: None = None


@dataclass(frozen=True)
class LogicalRfEvent:
    """One firmware-traced and completed logical radio transmission."""

    event_type: str
    timestamp_us: int
    completion_timestamp_us: int
    tag_id: int
    pose: Mapping[str, float] | None
    frequency_hz: int
    tx_power_dbm: int
    payload_hex: str
    sequence: int
    provenance: str
    source_trace_sequence: int
    status: str
    physical_rf_result: PhysicalRfResult
    _trace_evidence: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._trace_evidence is not _TRACE_EVIDENCE_TOKEN:
            raise ValueError("LogicalRfEvent must be produced from a validated firmware trace")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready event while keeping physical RF separate."""
        result = asdict(self)
        result.pop("_trace_evidence", None)
        if self.pose is not None:
            result["pose"] = dict(self.pose)
        return result


@dataclass(frozen=True)
class AnchorLogEntry:
    """Anchor log row for accepting a logical event, not an RF reception."""

    event_type: str
    provenance: str
    accepted: bool
    anchor_id: str
    timestamp_us: int
    tag_id: int
    sequence: int
    frequency_hz: int
    tx_power_dbm: int
    payload_hex: str
    pose: Mapping[str, float] | None
    physical_rf_result: PhysicalRfResult
    rssi_dbm: None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.pose is not None:
            result["pose"] = dict(self.pose)
        return result


def _integer(record: Mapping[str, Any], field: str, index: int, *, minimum: int = 0) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FirmwareTraceError(f"record {index}: {field} must be an integer >= {minimum}")
    return value


def _validate_pose(pose: Mapping[str, float] | None, sequence: int) -> Mapping[str, float] | None:
    if pose is None:
        return None
    if not isinstance(pose, Mapping) or not pose:
        raise ValueError(f"pose for packet sequence {sequence} must be a nonempty mapping")
    copied: dict[str, float] = {}
    for key, value in pose.items():
        if not isinstance(key, str) or not key:
            raise ValueError("pose keys must be nonempty strings")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"pose coordinate {key!r} must be finite")
        copied[key] = float(value)
    return copied


def _packet_metadata(record: Mapping[str, Any], tag_id: int, index: int) -> tuple[str, int]:
    packet_hex = record.get("packet_hex")
    length = _integer(record, "value0", index)
    packet_sequence = _integer(record, "value1", index)
    if not isinstance(packet_hex, str) or length != TELEMETRY_SIZE or len(packet_hex) != TELEMETRY_SIZE * 2:
        raise FirmwareTraceError(f"record {index}: PACKET_CREATED must contain exact 24-byte packet_hex")
    if packet_hex != packet_hex.lower() or any(char not in "0123456789abcdef" for char in packet_hex):
        raise FirmwareTraceError(f"record {index}: packet_hex must be lowercase hexadecimal")
    packet = bytes.fromhex(packet_hex)
    if packet[0] != 1:
        raise FirmwareTraceError(f"record {index}: unsupported telemetry packet version")
    behavior = _integer(record, "value2", index)
    if behavior != packet[1] or behavior > 3:
        raise FirmwareTraceError(f"record {index}: telemetry behavior does not match packet metadata")
    if int.from_bytes(packet[2:6], "little") != tag_id:
        raise FirmwareTraceError(f"record {index}: telemetry tag id does not match BOOT")
    if int.from_bytes(packet[6:10], "little") != packet_sequence:
        raise FirmwareTraceError(f"record {index}: telemetry sequence does not match packet metadata")
    packet_timestamp_ms = int.from_bytes(packet[10:14], "little")
    trace_timestamp_ms = _integer(record, "timestamp_us", index) // 1000
    if packet_timestamp_ms > trace_timestamp_ms or trace_timestamp_ms - packet_timestamp_ms > 1:
        raise FirmwareTraceError(f"record {index}: telemetry timestamp is inconsistent with firmware trace")
    if binascii.crc_hqx(packet[:22], 0xFFFF) != int.from_bytes(packet[22:24], "little"):
        raise FirmwareTraceError(f"record {index}: telemetry CRC is invalid")
    return packet_hex, packet_sequence


def read_firmware_trace(path: str | Path) -> list[dict[str, Any]]:
    """Read JSONL records without synthesizing or normalizing trace data."""
    trace_path = Path(path)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(trace_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FirmwareTraceError(f"invalid JSON in firmware trace line {line_number}") from exc
        if not isinstance(record, dict):
            raise FirmwareTraceError(f"firmware trace line {line_number} must be a JSON object")
        records.append(record)
    if not records:
        raise FirmwareTraceError("firmware trace is empty")
    return records


def logical_events_from_firmware_trace(
    records: Sequence[Mapping[str, Any]],
    *,
    poses_by_sequence: Mapping[int, Mapping[str, float]] | None = None,
) -> list[LogicalRfEvent]:
    """Convert only completed packet TX intervals into logical RF events.

    The input is expected to be output from an actual firmware execution or
    test trace. Incomplete, timed-out, failed, or payload-free TX attempts do
    not create events. This function never infers an RSSI or an antenna gain.
    """
    if not records:
        return []
    previous_timestamp = -1
    boot_id: int | None = None
    pending_packet: tuple[Mapping[str, Any], str, int] | None = None
    pending_tx: tuple[Mapping[str, Any], Mapping[str, Any], str, int, int] | None = None
    events: list[LogicalRfEvent] = []

    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise FirmwareTraceError(f"record {index}: expected mapping")
        if record.get("schema_version") != TRACE_SCHEMA or record.get("status") != "SIMULATED":
            raise FirmwareTraceError(f"record {index}: unsupported schema or non-SIMULATED provenance")
        sequence = _integer(record, "sequence", index)
        if sequence != index:
            raise FirmwareTraceError(f"record {index}: sequence must be contiguous from zero")
        timestamp = _integer(record, "timestamp_us", index)
        if timestamp < previous_timestamp:
            raise FirmwareTraceError(f"record {index}: timestamps must be monotonic")
        previous_timestamp = timestamp
        event_name = record.get("event")

        if index == 0:
            if event_name != "BOOT":
                raise FirmwareTraceError("firmware trace must start with BOOT")
            boot_id = _integer(record, "value0", index)
        elif event_name == "BOOT":
            raise FirmwareTraceError(f"record {index}: duplicate BOOT")

        if event_name == "PACKET_CREATED":
            if boot_id is None or pending_packet is not None or pending_tx is not None:
                raise FirmwareTraceError(f"record {index}: packet overlaps or precedes a valid cycle")
            payload_hex, packet_sequence = _packet_metadata(record, boot_id, index)
            pending_packet = (record, payload_hex, packet_sequence)
        elif event_name == "TX_START":
            if pending_packet is None or pending_tx is not None:
                raise FirmwareTraceError(f"record {index}: TX_START has no unique packet evidence")
            if (record.get("source") != "SX1262"
                    or isinstance(record.get("result"), bool)
                    or not isinstance(record.get("result"), int)
                    or record.get("result") != 0):
                raise FirmwareTraceError(f"record {index}: TX_START must be a successful SX1262 event")
            length = _integer(record, "value0", index)
            power_raw = _integer(record, "value1", index)
            # The C trace stores signed config values in an unsigned uint32_t
            # slot, so decode the two's-complement representation explicitly.
            power = power_raw - (1 << 32) if power_raw >= (1 << 31) else power_raw
            frequency = _integer(record, "value2", index, minimum=1)
            if not -128 <= power <= 127:
                raise FirmwareTraceError(f"record {index}: TX power must be a signed 8-bit integer")
            packet_record, payload_hex, packet_sequence = pending_packet
            if length != len(payload_hex) // 2 or packet_record.get("value0") != length:
                raise FirmwareTraceError(f"record {index}: TX_START length does not match packet evidence")
            pending_tx = (record, packet_record, payload_hex, packet_sequence, power)
            pending_packet = None
        elif event_name == "TX_DONE":
            if pending_tx is None:
                raise FirmwareTraceError(f"record {index}: TX_DONE has no matching TX_START")
            start_record, packet_record, payload_hex, packet_sequence, tx_power_dbm = pending_tx
            irq_mask = record.get("value0")
            length = record.get("value1")
            result = record.get("result")
            if (record.get("source") != "SX1262"
                    or isinstance(result, bool) or not isinstance(result, int) or result != 0
                    or isinstance(irq_mask, bool) or not isinstance(irq_mask, int) or irq_mask != TX_DONE_IRQ
                    or isinstance(length, bool) or not isinstance(length, int) or length != len(payload_hex) // 2
                    or timestamp < start_record["timestamp_us"]):
                raise FirmwareTraceError(f"record {index}: TX_DONE is not a successful matching completion")
            pose = None
            if poses_by_sequence is not None and packet_sequence in poses_by_sequence:
                pose = _validate_pose(poses_by_sequence[packet_sequence], packet_sequence)
            events.append(LogicalRfEvent(
                event_type=LOGICAL_RF_EVENT,
                # Event time is the firmware's TX_START; completion is kept
                # separately as evidence that the operation finished.
                timestamp_us=int(start_record["timestamp_us"]),
                completion_timestamp_us=timestamp,
                tag_id=boot_id if boot_id is not None else 0,
                pose=pose,
                frequency_hz=int(start_record["value2"]),
                tx_power_dbm=tx_power_dbm,
                payload_hex=payload_hex,
                sequence=packet_sequence,
                provenance="FIRMWARE_TRACE",
                source_trace_sequence=int(start_record["sequence"]),
                status="SIMULATED",
                physical_rf_result=PhysicalRfResult(),
                _trace_evidence=_TRACE_EVIDENCE_TOKEN,
            ))
            pending_tx = None
        elif event_name in {"TIMEOUT", "ERROR", "RECOVERY", "REBOOT", "TRACE_END"}:
            # A failed/incomplete radio cycle cannot be promoted to TX evidence.
            if pending_tx is not None:
                pending_tx = None
            if pending_packet is not None:
                pending_packet = None
        elif pending_tx is not None and event_name not in {"IRQ", "STATE"}:
            # Preserve paired protocol records but reject ambiguous unrelated
            # events between a TX start and its completion.
            raise FirmwareTraceError(f"record {index}: unexpected {event_name!r} during active TX")
        elif pending_packet is not None and event_name not in {"STATE"}:
            raise FirmwareTraceError(f"record {index}: unexpected {event_name!r} before TX_START")

    # Do not fabricate completion for a trace that ends during packet creation/TX.
    return events


class AnchorReceiver:
    """In-memory logical anchor that records accepted events, not RF captures."""

    def __init__(self, anchor_id: str):
        if not isinstance(anchor_id, str) or not anchor_id.strip():
            raise ValueError("anchor_id must be a nonempty string")
        self.anchor_id = anchor_id
        self.log: list[AnchorLogEntry] = []
        self._accepted: set[tuple[int, int]] = set()

    def accept_logical_event(self, event: LogicalRfEvent) -> AnchorLogEntry | None:
        """Log one logical event once; no physical receive/RSSI is implied."""
        if event.event_type != LOGICAL_RF_EVENT:
            raise ValueError("anchor accepts only LOGICAL_RF_EVENT records")
        if (event._trace_evidence is not _TRACE_EVIDENCE_TOKEN
                or event.provenance != "FIRMWARE_TRACE" or event.status != "SIMULATED"):
            raise ValueError("anchor accepts only firmware-trace-backed logical events")
        key = (event.tag_id, event.sequence)
        if key in self._accepted:
            return None
        entry = AnchorLogEntry(
            event_type=ANCHOR_LOGICAL_RF_EVENT_ACCEPTED,
            provenance=LOGICAL_RF_EVENT,
            accepted=True,
            anchor_id=self.anchor_id,
            timestamp_us=event.timestamp_us,
            tag_id=event.tag_id,
            sequence=event.sequence,
            frequency_hz=event.frequency_hz,
            tx_power_dbm=event.tx_power_dbm,
            payload_hex=event.payload_hex,
            pose=event.pose,
            physical_rf_result=event.physical_rf_result,
        )
        self._accepted.add(key)
        self.log.append(entry)
        return entry
