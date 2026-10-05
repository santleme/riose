#!/usr/bin/env python3
"""Convert portable firmware point events to assumed current intervals.

The C firmware trace supplies times and event boundaries only. Current values
and zero-resolution fallbacks must come from a caller-supplied ASSUMED profile;
this adapter never supplies or labels current as measured.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


class TraceConversionError(ValueError):
    """Trace cannot be converted without inventing a duration or load."""


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise TraceConversionError(f"{label} must be numeric, not boolean")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TraceConversionError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise TraceConversionError(f"{label} must be finite")
    return result


def _load_config(loads: dict[str, dict[str, Any]], key: str) -> dict[str, Any] | None:
    config = loads.get(key)
    if config is None:
        return None
    if not isinstance(config, dict):
        raise TraceConversionError(f"{key}: load profile entry must be an object")
    if config.get("current_status") != "ASSUMED":
        raise TraceConversionError(f"{key}: current_status must be ASSUMED")
    if not isinstance(config.get("source"), str) or not config["source"].strip():
        raise TraceConversionError(f"{key}: source is required for current provenance")
    current = _number(config.get("load_current_ma"), f"{key}.load_current_ma")
    if current < 0:
        raise TraceConversionError(f"{key}: current must be nonnegative")
    if not isinstance(config.get("component"), str) or not config["component"].strip():
        raise TraceConversionError(f"{key}: component is required")
    return {**config, "load_current_ma": current}


def _duration(config: dict[str, Any], key: str, measured_interval_s: float) -> tuple[float, str]:
    if measured_interval_s < 0:
        raise TraceConversionError(f"{key}: trace interval cannot be negative")
    if measured_interval_s > 0:
        return measured_interval_s, "TRACE_TIMESTAMP"
    fallback = config.get("fallback_duration_s")
    if fallback is None:
        raise TraceConversionError(
            f"{key}: event interval has zero timestamp resolution and no ASSUMED fallback_duration_s"
        )
    if config.get("duration_status") != "ASSUMED":
        raise TraceConversionError(f"{key}: fallback duration_status must be ASSUMED")
    if not config.get("duration_source"):
        raise TraceConversionError(f"{key}: fallback duration_source is required")
    duration = _number(fallback, f"{key}.fallback_duration_s")
    if duration <= 0:
        raise TraceConversionError(f"{key}: fallback duration must be positive")
    return duration, "ASSUMED_FALLBACK"


def trace_to_schedule(records: list[dict[str, Any]],
                      loads: dict[str, dict[str, Any]],
                      trace_duration_s: float | None = None) -> list[dict[str, Any]]:
    """Build power interval rows from firmware JSONL records and a load profile.

    `loads` keys use `state:<name>`, `event:<name>`, or
    `pair:<start_event>:<end_event>`. State intervals use adjacent STATE events;
    paired event intervals use their event timestamps. A configured point event
    uses its explicit ASSUMED fallback duration. The `MCU_SLEEP` payload can
    close a terminal SLEEP state interval because the harness may end during it.
    """
    if not records:
        raise TraceConversionError("trace contains no records")
    parsed = []
    previous_us = -1
    previous_sequence = -1
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise TraceConversionError(f"record {index}: expected a JSON object")
        if record.get("status") != "SIMULATED":
            raise TraceConversionError(f"record {index}: expected status=SIMULATED")
        timestamp_us = _number(record.get("timestamp_us"), f"record {index}.timestamp_us")
        if timestamp_us < 0 or timestamp_us < previous_us:
            raise TraceConversionError(f"record {index}: timestamps must be nonnegative and monotonic")
        previous_us = timestamp_us
        if not record.get("event") or not record.get("state"):
            raise TraceConversionError(f"record {index}: event and state are required")
        schema = record.get("schema_version")
        if schema is not None and schema != "riose.firmware.trace/v1":
            raise TraceConversionError(f"record {index}: unsupported schema_version {schema!r}")
        if "sequence" in record:
            sequence = record["sequence"]
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise TraceConversionError(f"record {index}: sequence must be an integer")
            if sequence != previous_sequence + 1:
                raise TraceConversionError(f"record {index}: sequence must be contiguous from zero")
            previous_sequence = sequence
        parsed.append({**record, "_timestamp_s": timestamp_us / 1_000_000.0})

    pending_radio = None
    radio_pairs = {"TX_START": "TX_DONE", "RX_START": "RX_DONE"}
    for index, record in enumerate(parsed, start=1):
        event = record["event"]
        if event in radio_pairs:
            if pending_radio is not None:
                raise TraceConversionError(f"record {index}: radio intervals overlap ({pending_radio} already active)")
            pending_radio = event
        elif event in radio_pairs.values():
            expected_start = next(start for start, end in radio_pairs.items() if end == event)
            if pending_radio != expected_start:
                raise TraceConversionError(f"record {index}: {event} does not close the active radio interval")
            pending_radio = None

    trace_start_s = parsed[0]["_timestamp_s"]
    if any("sequence" in record for record in parsed) and not all(
            "sequence" in record for record in parsed):
        raise TraceConversionError("sequence must be present on every trace record or none")
    # Work in trace-relative time so every scenario profile starts at t=0.
    for record in parsed:
        record["_timestamp_s"] -= trace_start_s

    rows: list[dict[str, Any]] = []

    def append(key: str, event: str, state: str, start_s: float, elapsed_s: float,
               elapsed_source: str | None = None) -> None:
        config = _load_config(loads, key)
        if config is None:
            return
        duration, duration_source = _duration(config, key, elapsed_s)
        rows.append({
            "timestamp_s": start_s,
            "event": event,
            "state": state,
            "component": str(config["component"]),
            "duration_s": duration,
            "load_current_ma": config["load_current_ma"],
            "status": "SIMULATED",
            "current_status": "ASSUMED",
            "duration_source": elapsed_source or duration_source,
            "provenance": str(config["source"]),
        })

    # State intervals are inferred from adjacent transitions; close a terminal
    # SLEEP interval from the HAL wait duration recorded by the C FSM.
    state_records = [r for r in parsed if r["event"] == "STATE"]
    for i, record in enumerate(state_records):
        state = str(record["state"])
        key = f"state:{state}"
        config = _load_config(loads, key)
        if config is None:
            raise TraceConversionError(f"{key}: load profile entry is required to cover the trace state")
        start = record["_timestamp_s"]
        if i + 1 < len(state_records):
            elapsed = state_records[i + 1]["_timestamp_s"] - start
            # Several state transitions may share a HAL millisecond tick. They
            # represent zero-duration markers at this trace resolution, not a
            # request to invent an entire dwell interval for each repeated state.
            # The explicit idle baseline in the power model still spans time.
            if elapsed == 0:
                continue
        else:
            sleep = next((r for r in reversed(parsed) if r["event"] == "MCU_SLEEP" and
                          r["state"] == state and r.get("value0") is not None and
                          r["_timestamp_s"] >= start), None)
            if sleep is None:
                # A final state marker has no measured dwell. Do not turn the
                # load profile's assumed fallback into time absent from trace.
                continue
            elapsed = _number(sleep["value0"], "MCU_SLEEP.value0") / 1000.0
            if elapsed == 0:
                continue
            append(key, state, state, start, elapsed,
                   "TRACE_EVENT_PAYLOAD")
            continue
        append(key, state, state, start, elapsed)

    structural_events = {
        "BOOT", "MCU_INIT", "PACKET_CREATED", "RADIO_STANDBY", "RADIO_SLEEP",
        "ERROR", "RECOVERY", "MCU_SLEEP", "STATE", "SPI", "IRQ", "TIMEOUT",
        "WAKE", "TRACE_END",
    }
    event_coverage: dict[str, dict[str, Any]] = {}
    for record in parsed:
        event = str(record["event"])
        if event in {"TX_START", "TX_DONE", "RX_START", "RX_DONE"}:
            handler = "PAIRED_RADIO_INTERVAL"
            if f"event:{event}" in loads:
                raise TraceConversionError(
                    f"event:{event}: radio markers are covered by paired intervals and cannot also be point loads")
        elif f"event:{event}" in loads:
            handler = "ASSUMED_POINT_INTERVAL"
        elif event in structural_events:
            handler = "STRUCTURAL_MARKER_NO_SEPARATE_LOAD"
        else:
            raise TraceConversionError(f"event:{event}: no state, pair, point-load, or structural handling")
        entry = event_coverage.setdefault(event, {"count": 0, "handling": handler})
        if entry["handling"] != handler:
            raise TraceConversionError(f"event:{event}: inconsistent event handling")
        entry["count"] += 1

    # Explicitly paired radio stages preserve their event duration. A missing
    # end marker is an error when that interval is requested in the load profile.
    for start_event, end_event, label in (("TX_START", "TX_DONE", "TX"),
                                          ("RX_START", "RX_DONE", "RX")):
        key = f"pair:{start_event}:{end_event}"
        config = _load_config(loads, key)
        starts = [r for r in parsed if r["event"] == start_event]
        ends = [r for r in parsed if r["event"] == end_event]
        if (starts or ends) and config is None:
            raise TraceConversionError(f"{key}: load profile entry is required to cover radio intervals")
        if config is None:
            continue
        if len(starts) != len(ends):
            raise TraceConversionError(
                f"{label} trace has {len(starts)} start(s) and {len(ends)} end(s); cannot infer interval"
            )
        pending = None
        intervals = []
        for record in parsed:
            if record["event"] == start_event:
                if pending is not None:
                    raise TraceConversionError(f"{label} trace has overlapping or nested start markers")
                pending = record
            elif record["event"] == end_event:
                if pending is None:
                    raise TraceConversionError(f"{label} trace has an end marker without a preceding start")
                if record["_timestamp_s"] < pending["_timestamp_s"]:
                    raise TraceConversionError(f"{label} end marker cannot precede its start")
                intervals.append((pending, record))
                pending = None
        if pending is not None:
            raise TraceConversionError(f"{label} trace has a start marker without a following end")
        for start, end in intervals:
            elapsed = end["_timestamp_s"] - start["_timestamp_s"]
            interval_state = "RF_TX" if label == "TX" else "RF_RX"
            append(key, label, interval_state, start["_timestamp_s"], elapsed)

    # Point events can represent short component work only when the caller
    # provides an explicit ASSUMED duration; no default dwell is fabricated.
    for record in parsed:
        key = f"event:{record['event']}"
        config = _load_config(loads, key)
        if config is None:
            continue
        if config.get("fallback_duration_s") is None:
            raise TraceConversionError(f"{key}: point event requires ASSUMED fallback_duration_s")
        append(key, str(record["event"]), str(record["state"]),
               record["_timestamp_s"], 0.0)

    if not rows:
        raise TraceConversionError("load profile produced no intervals for this trace")
    terminal_sleep = parsed[-1] if parsed[-1]["event"] == "MCU_SLEEP" else None
    trace_end_s = parsed[-1]["_timestamp_s"]
    if terminal_sleep is not None and terminal_sleep.get("value0") is not None:
        sleep_s = _number(terminal_sleep["value0"], "MCU_SLEEP.value0") / 1000.0
        if sleep_s > 0:
            trace_end_s += sleep_s
    if trace_duration_s is not None:
        trace_duration_s = _number(trace_duration_s, "trace_duration_s")
        if trace_duration_s <= 0 or trace_duration_s < trace_end_s - 1e-12:
            raise TraceConversionError(
                "trace_duration_s must be positive and cover all firmware trace intervals")
        trace_end_s = trace_duration_s
    for row in rows:
        if row["timestamp_s"] + row["duration_s"] > trace_end_s + 1e-12:
            raise TraceConversionError(
                f"{row['event']}: modeled interval extends beyond the trace window")
        row["trace_window_start_s"] = 0.0
        row["trace_window_end_s"] = trace_end_s
        row["trace_source_start_s"] = trace_start_s
        row["trace_event_coverage"] = event_coverage
        row["trace_status"] = "SIMULATED"
        row["trace_schema_version"] = "riose.firmware.trace/v1"
    return sorted(rows, key=lambda row: (row["timestamp_s"], row["component"], row["event"]))


def read_load_profile(path: Path) -> dict[str, dict[str, Any]]:
    """Read the orchestrator's ASSUMED profile envelope or a bare JSON mapping."""
    loads: Any = json.loads(path.read_text())
    if not isinstance(loads, dict):
        raise TraceConversionError("load profile must be a JSON object")
    if isinstance(loads.get("loads"), dict):
        if loads.get("status") != "ASSUMED":
            raise TraceConversionError("load profile envelope must have status=ASSUMED")
        if loads.get("schema_version") != "riose.power.loads/v1":
            raise TraceConversionError("unsupported load profile schema_version")
        loads = loads["loads"]
    return loads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="SIMULATED firmware trace JSONL")
    parser.add_argument("--loads", required=True, type=Path,
                        help="ASSUMED JSON load profile generated from hardware/spec.yaml")
    parser.add_argument("--output", required=True, type=Path,
                        help="power-tool-compatible schedule JSONL output")
    parser.add_argument("--trace-duration-s", type=float,
                        help="SIMULATED experiment duration when the firmware trace ends before simulation time")
    args = parser.parse_args()
    args.output.unlink(missing_ok=True)
    try:
        trace_bytes = args.trace.read_bytes()
        records = [json.loads(line) for line in trace_bytes.decode("utf-8").splitlines() if line.strip()]
        loads = read_load_profile(args.loads)
        rows = trace_to_schedule(records, loads, args.trace_duration_s)
        trace_provenance = {"path": str(args.trace), "sha256": hashlib.sha256(trace_bytes).hexdigest(),
                            "status": "SIMULATED", "schema_version": "riose.firmware.trace/v1"}
        for row in rows:
            row["trace_provenance"] = trace_provenance
    except (TraceConversionError, json.JSONDecodeError, OSError, UnicodeError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as stream:
        if args.output.suffix.lower() == ".json":
            json.dump(rows, stream, indent=2, sort_keys=True)
            stream.write("\n")
        else:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
