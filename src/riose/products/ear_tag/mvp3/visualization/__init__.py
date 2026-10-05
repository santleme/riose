"""Build a self-contained view from recorded MVP3 experiment streams.

Gazebo pose and IMU data are plotted on simulation time. Firmware, logical RF,
anchor and power-schedule events use source-trace time unless an explicitly
versioned offline or barrier-validated live clock mapping is present.
No event is joined to an animal pose here.
"""

from __future__ import annotations

import csv
import html
import json
import math
from pathlib import Path
from typing import Any

AXIS_COLORS = {"x": "#d95f02", "y": "#1b9e77", "z": "#386cb0"}
WIDTH, HEIGHT = 960, 420
PLOT = (72, 34, 900, 338)


def _number(value: Any, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{description} must be a finite number")
    return float(value)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path.name} line {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path.name} line {line_number} must be a JSON object")
            records.append(value)
    return records


def _read_imu_csv(experiment: Path) -> list[tuple[float, float, float, float]]:
    path = experiment / "gazebo_imu.csv"
    if not path.is_file():
        raise FileNotFoundError(f"Missing normalized Gazebo trace: {path}")
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = {"timestamp_s", "x_g", "y_g", "z_g"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"{path} must contain columns: timestamp_s, x_g, y_g, z_g")
        for line, row in enumerate(reader, 2):
            try:
                sample = tuple(float(row[key]) for key in ("timestamp_s", "x_g", "y_g", "z_g"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid numeric IMU value at CSV line {line}") from exc
            if not all(math.isfinite(value) for value in sample):
                raise ValueError(f"Non-finite IMU value at CSV line {line}")
            if rows and sample[0] <= rows[-1][0]:
                raise ValueError(f"IMU timestamps must increase (CSV line {line})")
            rows.append(sample)
    if not rows:
        raise ValueError(f"No IMU samples in {path}")
    return rows


def _read_recording(experiment: Path) -> list[dict[str, Any]]:
    path = experiment / "recording.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"Missing timestamped pose recording: {path}")
    rows = _jsonl(path)
    previous_pose_time = -math.inf
    previous_imu_time = -math.inf
    output = []
    for index, row in enumerate(rows, 1):
        imu_time = _number(row.get("simulation_timestamp_s"), f"recording row {index} simulation_timestamp_s")
        pose_time = _number(row.get("pose_sample_timestamp_s"), f"recording row {index} pose_sample_timestamp_s")
        if imu_time <= previous_imu_time or pose_time < previous_pose_time:
            raise ValueError(f"recording timestamps must be monotonic (row {index})")
        previous_imu_time, previous_pose_time = imu_time, pose_time
        pose = row.get("tag_pose")
        position = pose.get("position") if isinstance(pose, dict) else None
        if not isinstance(position, dict) or not all(axis in position for axis in "xyz"):
            raise ValueError(f"recording row {index} has no complete timestamp-backed tag_pose.position")
        coordinates = {axis: _number(position[axis], f"recording row {index} tag pose {axis}") for axis in "xyz"}
        output.append({"pose_time": pose_time, "position": coordinates})
    if not output:
        raise ValueError(f"No timestamped pose records in {path}")
    return output


def _downsample(samples: list[tuple[float, float, float, float]], limit: int = 1400):
    if len(samples) <= limit:
        return samples
    stride = (len(samples) - 1) / (limit - 1)
    return [samples[round(index * stride)] for index in range(limit)]


def _line_chart(samples: list[tuple[float, float, float, float]], *,
                y_label: str, title: str,
                axes: tuple[str, ...] = ("x", "y", "z")) -> str:
    left, top, right, bottom = PLOT
    start, end = samples[0][0], samples[-1][0]
    duration = max(end - start, 1e-9)
    axis_indices = {axis: "xyz".index(axis) + 1 for axis in axes}
    values = [row[index] for row in samples for index in axis_indices.values()]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        lo, hi = lo - 0.5, hi + 0.5
    else:
        padding = max((hi - lo) * 0.08, 0.03)
        lo, hi = lo - padding, hi + padding

    def xy(index: int, value: float) -> tuple[float, float]:
        tx = left + (samples[index][0] - start) / duration * (right - left)
        ty = bottom - (value - lo) / (hi - lo) * (bottom - top)
        return tx, ty

    elements = [f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{html.escape(title)}">',
                '<rect width="100%" height="100%" fill="#fff"/>',
                f'<text x="{left}" y="22" class="chart-title">{html.escape(title)}</text>']
    for step in range(5):
        value = lo + (hi - lo) * step / 4
        y = bottom - (bottom - top) * step / 4
        elements.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" class="grid"/>')
        elements.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end">{value:.2f}</text>')
    elements.extend([f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" class="axis"/>',
                     f'<text x="{(left+right)/2}" y="{HEIGHT-12}" text-anchor="middle">Gazebo simulation time (s)</text>',
                     f'<text x="18" y="{(top+bottom)/2}" transform="rotate(-90 18 {(top+bottom)/2})" text-anchor="middle">{html.escape(y_label)}</text>'])
    for axis in axes:
        axis_index = axis_indices[axis]
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                           (xy(index, row[axis_index]) for index, row in enumerate(samples)))
        elements.append(f'<polyline fill="none" stroke="{AXIS_COLORS[axis]}" stroke-width="1.7" points="{points}"/>')
    elements.append("</svg>")
    return "\n".join(elements)


def _trajectory_data(recording: list[dict[str, Any]]) -> list[dict[str, float]]:
    return [{"x": row["position"]["x"], "y": row["position"]["y"], "z": row["position"]["z"],
             "timestamp_s": row["pose_time"]} for row in recording]


def _clock_mapping(experiment: Path) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """Return a versioned offline mapping or barrier-validated live mapping."""
    replay_path = experiment / "firmware_replay.json"
    replay: dict[str, Any] = {}
    if replay_path.is_file():
        replay = json.loads(replay_path.read_text(encoding="utf-8"))
        if not isinstance(replay, dict) or "clock_mapping" not in replay:
            return None, None, None
        value = replay["clock_mapping"]
    else:
        manifest_path = experiment / "manifest.json"
        if not manifest_path.is_file():
            return None, None, None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or "clock_mapping" not in manifest:
            return None, None, None
        value = manifest["clock_mapping"]
    if not isinstance(value, dict):
        return None, "clock_mapping must be a JSON object", None
    common = {
        "schema_version": "riose.mvp3.clock_mapping/v1",
        "status": "SIMULATED",
        "source_clock": "RENODE_VIRTUAL_TIME",
        "target_clock": "GAZEBO_SIMULATION_TIME",
    }
    for key, expected_value in common.items():
        if value.get(key) != expected_value:
            return None, f"clock_mapping.{key} is missing or unsupported", None
    method = value.get("method")
    is_live = method == "LIVE_GAZEBO_MASTER_LOCKSTEP" and value.get("live_lockstep") is True
    is_offline = method == "AFFINE_OFFLINE_RESD_REPLAY" and value.get("live_lockstep") is False
    if not (is_live or is_offline):
        return None, "clock_mapping method/live_lockstep combination is unsupported", None
    try:
        scale = _number(value.get("scale"), "clock_mapping.scale")
        offset = _number(value.get("offset_s"), "clock_mapping.offset_s")
    except ValueError as exc:
        return None, str(exc), None
    if scale <= 0:
        return None, "clock_mapping.scale must be greater than zero", None
    mapping = dict(value, scale=scale, offset_s=offset)
    if is_live:
        barrier_path = value.get("barrier_evidence")
        if not isinstance(barrier_path, str):
            return None, "live clock_mapping needs barrier_evidence", None
        evidence = Path(barrier_path)
        if not evidence.is_absolute():
            evidence = evidence if evidence.is_file() else experiment / evidence
        try:
            barriers = [json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines()
                        if line.strip()]
            barriers = [row for row in barriers if row.get("event") == "CLOCK_BARRIER"]
            if not barriers:
                return None, "live clock mapping has no CLOCK_BARRIER evidence", None
            previous = -math.inf
            for row in barriers:
                def barrier_number(field: str) -> float:
                    raw = row.get(field)
                    if isinstance(raw, bool):
                        raise ValueError(f"barrier.{field} must be numeric")
                    try:
                        number = float(raw)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"barrier.{field} must be numeric") from exc
                    if not math.isfinite(number):
                        raise ValueError(f"barrier.{field} must be finite")
                    return number

                sim = barrier_number("simulation_timestamp_s")
                renode = barrier_number("renode_timestamp_s")
                error = barrier_number("clock_error_s")
                if sim <= previous or abs(sim - renode) > 1e-9 or abs(error) > 1e-9:
                    return None, "live Gazebo/Renode barrier timestamps are not synchronized", None
                previous = sim
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return None, f"live clock barrier evidence is invalid: {exc}", None
        if abs(scale - 1.0) > 1e-12 or abs(offset) > 1e-12:
            return None, "live lockstep mapping must have unit scale and zero offset", None
        return mapping, None, None
    # The replay contract nests the optional check beside scale/offset in the
    # mapping object. Accept the earlier top-level placement for compatibility.
    check = value.get("alignment_check", replay.get("alignment_check"))
    return mapping, None, check if isinstance(check, dict) else None


