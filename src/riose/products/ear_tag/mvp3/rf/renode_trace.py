"""Convert captured Renode SX1262 transactions to firmware-trace JSONL.

This is a schema and unit adapter only. It does not invent positions, firmware
state transitions, RF reception, or current. Generated structural firmware
trace rows use ``state=NOT_CAPTURED`` and retain ``SIMULATED`` provenance.
"""
from __future__ import annotations

import argparse
import binascii
import json
from pathlib import Path
from typing import Any, Mapping


SOURCE_SCHEMA = "riose.renode.sx1262_tx_trace/v1"
FIRMWARE_SCHEMA = "riose.firmware.trace/v1"
EXPECTED_CLOCK = "Machine.ElapsedVirtualTime.TimeElapsed"
PLL_STEP_HZ_NUMERATOR = 32_000_000
PLL_STEP_HZ_DENOMINATOR = 1 << 25
PACKET_LENGTH = 24


class RenodeTraceConversionError(ValueError):
    """Renode trace cannot be mapped without missing or inventing evidence."""


def _integer(value: Any, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RenodeTraceConversionError(f"{label} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise RenodeTraceConversionError(f"{label} must be <= {maximum}")
    return value


def _frequency_hz(word: int) -> int:
    """Decode SX126x PLL word with exact nearest-integer rational arithmetic."""
    numerator = word * PLL_STEP_HZ_NUMERATOR
    return (numerator + PLL_STEP_HZ_DENOMINATOR // 2) // PLL_STEP_HZ_DENOMINATOR


def _make_record(sequence: int, timestamp_us: int, event: str, source: str,
                 value0: int, value1: int, value2: int, result: int,
                 packet_hex: str = "") -> dict[str, Any]:
    return {
        "schema_version": FIRMWARE_SCHEMA,
        "sequence": sequence,
        "status": "SIMULATED",
        "timestamp_us": timestamp_us,
        "event": event,
        # The SX1262 trace contains no MCU state capture. This explicit value
        # satisfies the shared trace envelope without asserting a state.
        "state": "NOT_CAPTURED",
        "source": source,
        "value0": value0,
        "value1": value1,
        "value2": value2,
        "result": result,
        "packet_hex": packet_hex,
    }


def convert_sx1262_trace(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return adapter-compatible firmware trace rows from a complete TX trace.

    The SX1262 captures nanoseconds, while the firmware adapters consume
    microseconds. TX timestamps are floored to the nearest representable
    microsecond. Frequency is decoded from the actual captured PLL word using
    the SX126x 32 MHz / 2^25 step. Packet metadata comes only from captured
    payload bytes. Incomplete or inconsistent transactions fail closed.
    """
    if not isinstance(source, Mapping):
        raise RenodeTraceConversionError("source trace must be a JSON object")
    if source.get("schema_version") != SOURCE_SCHEMA:
        raise RenodeTraceConversionError("unsupported SX1262 source schema")
    if source.get("clock") != EXPECTED_CLOCK or source.get("time_unit") != "ns":
        raise RenodeTraceConversionError("source trace must use Renode virtual time in ns")
    if source.get("provenance") != "SIMULATED":
        raise RenodeTraceConversionError("source trace provenance must be SIMULATED")
    transactions = source.get("records")
    if not isinstance(transactions, list) or not transactions:
        raise RenodeTraceConversionError("source trace must contain TX records")

    parsed: list[tuple[int, int, int, int, bytes, int, int]] = []
    previous_start_ns = -1
    previous_done_ns = -1
    tag_id: int | None = None
    for index, transaction in enumerate(transactions, 1):
        label = f"TX record {index}"
        if not isinstance(transaction, Mapping):
            raise RenodeTraceConversionError(f"{label} must be an object")
        if _integer(transaction.get("tx_index"), f"{label}.tx_index", minimum=1) != index:
            raise RenodeTraceConversionError(f"{label}.tx_index must be contiguous from one")
        if transaction.get("status") != "completed":
            raise RenodeTraceConversionError(f"{label} is not completed")
        if transaction.get("payload_captured") is not True:
            raise RenodeTraceConversionError(f"{label}.payload_captured must be true")
        start_ns = _integer(transaction.get("tx_start_ns"), f"{label}.tx_start_ns")
        done_ns = _integer(transaction.get("tx_done_ns"), f"{label}.tx_done_ns")
        if done_ns < start_ns:
            raise RenodeTraceConversionError(f"{label}.tx_done_ns precedes TX_START")
        if start_ns <= previous_start_ns or start_ns < previous_done_ns:
            raise RenodeTraceConversionError(f"{label} timestamps overlap or are not monotonic")
        previous_start_ns, previous_done_ns = start_ns, done_ns

        payload_length = _integer(transaction.get("payload_length"),
                                  f"{label}.payload_length", minimum=1)
        payload_hex = transaction.get("payload_hex")
        if (not isinstance(payload_hex, str) or payload_hex != payload_hex.lower()
                or len(payload_hex) != payload_length * 2
                or any(char not in "0123456789abcdef" for char in payload_hex)):
            raise RenodeTraceConversionError(f"{label} payload must be exact lowercase hex")
        if payload_length != PACKET_LENGTH:
            raise RenodeTraceConversionError(f"{label} payload must be {PACKET_LENGTH} bytes")
        payload = bytes.fromhex(payload_hex)
        if payload[0] != 1:
            raise RenodeTraceConversionError(f"{label} has unsupported telemetry packet version")
        if binascii.crc_hqx(payload[:22], 0xFFFF) != int.from_bytes(payload[22:24], "little"):
            raise RenodeTraceConversionError(f"{label} payload CRC is invalid")
        packet_tag_id = int.from_bytes(payload[2:6], "little")
        if tag_id is None:
            tag_id = packet_tag_id
        elif packet_tag_id != tag_id:
            raise RenodeTraceConversionError("TX payloads contain inconsistent tag ids")

        packet_time_ms = int.from_bytes(payload[10:14], "little")
        start_us = start_ns // 1_000
        done_us = done_ns // 1_000
        packet_time_us = packet_time_ms * 1_000
        if packet_time_us > start_us:
            raise RenodeTraceConversionError(f"{label} packet timestamp is after TX_START")

        frequency_word = _integer(transaction.get("rf_frequency_word"),
                                  f"{label}.rf_frequency_word", minimum=1, maximum=0xFFFFFFFF)
        tx_power_dbm = transaction.get("tx_power_dbm")
        if (isinstance(tx_power_dbm, bool) or not isinstance(tx_power_dbm, int)
                or not -128 <= tx_power_dbm <= 127):
            raise RenodeTraceConversionError(f"{label}.tx_power_dbm must be a signed 8-bit integer")
        parsed.append((packet_time_us, start_us, done_us, frequency_word,
                       payload, tx_power_dbm, payload_length))

    # The BOOT row is a structural adapter marker derived from the first
    # captured packet identity; no boot timing/state is claimed by the source.
    output = [_make_record(0, 0, "BOOT", "RENODE_SX1262_TRACE",
                           tag_id if tag_id is not None else 0, 0, 0, 0)]
    previous_us = 0
    sequence = 1
    for packet_time_us, start_us, done_us, frequency_word, payload, power_dbm, length in parsed:
        if packet_time_us < previous_us:
            raise RenodeTraceConversionError("converted event timestamps are not monotonic")
        if start_us < packet_time_us or done_us < start_us:
            raise RenodeTraceConversionError("microsecond conversion reverses a TX interval")
        frequency_hz = _frequency_hz(frequency_word)
        packet_sequence = int.from_bytes(payload[6:10], "little")
        behavior = payload[1]
        packet_hex = payload.hex()
        output.append(_make_record(sequence, packet_time_us, "PACKET_CREATED",
                                   "RENODE_SX1262_TRACE", length, packet_sequence,
                                   behavior, 0, packet_hex))
        sequence += 1
        output.append(_make_record(sequence, start_us, "TX_START", "SX1262",
                                   length, power_dbm & 0xFFFFFFFF, frequency_hz,
                                   0))
        sequence += 1
        output.append(_make_record(sequence, done_us, "TX_DONE", "SX1262",
                                   1, length, 0, 0))
        sequence += 1
        previous_us = done_us
    return output


def convert_sx1262_trace_file(source_path: str | Path,
                              output_path: str | Path) -> list[dict[str, Any]]:
    """Read a source JSON trace and write the converted firmware JSONL."""
    source_file = Path(source_path)
    output_file = Path(output_path)
    if source_file.resolve() == output_file.resolve():
        raise RenodeTraceConversionError("output must not overwrite the source trace")
    try:
        source = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RenodeTraceConversionError(f"cannot read source trace: {exc}") from exc
    records = convert_sx1262_trace(source)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n"
                                  for row in records), encoding="utf-8")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Renode SX1262 JSON trace")
    parser.add_argument("output", type=Path, help="firmware trace JSONL output")
    args = parser.parse_args()
    try:
        convert_sx1262_trace_file(args.source, args.output)
    except RenodeTraceConversionError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
