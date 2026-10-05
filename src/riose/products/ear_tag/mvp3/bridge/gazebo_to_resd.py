#!/usr/bin/env python3
"""Convert Gazebo Harmonic IMU JSON/CSV samples to the Renode RESD contract.

The input is a recorded Gazebo IMU trace, not a live clock bridge. During
capture, Gazebo simulation time is the source clock. The adapter validates its
fixed sample interval, subtracts the first timestamp, and stores that origin
in the manifest. During replay, Renode's virtual clock is the clock master:
RESD sample times are relative to replay start and advance only as Renode
emulation advances. This preserves sample cadence, but does not preserve wall
clock duration or Gazebo real-time factor, and it does not provide live
lockstep/back-pressure between the two simulators.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

G0_M_S2 = Decimal("9.80665")
TIMESTAMP_TOLERANCE_S = Decimal("0.000000001")
MAX_SAMPLE_RATE_HZ = Decimal("1600")


def _number(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _first(mapping: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _timestamp_from_message(message: dict[str, Any], row: int) -> Decimal:
    direct = _first(message, ("timestamp_s", "simulation_time_s", "time_s", "stamp_s"))
    if direct is not None:
        return _number(direct, f"timestamp at sample {row}")

    header = message.get("header")
    stamp = header.get("stamp") if isinstance(header, dict) else None
    if isinstance(stamp, dict):
        seconds = _first(stamp, ("sec", "seconds"))
        nanos = _first(stamp, ("nsec", "nanosec", "nanoseconds"))
        if seconds is not None or nanos is not None:
            # Protobuf JSON omits default-valued seconds (zero) but still emits
            # nonzero nanoseconds for the first simulated second.
            timestamp = _number(seconds if seconds is not None else 0,
                                f"header.stamp.sec at sample {row}")
            if nanos is not None:
                timestamp += _number(nanos, f"header.stamp.nsec at sample {row}") / Decimal(1_000_000_000)
            return timestamp

    raise ValueError(
        f"sample {row} has no timestamp_s or Gazebo header.stamp.sec/nsec; "
        "record simulation time in the trace"
    )


def _acceleration_vector(message: dict[str, Any], row: int) -> tuple[Decimal, Decimal, Decimal]:
    vector = message.get("linear_acceleration", message.get("linearAcceleration"))
    if not isinstance(vector, dict):
        vector = message.get("acceleration")
    if isinstance(vector, dict):
        values = tuple(vector.get(axis) for axis in "xyz")
    else:
        values = tuple(
            _first(message, (
                f"a{axis}_m_s2",
                f"linear_acceleration_{axis}_m_s2",
                f"linear_acceleration.{axis}",
                f"linearAcceleration.{axis}",
                f"linear_acceleration_{axis}",
            ))
            for axis in "xyz"
        )
    if any(value is None for value in values):
        raise ValueError(
            f"sample {row} must contain linear_acceleration x/y/z in m/s^2; "
            "orientation and angular_velocity are not used by the LIS2DW12 replay"
        )
    return tuple(_number(value, f"linear_acceleration.{axis} at sample {row}") for axis, value in zip("xyz", values))


def _flatten_json(value: Any) -> list[dict[str, Any]]:
    """Accept one IMU message, an array, or common Gazebo recording wrappers."""
    if isinstance(value, list):
        messages: list[dict[str, Any]] = []
        for item in value:
            messages.extend(_flatten_json(item))
        return messages
    if not isinstance(value, dict):
        raise ValueError("JSON trace must contain IMU message objects")
    for key in ("messages", "data", "samples"):
        wrapped = value.get(key)
        if isinstance(wrapped, list):
            return _flatten_json(wrapped)
    # ros/gz recording wrappers commonly store the protobuf under `message`.
    wrapped = value.get("message")
    if isinstance(wrapped, dict) and not any(k in value for k in ("linear_acceleration", "acceleration")):
        return _flatten_json(wrapped)
    return [value]


def _parse_json(path: Path) -> list[tuple[Decimal, tuple[Decimal, Decimal, Decimal]]]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError("input trace is empty")
    try:
        document = json.loads(raw)
        messages = _flatten_json(document)
    except json.JSONDecodeError:
        # Gazebo topic JSON output can be captured as JSON Lines.
        messages = []
        for line_number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                messages.extend(_flatten_json(json.loads(line)))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON or JSON Lines input at line {line_number}") from exc
    rows = []
    for index, message in enumerate(messages, 1):
        rows.append((_timestamp_from_message(message, index), _acceleration_vector(message, index)))
    return rows


def _column(fieldnames: list[str] | None, candidates: tuple[str, ...], label: str) -> str:
    if fieldnames is None:
        raise ValueError("CSV trace has no header")
    for candidate in candidates:
        if candidate in fieldnames:
            return candidate
    raise ValueError(f"CSV is missing {label}; accepted columns: {', '.join(candidates)}")


def _parse_csv(path: Path) -> list[tuple[Decimal, tuple[Decimal, Decimal, Decimal]]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        time_key = _column(reader.fieldnames, ("timestamp_s", "simulation_time_s", "time_s"), "simulation timestamp")
        axis_keys = tuple(
            _column(reader.fieldnames, (
                f"a{axis}_m_s2", f"linear_acceleration_{axis}_m_s2",
                f"linear_acceleration.{axis}", f"linearAcceleration.{axis}",
                f"linear_acceleration_{axis}",
            ), f"linear acceleration {axis} (m/s^2)")
            for axis in "xyz"
        )
        rows = []
        for index, row in enumerate(reader, 1):
            rows.append((
                _number(row[time_key], f"timestamp at CSV row {index + 1}"),
                tuple(_number(row[key], f"{key} at CSV row {index + 1}") for key in axis_keys),
            ))
    return rows


def parse_trace(path: Path) -> list[tuple[Decimal, tuple[Decimal, Decimal, Decimal]]]:
    suffix = path.suffix.lower()
    if suffix in (".json", ".jsonl", ".ndjson"):
        return _parse_json(path)
    if suffix == ".csv":
        return _parse_csv(path)
    raise ValueError("input must be Gazebo IMU .json/.jsonl/.ndjson or .csv")


def normalize_trace(
    rows: list[tuple[Decimal, tuple[Decimal, Decimal, Decimal]]],
    sample_rate_hz: Decimal | None,
) -> tuple[Decimal, list[tuple[Decimal, Decimal, Decimal]]]:
    if not rows:
        raise ValueError("input trace contains no IMU samples")
    timestamps = [row[0] for row in rows]
    if any(not value.is_finite() for value in timestamps):
        raise ValueError("timestamps must be finite")
    deltas = [timestamps[index] - timestamps[index - 1] for index in range(1, len(timestamps))]
    if any(delta <= 0 for delta in deltas):
        raise ValueError("simulation timestamps must be strictly increasing")
    if sample_rate_hz is None:
        if not deltas:
            raise ValueError("a one-sample trace requires --sample-rate-hz")
        sample_rate_hz = Decimal(1) / deltas[0]
    if not sample_rate_hz.is_finite() or sample_rate_hz <= 0 or sample_rate_hz > MAX_SAMPLE_RATE_HZ:
        raise ValueError(f"sample rate must be finite and in (0, {MAX_SAMPLE_RATE_HZ}] Hz")
    period = Decimal(1) / sample_rate_hz
    for index, delta in enumerate(deltas, 1):
        if abs(delta - period) > TIMESTAMP_TOLERANCE_S:
            raise ValueError(
                f"timestamp interval before sample {index + 1} is {delta}s, "
                f"but declared/derived rate {sample_rate_hz} Hz requires {period}s "
                f"(tolerance {TIMESTAMP_TOLERANCE_S}s)"
            )
    normalized = [tuple(axis / G0_M_S2 for axis in vector) for _, vector in rows]
    return sample_rate_hz, normalized


def resample_trace(
    rows: list[tuple[Decimal, tuple[Decimal, Decimal, Decimal]]],
    sample_rate_hz: Decimal,
) -> list[tuple[Decimal, tuple[Decimal, Decimal, Decimal]]]:
    """Linearly interpolate an irregular capture onto a declared fixed cadence."""
    if len(rows) < 2:
        raise ValueError("resampling requires at least two IMU samples")
    if not sample_rate_hz.is_finite() or sample_rate_hz <= 0 or sample_rate_hz > MAX_SAMPLE_RATE_HZ:
        raise ValueError(f"resampling rate must be finite and in (0, {MAX_SAMPLE_RATE_HZ}] Hz")
    timestamps = [item[0] for item in rows]
    if any(not value.is_finite() for value in timestamps):
        raise ValueError("timestamps must be finite")
    if any(timestamps[i] <= timestamps[i - 1] for i in range(1, len(timestamps))):
        raise ValueError("simulation timestamps must be strictly increasing")
    period = Decimal(1) / sample_rate_hz
    start, end = timestamps[0], timestamps[-1]
    output = []
    source_index = 0
    count = int((end - start) * sample_rate_hz) + 1
    for index in range(count):
        timestamp = start + Decimal(index) * period
        while source_index + 1 < len(rows) - 1 and rows[source_index + 1][0] < timestamp:
            source_index += 1
        left_t, left_v = rows[source_index]
        right_t, right_v = rows[min(source_index + 1, len(rows) - 1)]
        if timestamp <= left_t or right_t == left_t:
            vector = left_v
        elif timestamp >= right_t:
            vector = right_v
        else:
            fraction = (timestamp - left_t) / (right_t - left_t)
            vector = tuple(a + fraction * (b - a) for a, b in zip(left_v, right_v))
        output.append((timestamp, vector))
    return output


def convert_trace(
    input_path: Path,
    output_dir: Path,
    sample_rate_hz: Decimal | None = None,
    seed: int = 0,
    *,
    resample_rate_hz: Decimal | None = None,
) -> Path:
    """Write a dataset manifest and normalized CSV for dataset_to_resd.py."""
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    source_rows = parse_trace(input_path)
    rows = resample_trace(source_rows, resample_rate_hz) if resample_rate_hz is not None else source_rows
    rate, acceleration_g = normalize_trace(rows, sample_rate_hz)
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = output_dir / "gazebo.csv"
    with normalized_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("timestamp_s", "x_g", "y_g", "z_g"))
        for index, vector in enumerate(acceleration_g):
            writer.writerow((f"{Decimal(index) / rate:.9f}", *(f"{axis:.9f}" for axis in vector)))
    manifest = {
        "schema_version": 1,
        "provenance": "SIMULATED",
        "description": "Gazebo Harmonic IMU acceleration replay; virtual sensor trace, not measured animal data.",
        "datasets": [{
            "name": "GAZEBO",
            "file": normalized_path.name,
            "axes": ["x", "y", "z"],
            "samples": len(rows),
            "sample_rate_hz": float(rate),
            "duration_s": float(Decimal(len(rows)) / rate),
            "unit": "g",
            "seed": seed,
            "status": "SIMULATED",
            "source": f"Gazebo Harmonic IMU trace {input_path.name}; acceleration converted from m/s^2 using g0={G0_M_S2} m/s^2",
            "source_file": input_path.name,
            "source_time_origin_s": str(rows[0][0]),
            "source_timestamp_tolerance_s": str(TIMESTAMP_TOLERANCE_S),
        }],
    }
    if resample_rate_hz is not None:
        source_deltas = [source_rows[i][0] - source_rows[i - 1][0] for i in range(1, len(source_rows))]
        manifest["datasets"][0]["transformations"] = [{
            "method": "linear_interpolation_to_uniform_cadence",
            "status": "INTERPOLATED",
            "source_samples": len(source_rows),
            "source_sample_interval_min_s": str(min(source_deltas)),
            "source_sample_interval_max_s": str(max(source_deltas)),
            "target_sample_rate_hz": float(resample_rate_hz),
        }]
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Gazebo IMU JSON/JSON Lines/CSV recording")
    parser.add_argument("--output-dir", type=Path, required=True, help="directory for GAZEBO manifest and normalized CSV")
    parser.add_argument("--sample-rate-hz", type=Decimal, help="required for one sample; otherwise validates/overrides inferred cadence")
    parser.add_argument("--seed", type=int, default=0, help="manifest identity seed (default: 0; no data is generated from it)")
    parser.add_argument("--resd-output", type=Path, help="also convert the GAZEBO dataset to RESD for Renode replay")
    parser.add_argument("--renode-home", type=Path, help="Renode install root passed through to dataset_to_resd.py")
    args = parser.parse_args()
    try:
        manifest_path = convert_trace(args.input, args.output_dir, args.sample_rate_hz, args.seed)
        if args.resd_output:
            converter = Path(__file__).parents[6] / "hardware" / "renode" / "scripts" / "dataset_to_resd.py"
            command = [sys.executable, str(converter), "GAZEBO", "--manifest", str(manifest_path), "--output", str(args.resd_output)]
            if args.renode_home:
                command.extend(("--renode-home", str(args.renode_home)))
            subprocess.run(command, check=True)
        dataset = json.loads(manifest_path.read_text(encoding="utf-8"))["datasets"][0]
        print(
            f"Wrote GAZEBO SIMULATED trace: samples={dataset['samples']} "
            f"rate={dataset['sample_rate_hz']:g} Hz unit=g manifest={manifest_path}"
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