def _timestamped_events(experiment: Path) -> dict[str, list[dict[str, Any]]]:
    tracks: dict[str, list[dict[str, Any]]] = {
        "firmware": [], "logical_rf": [], "anchor": [], "power": []}
    firmware_path = experiment / "firmware_trace.jsonl"
    if firmware_path.is_file():
        for index, row in enumerate(_jsonl(firmware_path), 1):
            if row.get("schema_version") != "riose.firmware.trace/v1" or row.get("status") != "SIMULATED":
                raise ValueError(f"firmware trace row {index} has unsupported schema or provenance")
            timestamp = row.get("timestamp_us")
            if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
                raise ValueError(f"firmware trace row {index} has invalid timestamp_us")
            tracks["firmware"].append({"t": timestamp / 1_000_000, "label": str(row.get("event", "event")),
                                       "source": "firmware_trace.jsonl"})
    rf_path = experiment / "rf_events.jsonl"
    if rf_path.is_file():
        for index, row in enumerate(_jsonl(rf_path), 1):
            timestamp, completion = row.get("timestamp_us"), row.get("completion_timestamp_us")
            if (row.get("event_type") != "LOGICAL_RF_EVENT" or row.get("status") != "SIMULATED"
                    or not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0
                    or not isinstance(completion, int) or isinstance(completion, bool) or completion < timestamp):
                raise ValueError(f"logical RF row {index} is missing valid timestamp-backed event evidence")
            tracks["logical_rf"].append({"t": timestamp / 1_000_000,
                                         "end": completion / 1_000_000,
                                         "label": f"TX seq {row.get('sequence', '?')}",
                                         "source": "rf_events.jsonl · logical TX only"})
    anchor_path = experiment / "anchor_events.jsonl"
    if anchor_path.is_file():
        for index, row in enumerate(_jsonl(anchor_path), 1):
            timestamp = row.get("timestamp_us")
            if (row.get("event_type") != "ANCHOR_LOGICAL_RF_EVENT_ACCEPTED" or row.get("accepted") is not True
                    or not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0):
                raise ValueError(f"anchor row {index} is missing accepted timestamp-backed logical-event evidence")
            tracks["anchor"].append({"t": timestamp / 1_000_000,
                                     "label": f"accepted seq {row.get('sequence', '?')}",
                                     "source": "anchor_events.jsonl · logical acceptance"})
    schedule_path = experiment / "power" / "schedule.jsonl"
    if not schedule_path.is_file():
        schedule_path = experiment / "schedule.jsonl"
    if schedule_path.is_file():
        for index, row in enumerate(_jsonl(schedule_path), 1):
            if row.get("status") != "SIMULATED" or not isinstance(row.get("trace_provenance"), dict):
                raise ValueError(f"power schedule row {index} lacks SIMULATED firmware-trace provenance")
            start = _number(row.get("timestamp_s"), f"power schedule row {index} timestamp_s")
            duration = _number(row.get("duration_s"), f"power schedule row {index} duration_s")
            if start < 0 or duration < 0:
                raise ValueError(f"power schedule row {index} has negative time or duration")
            tracks["power"].append({"t": start, "end": start + duration,
                                    "label": f"{row.get('event', 'load')} · {row.get('component', 'load')} ({row.get('duration_source', 'duration')})",
                                    "source": "power/schedule.jsonl · simulated schedule; assumed current"})
    return tracks


def _verify_alignment_check(check: dict[str, Any] | None, mapping: dict[str, Any] | None,
                            experiment: Path, imu: list[tuple[float, float, float, float]]) -> tuple[dict[str, float] | None, str | None]:
    """Recompute the claimed TX↔IMU nearest-sample check from source records."""
    if check is None:
        return None, None
    if mapping is None:
        return None, "alignment_check is present but no valid clock mapping is available"
    if check.get("status") != "RECOMPUTED_FROM_RECORDS":
        return None, "alignment_check status is not RECOMPUTED_FROM_RECORDS"
    trace_path = experiment / "firmware_trace.jsonl"
    if not trace_path.is_file():
        return None, "alignment_check cannot be verified without firmware_trace.jsonl"
    tx_starts = []
    for index, row in enumerate(_jsonl(trace_path), 1):
        if row.get("schema_version") != "riose.firmware.trace/v1" or row.get("status") != "SIMULATED":
            return None, f"firmware trace row {index} has unsupported schema or provenance"
        timestamp = row.get("timestamp_us")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            return None, f"firmware trace row {index} has invalid timestamp_us"
        if row.get("event") == "TX_START":
            tx_starts.append(timestamp / 1_000_000)
    try:
        claimed_source = _number(check.get("renode_tx_start_s"), "alignment_check.renode_tx_start_s")
        claimed_target = _number(check.get("mapped_gazebo_time_s"), "alignment_check.mapped_gazebo_time_s")
        claimed_nearest = _number(check.get("nearest_imu_sample_time_s"), "alignment_check.nearest_imu_sample_time_s")
        claimed_error = _number(check.get("absolute_error_s"), "alignment_check.absolute_error_s")
    except ValueError as exc:
        return None, str(exc)
    # The canonical firmware trace stores microseconds; the replay's alignment
    # check may retain nanoseconds, so compare within one trace tick.
    source_candidates = [time for time in tx_starts if abs(time - claimed_source) <= 1e-6]
    if len(source_candidates) != 1:
        return None, "alignment_check TX_START time does not uniquely match a recorded firmware event"
    source_time = source_candidates[0]
    mapped_time = mapping["scale"] * source_time + mapping["offset_s"]
    nearest_sample = min((row[0] for row in imu), key=lambda time: (abs(time - mapped_time), time))
    absolute_error = abs(nearest_sample - mapped_time)
    tolerance = 1e-6
    if (abs(claimed_target - mapped_time) > tolerance
            or abs(claimed_nearest - nearest_sample) > tolerance
            or abs(claimed_error - absolute_error) > tolerance):
        return None, "alignment_check values do not match the clock mapping, firmware trace, and Gazebo IMU CSV"
    return {"renode_tx_start_s": source_time, "mapped_gazebo_time_s": mapped_time,
            "nearest_imu_sample_time_s": nearest_sample, "absolute_error_s": absolute_error}, None


def _event_html(tracks: dict[str, list[dict[str, Any]]], *,
                gazebo_range: tuple[float, float],
                mapping: dict[str, Any] | None) -> tuple[str, str]:
    titles = {"firmware": "Firmware trace", "logical_rf": "Logical RF TX", "anchor": "Anchor logical acceptance", "power": "Power schedule"}
    colors = {"firmware": "#475467", "logical_rf": "#d97706", "anchor": "#1570ef", "power": "#7a5af8"}
    mapped_tracks: dict[str, list[dict[str, Any]]] = {}
    for lane, values in tracks.items():
        mapped_tracks[lane] = []
        for event in values:
            item = dict(event)
            if mapping:
                item["trace_t"] = item["t"]
                item["t"] = mapping["scale"] * item["t"] + mapping["offset_s"]
                if "end" in item:
                    item["trace_end"] = item["end"]
                    item["end"] = mapping["scale"] * item["end"] + mapping["offset_s"]
            mapped_tracks[lane].append(item)
    tracks = mapped_tracks
    all_times = ([gazebo_range[0], gazebo_range[1]] if mapping else
                 [event[key] for values in tracks.values() for event in values for key in ("t", "end") if key in event])
    if not all_times:
        return "<p class=muted>No timestamped firmware, logical RF, anchor, or power records were found.</p>", ""
    lo, hi = min(all_times), max(all_times)
    if hi <= lo:
        hi = lo + 0.001
    left, right = 220, 930
    span = right - left
    x_axis_title = "Gazebo simulation time (s) · affine offline mapping" if mapping else "trace-relative time (s)"
    items = [f'<svg viewBox="0 0 960 {max(150, 52 * len(titles) + 55)}" role="img" aria-label="Firmware events on Gazebo simulation time"' if mapping else
             f'<svg viewBox="0 0 960 {max(150, 52 * len(titles) + 55)}" role="img" aria-label="Firmware trace-time event lanes"',
             '<rect width="100%" height="100%" fill="#fff"/>']
    row_y = {}
    for lane, name in enumerate(titles):
        y = 42 + lane * 52
        row_y[name] = y
        items.append(f'<text x="8" y="{y+4}" class="lane-title">{titles[name]}</text>')
        items.append(f'<line x1="{left}" y1="{y}" x2="{right}" y2="{y}" class="grid"/>')
    for tick in range(5):
        tick_time = lo + (hi - lo) * tick / 4
        tick_x = left + span * tick / 4
        items.append(f'<line x1="{tick_x:.2f}" y1="25" x2="{tick_x:.2f}" y2="{42+(len(titles)-1)*52+18}" class="grid"/>')
        items.append(f'<text x="{tick_x:.2f}" y="{42+len(titles)*52}" text-anchor="middle" class="muted">{tick_time:.3f}</text>')
    items.append(f'<text x="{(left+right)/2}" y="{42+len(titles)*52+18}" text-anchor="middle" class="muted">{x_axis_title}</text>')
    detail = []
    for lane, values in tracks.items():
        y = row_y[lane]
        for event in values:
            if mapping and (event.get("end", event["t"]) < lo or event["t"] > hi):
                # Keep the exact event in the detail list, but don't extrapolate
                # outside the recorded Gazebo window in the aligned plot.
                plot_event = False
            else:
                plot_event = True
            x = left + (event["t"] - lo) / (hi - lo) * span
            if plot_event:
                if "end" in event and event["end"] > event["t"]:
                    visible_start, visible_end = max(event["t"], lo), min(event["end"], hi)
                    start_x = left + (visible_start - lo) / (hi - lo) * span
                    end_x = left + (visible_end - lo) / (hi - lo) * span
                    items.append(f'<line x1="{start_x:.2f}" y1="{y}" x2="{end_x:.2f}" y2="{y}" stroke="{colors[lane]}" stroke-width="7" stroke-linecap="round"/>')
                if lo <= event["t"] <= hi:
                    items.append(f'<circle cx="{x:.2f}" cy="{y}" r="5" fill="{colors[lane]}"/>')
            time_text = (f'{event["trace_t"]:.6f} s Renode → {event["t"]:.6f} s Gazebo'
                         if mapping else f'{event["t"]:.6f} s trace-relative')
            detail.append(f'<li><strong>{html.escape(titles[lane])}</strong> · {time_text} · {html.escape(event["label"])} <span class="muted">({html.escape(event["source"])})</span></li>')
    items.append("</svg>")
    return "\n".join(items), "<ul class=event-list>" + "".join(detail) + "</ul>"


def create_viewer(experiment: Path, output: Path | None = None) -> Path:
    """Create a self-contained synchronized-record viewer and companion SVG."""
    experiment = experiment.resolve()
    imu = _read_imu_csv(experiment)
    imu_sampled = _downsample(imu)
    recording = _read_recording(experiment)
    pose_samples = [(row["pose_time"], row["position"]["x"],
                     row["position"]["y"], row["position"]["z"]) for row in recording]
    # Draw each coordinate against the same Gazebo clock without inventing samples.
    pose_svg_parts = []
    for axis in "xyz":
        pose_svg_parts.append(_line_chart(_downsample(pose_samples), axes=(axis,),
                                          y_label=f"Tag position {axis} (m)",
                                          title=f"Ear-tag position · {axis.upper()} vs Gazebo simulation time"))
    tracks = _timestamped_events(experiment)
    mapping, mapping_error, alignment_check = _clock_mapping(experiment)
    checked_alignment, alignment_error = _verify_alignment_check(alignment_check, mapping, experiment, imu)
    if alignment_error:
        # A replay's optional consistency check must not be allowed to override
        # the source traces. Keep the original trace-time view in that case.
        mapping = None
        mapping_error = alignment_error
    event_svg, event_list = _event_html(
        tracks, gazebo_range=(imu[0][0], imu[-1][0]), mapping=mapping)
    manifest_path = experiment / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    title_kind = "live synchronized simulation" if mapping and mapping.get("live_lockstep") else "offline replay"
    title = f"MVP3 {title_kind} — {manifest.get('scenario', experiment.name)}"
    imu_svg = _line_chart(imu_sampled, y_label="Acceleration (g)",
                          title="Recorded Gazebo IMU acceleration")
    target = (output or experiment / "imu_viewer.html").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Companion SVG remains a portable IMU-only plot for existing consumers.
    target.with_suffix(".svg").write_text(imu_svg, encoding="utf-8")
    trajectory = json.dumps(_trajectory_data(recording), separators=(",", ":"))
    trajectory = trajectory.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    events_exist = any(tracks.values())
    if mapping:
        if mapping.get("live_lockstep"):
            clock_notice = ("Valid SIMULATED live lockstep mapping: Renode and Gazebo simulation times "
                            "matched at every recorded CLOCK_BARRIER.")
        else:
            clock_notice = ("Valid SIMULATED affine offline replay mapping: "
                            f"Gazebo time = {mapping['scale']:.9g} × Renode time + {mapping['offset_s']:.9g} s. "
                            "This is an offline RESD replay alignment, not live lockstep.")
        if checked_alignment:
            clock_notice += (" Recomputed from records: TX_START at "
                             f"{checked_alignment['renode_tx_start_s']:.9f} s Renode → "
                             f"{checked_alignment['mapped_gazebo_time_s']:.9f} s Gazebo; nearest IMU sample "
                             f"{checked_alignment['nearest_imu_sample_time_s']:.9f} s "
                             f"(absolute error {checked_alignment['absolute_error_s']*1000:.3f} ms).")
    elif mapping_error:
        clock_notice = ("Clock mapping was not applied: " + mapping_error +
                        ". Event lanes remain in source trace time.")
    else:
        clock_notice = ("No valid clock_mapping is recorded. Event lanes remain in source trace time; "
                        "the experiment manifest does not establish a Renode↔Gazebo clock mapping.")
    html_doc = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font:16px system-ui,sans-serif;margin:2rem auto;padding:0 1rem;max-width:1000px;color:#182230}}h1{{font-size:1.5rem}}h2{{margin-top:2rem}}.badge{{display:inline-block;background:#fff3cd;color:#664d03;padding:.25rem .6rem;border-radius:1rem;font-weight:650}}.meta,.muted,footer{{color:#475467}}svg{{display:block;width:100%;height:auto;border:1px solid #e4e7ec;border-radius:8px;margin:1rem 0}}svg text{{font:12px system-ui,sans-serif;fill:#475467}}.chart-title,.lane-title{{font-weight:650;fill:#182230}}.grid{{stroke:#e5e7eb}}.axis{{stroke:#667085}}.legend{{display:flex;gap:1.2rem}}.legend span:before{{content:' ';display:inline-block;width:.8rem;height:.15rem;background:var(--c);vertical-align:middle;margin-right:.4rem}}.notice{{padding:.75rem 1rem;background:#f2f4f7;border-radius:8px}}.event-list{{max-height:16rem;overflow:auto;padding-left:1.5rem;font-size:.9rem}}.camera{{display:flex;align-items:center;gap:.5rem}}select{{font:inherit;padding:.25rem}}
</style>
<h1>{html.escape(title)}</h1><p><span class="badge">SIMULATED · OFFLINE REPLAY · PROVENANCE SHOWN</span></p>
<p class="meta">Gazebo: {len(imu)} IMU samples, {len(recording)} timestamped pose samples · {imu[0][0]:.3f}–{imu[-1][0]:.3f} s</p>
<div class="notice">Gazebo pose and IMU share simulation-time records. {html.escape(clock_notice)} Logical anchor acceptance is not physical RF reception.</div>
<h2>Gazebo simulation-time signals</h2><div class="legend"><span style="--c:{AXIS_COLORS['x']}">X</span><span style="--c:{AXIS_COLORS['y']}">Y</span><span style="--c:{AXIS_COLORS['z']}">Z</span></div>
{imu_svg}
{''.join(pose_svg_parts)}
<h2>Recorded tag trajectory projection</h2><p class="meta">Projection of recorded tag_pose positions only; this is a 2D offline view, not live Gazebo animation.</p>
<label class="camera">Projection preset <select id="projection"><option value="xy">Top · X/Y</option><option value="xz">Side · X/Z</option><option value="yz">Front · Y/Z</option></select></label>
<svg id="trajectory" viewBox="0 0 960 420" role="img" aria-label="Offline ear-tag position projection"><rect width="100%" height="100%" fill="#fff"/><g id="trajectory-content"></g></svg>
<h2>{'Gazebo simulation-time event lanes' if mapping else 'Firmware-clock event lanes'}</h2><p class="meta">Start points are record timestamps; bars show intervals only where the source records both endpoints. Visible event details retain the source file and provenance class.</p>
{event_svg}{event_list}
<footer><p>Pose/IMU source files: <code>recording.jsonl</code> and <code>gazebo_imu.csv</code>. Event sources: <code>firmware_trace.jsonl</code>, <code>rf_events.jsonl</code>, <code>anchor_events.jsonl</code>, and <code>power/schedule.jsonl</code> when present. These are simulated records; current assumptions remain assumptions. No physical animal behavior, physical RF reception, or measured battery use is shown.</p></footer>
<script>
const samples={trajectory};
const NS='http://www.w3.org/2000/svg';
function drawProjection(mode){{
 const g=document.getElementById('trajectory-content');g.replaceChildren();
 const axes=mode==='xy'?['x','y']:mode==='xz'?['x','z']:['y','z'];
 const vals=samples.map(p=>[p[axes[0]],p[axes[1]]]);
 const minX=Math.min(...vals.map(p=>p[0])),maxX=Math.max(...vals.map(p=>p[0]));
 const minY=Math.min(...vals.map(p=>p[1])),maxY=Math.max(...vals.map(p=>p[1]));
 const dx=Math.max(maxX-minX,1e-9),dy=Math.max(maxY-minY,1e-9);
 const xy=vals.map(p=>[70+(p[0]-minX)/dx*820,350-(p[1]-minY)/dy*300]);
 const line=document.createElementNS(NS,'polyline');line.setAttribute('fill','none');line.setAttribute('stroke','#1570ef');line.setAttribute('stroke-width','2');line.setAttribute('points',xy.map(p=>p.join(',')).join(' '));g.append(line);
 for(const [idx,label] of [[0,'start'],[xy.length-1,'end']]){{const c=document.createElementNS(NS,'circle');c.setAttribute('cx',xy[idx][0]);c.setAttribute('cy',xy[idx][1]);c.setAttribute('r','5');c.setAttribute('fill',idx===0?'#12b76a':'#f04438');g.append(c);const t=document.createElementNS(NS,'text');t.setAttribute('x',xy[idx][0]+8);t.setAttribute('y',xy[idx][1]-8);t.textContent=label;g.append(t)}}
 const label=document.createElementNS(NS,'text');label.setAttribute('x','480');label.setAttribute('y','400');label.setAttribute('text-anchor','middle');label.textContent=axes[0].toUpperCase()+' position (m)';g.append(label);
 const vlabel=document.createElementNS(NS,'text');vlabel.setAttribute('x','24');vlabel.setAttribute('y','210');vlabel.setAttribute('transform','rotate(-90 24 210)');vlabel.setAttribute('text-anchor','middle');vlabel.textContent=axes[1].toUpperCase()+' position (m)';g.append(vlabel);
}}
document.getElementById('projection').addEventListener('change',e=>drawProjection(e.target.value));drawProjection('xy');
</script></html>"""
    target.write_text(html_doc, encoding="utf-8")
    return target


__all__ = ["create_viewer"]
