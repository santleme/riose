#!/usr/bin/env python3
"""MVP 2 trace-driven power analysis. All outputs are SIMULATED, never measured.

Input rows describe current-bearing intervals using timestamp_s, event, state,
component, duration_s, load_current_ma. Current is the rail load during that
interval. Overlapping component intervals add; unoccupied time uses the
explicit assumed idle_current_ma. CSV and JSONL are accepted.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DEFAULT_ASSUMPTIONS = HERE / "mvp2_power_assumptions.json"
REQUIRED = ("timestamp_s", "event", "state", "component", "duration_s", "load_current_ma")


def load_schedule(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text())
        rows = data["events"] if isinstance(data, dict) else data
    else:
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError("Schedule contains no intervals")
    normalized = []
    for index, source in enumerate(rows):
        missing = [key for key in REQUIRED if key not in source or source[key] == ""]
        if missing:
            raise ValueError(f"Row {index + 1} missing fields: {', '.join(missing)}")
        row = dict(source)
        for name in ("timestamp_s", "duration_s", "load_current_ma"):
            if isinstance(row[name], bool):
                raise ValueError(f"Row {index + 1}: {name} must be numeric, not boolean")
            try:
                row[name] = float(row[name])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Row {index + 1}: {name} must be numeric") from exc
            if not math.isfinite(row[name]):
                raise ValueError(f"Row {index + 1}: {name} must be finite")
        if row["timestamp_s"] < 0 or row["duration_s"] <= 0 or row["load_current_ma"] < 0:
            raise ValueError(f"Row {index + 1}: timestamps/current must be nonnegative and duration positive")
        row["event"] = str(row["event"])
        row["state"] = str(row["state"])
        row["component"] = str(row["component"])
        row["end_s"] = row["timestamp_s"] + row["duration_s"]
        if not math.isfinite(row["end_s"]):
            raise ValueError(f"Row {index + 1}: interval end must be finite")
        if "trace_status" in row and row["trace_status"] != "SIMULATED":
            raise ValueError(f"Row {index + 1}: trace status must be SIMULATED")
        if "current_status" in row and row["current_status"] not in {"ASSUMED", "DATASHEET", "SIMULATED"}:
            raise ValueError(f"Row {index + 1}: unsupported load evidence status")
        normalized.append(row)
    return sorted(normalized, key=lambda r: (r["timestamp_s"], r["end_s"], r["component"]))


def _value(assumptions: dict, key: str, override: float | None = None) -> float:
    value = override if override is not None else assumptions[key]["value"]
    if isinstance(value, bool):
        raise ValueError(f"{key} must be numeric, not boolean")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{key} must be numeric") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{key} must be finite")
    return numeric


def _trace_window(rows: list[dict]) -> tuple[float, float]:
    starts = {float(row["trace_window_start_s"]) for row in rows if "trace_window_start_s" in row}
    ends = {float(row["trace_window_end_s"]) for row in rows if "trace_window_end_s" in row}
    if len(starts) > 1 or len(ends) > 1:
        raise ValueError("schedule rows disagree on trace window")
    start = next(iter(starts), 0.0)
    end = next(iter(ends), max(float(row.get("end_s", row["timestamp_s"] + row["duration_s"]))
                               for row in rows))
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("trace window must be finite, nonnegative, and nonempty")
    if any(float(row["timestamp_s"]) < start or
           float(row.get("end_s", row["timestamp_s"] + row["duration_s"])) > end for row in rows):
        raise ValueError("schedule interval falls outside the declared trace window")
    return start, end


def _shared_trace_metadata(rows: list[dict], key: str) -> Any:
    values = [row[key] for row in rows if key in row]
    if values and (len(values) != len(rows) or any(value != values[0] for value in values[1:])):
        raise ValueError(f"schedule rows disagree on {key}")
    return values[0] if values else None


def _ideal_capacity_division(assumptions: dict, consumption: float | None,
                             consumption_provenance: dict | None) -> dict:
    """Return nominal-capacity arithmetic with strict provenance and no lifetime claim."""
    caveat = (
        "This is only mathematical division of nominal capacity by simulated consumption. "
        "It does not represent usable capacity, aging, temperature, discharge curve, cutoff, "
        "real efficiency, or predicted product autonomy."
    )
    result = {
        "status": "NOT_AVAILABLE_NO_VALID_INPUTS",
        "classification": "THEORETICAL_IDEAL_NOMINAL_ONLY",
        "nominal_capacity": None,
        "simulated_consumption": None,
        "formula": "nominal_capacity_mAh / simulated_consumption_mAh_per_day",
        "value_days": None,
        "caveat": caveat,
    }
    capacity = assumptions.get("nominal_capacity_mah")
    if capacity is None:
        result["status"] = "NOT_AVAILABLE_NO_NOMINAL_CAPACITY"
        return result
    if not isinstance(capacity, dict):
        result["status"] = "NOT_AVAILABLE_INVALID_NOMINAL_CAPACITY"
        return result
    if capacity.get("unit") != "mAh":
        result["status"] = "NOT_AVAILABLE_INVALID_NOMINAL_CAPACITY_UNIT"
        return result
    if (not isinstance(capacity.get("status"), str) or
            capacity["status"] not in {"DATASHEET", "ASSUMED", "SIMULATED"} or
            not isinstance(capacity.get("source"), str) or not capacity["source"].strip()):
        result["status"] = "NOT_AVAILABLE_MISSING_CAPACITY_PROVENANCE"
        return result
    try:
        capacity_mah = _value({"nominal_capacity_mah": capacity}, "nominal_capacity_mah")
    except (KeyError, ValueError):
        result["status"] = "NOT_AVAILABLE_INVALID_NOMINAL_CAPACITY"
        return result
    if capacity_mah <= 0:
        result["status"] = "NOT_AVAILABLE_INVALID_NOMINAL_CAPACITY"
        return result
    result["nominal_capacity"] = {"value": capacity_mah, "unit": "mAh",
                                  "status": capacity["status"], "source": capacity["source"]}
    if (isinstance(consumption, bool) or not isinstance(consumption, (int, float)) or
            not math.isfinite(consumption) or consumption <= 0):
        result["status"] = "NOT_AVAILABLE_INVALID_SIMULATED_CONSUMPTION"
        return result
    valid_consumption_provenance = (
        isinstance(consumption_provenance, dict) and
        consumption_provenance.get("unit") == "mAh/day" and
        consumption_provenance.get("value") == consumption and
        consumption_provenance.get("status") == "SIMULATED_EXTRAPOLATION_FROM_DECLARED_REPEAT_PERIOD" and
        isinstance(consumption_provenance.get("source"), str) and
        bool(consumption_provenance["source"].strip())
    )
    if not valid_consumption_provenance:
        result["status"] = "NOT_AVAILABLE_MISSING_CONSUMPTION_PROVENANCE"
        return result
    days = capacity_mah / consumption
    if not math.isfinite(days) or days <= 0:
        result["status"] = "NOT_AVAILABLE_INVALID_DIVISION_RESULT"
        return result
    result.update({
        "status": "THEORETICAL_IDEAL_CAPACITY_DIVISION",
        "simulated_consumption": consumption_provenance,
        "value_days": days,
    })
    return result


def _validated_period(rows: list[dict], period_s: float | None) -> float | None:
    if period_s is None:
        return None
    if isinstance(period_s, bool):
        raise ValueError("period_s must be numeric, not boolean")
    try:
        value = float(period_s)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("period_s must be numeric seconds") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("period_s must be finite and positive")
    if value < _trace_window(rows)[1]:
        raise ValueError("period_s must cover the full trace window")
    return value


def _has_valid_time_axis(points: list[tuple[float, ...]]) -> bool:
    """Accept ngspice's repeated timestamps at discontinuities, but not reversals."""
    return (len(points) >= 2
            and any(current[0] > previous[0] for previous, current in zip(points, points[1:]))
            and all(current[0] >= previous[0] for previous, current in zip(points, points[1:])))


def _validate_assumptions(assumptions: dict, overrides: dict[str, float] | None = None) -> None:
    overrides = overrides or {}
    values = {key: _value(assumptions, key, overrides.get(key)) for key in (
        "battery_voltage_v", "battery_esr_ohm", "regulator_output_v", "regulator_dropout_v",
        "regulator_efficiency", "regulator_quiescent_ma",
        "regulator_output_resistance_ohm", "output_capacitance_f",
        "idle_current_ma", "brownout_threshold_v", "pwl_edge_s")}
    expected_units = {
        "battery_voltage_v": "V", "battery_esr_ohm": "ohm",
        "regulator_output_v": "V", "regulator_dropout_v": "V", "regulator_efficiency": "fraction",
        "regulator_quiescent_ma": "mA", "regulator_output_resistance_ohm": "ohm",
        "output_capacitance_f": "F", "idle_current_ma": "mA",
        "brownout_threshold_v": "V", "pwl_edge_s": "s",
    }
    for key in values:
        record = assumptions.get(key)
        if (not isinstance(record, dict) or not isinstance(record.get("status"), str)
                or record["status"] not in {"DATASHEET", "ASSUMED", "SIMULATED"}
                or not isinstance(record.get("source"), str) or not record["source"].strip()):
            raise ValueError(f"{key} requires DATASHEET/ASSUMED/SIMULATED status and source provenance")
        if record.get("unit") != expected_units[key]:
            raise ValueError(f"{key} unit must be {expected_units[key]}")
    if values["battery_voltage_v"] <= 0 or values["regulator_output_v"] <= 0:
        raise ValueError("battery and regulator voltages must be positive")
    if values["regulator_dropout_v"] < 0:
        raise ValueError("regulator dropout voltage must be nonnegative")
    if values["battery_esr_ohm"] < 0 or values["regulator_output_resistance_ohm"] < 0:
        raise ValueError("resistances must be nonnegative")
    if not 0 < values["regulator_efficiency"] <= 1:
        raise ValueError("regulator_efficiency must be in (0, 1]")
    if values["regulator_quiescent_ma"] < 0 or values["idle_current_ma"] < 0:
        raise ValueError("quiescent and idle currents must be nonnegative")
    if values["output_capacitance_f"] <= 0 or values["brownout_threshold_v"] <= 0:
        raise ValueError("output capacitance and functional voltage limit must be positive")
    if values["pwl_edge_s"] <= 0:
        raise ValueError("pwl_edge_s must be positive")
    for key, value in overrides.items():
        try:
            finite = math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            finite = False
        if not finite:
            raise ValueError(f"{key} override must be finite")


def analyze_schedule(rows: list[dict], assumptions: dict, period_s: float | None = None,
                     period_source: str | None = None,
                     period_status: str | None = None,
                     period_unit: str = "s") -> dict:
    """Integrate intervals and produce event/component charge and a complete timeline."""
    idle = _value(assumptions, "idle_current_ma")
    window_start, window_end = _trace_window(rows)
    period_s = _validated_period(rows, period_s)
    trace_provenance = _shared_trace_metadata(rows, "trace_provenance")
    trace_event_coverage = _shared_trace_metadata(rows, "trace_event_coverage")
    idle_record = assumptions.get("idle_current_ma", {})
    idle_source = idle_record.get("source") if isinstance(idle_record, dict) else None
    idle_status = idle_record.get("status", "UNKNOWN") if isinstance(idle_record, dict) else "UNKNOWN"
    if period_s is not None:
        if period_unit != "s":
            raise ValueError("period_unit must be seconds ('s')")
        if (not isinstance(period_status, str) or
                period_status not in {"DATASHEET", "ASSUMED", "SIMULATED"} or
                not isinstance(period_source, str) or not period_source.strip()):
            raise ValueError("a repeat period requires allowed status and source provenance")
    elif period_source is not None or period_status is not None:
        raise ValueError("repeat period provenance cannot be supplied without period_s")
    total_end = period_s or window_end
    boundaries = sorted({window_start, total_end, *(r["timestamp_s"] for r in rows), *(r["end_s"] for r in rows)})
    timeline = []
    by_event = defaultdict(float)
    by_component = defaultdict(float)
    event_provenance: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"sources": set(), "duration_sources": set(), "statuses": set()})
    component_provenance: dict[str, set[str]] = defaultdict(set)
    charge_mah = 0.0
    for start, stop in zip(boundaries, boundaries[1:]):
        if stop <= start:
            continue
        active = [r for r in rows if r["timestamp_s"] < stop and r["end_s"] > start]
        current = idle + sum(r["load_current_ma"] for r in active)
        dt = stop - start
        amount_mah = current * dt / 3600.0
        charge_mah += amount_mah
        event_names = sorted({r["event"] for r in active}) or ["IDLE"]
        components = sorted({r["component"] for r in active})
        input_sources = sorted({str(r["provenance"]) for r in active if r.get("provenance")} |
                               ({str(idle_source)} if idle_source else set()))
        timeline.append({"timestamp_start_s": start, "timestamp_end_s": stop,
                         "duration_s": dt, "load_current_ma": current,
                         "active_events": ";".join(event_names),
                         "active_components": ";".join(components), "status": "SIMULATED",
                         "current_status": sorted({str(r.get("current_status", "UNKNOWN")) for r in active} |
                                                   ({idle_status} if idle_status else set())),
                         "input_provenance": input_sources})
        # Allocate idle baseline separately; overlapping loads are attributed to each source.
        idle_q = idle * dt / 3600.0
        by_event[("IDLE_BASELINE", "idle_baseline")] += idle_q
        by_component["idle_baseline"] += idle_q
        for r in active:
            q = r["load_current_ma"] * dt / 3600.0
            by_event[(r["event"], r["component"])] += q
            by_component[r["component"]] += q
            provenance = str(r.get("provenance", ""))
            if provenance:
                event_provenance[(r["event"], r["component"])]["sources"].add(provenance)
                component_provenance[r["component"]].add(provenance)
            duration_source = str(r.get("duration_source", ""))
            if duration_source:
                event_provenance[(r["event"], r["component"])]["duration_sources"].add(duration_source)
            event_provenance[(r["event"], r["component"])]["statuses"].add(
                str(r.get("current_status", "UNKNOWN")))
    event_rows = [{"event": event, "component": component,
                   "charge_uah": q * 1000, "status": "SIMULATED",
                   "current_status": sorted(event_provenance[(event, component)]["statuses"]),
                   "provenance": sorted(event_provenance[(event, component)]["sources"]),
                   "duration_provenance": sorted(event_provenance[(event, component)]["duration_sources"])}
                  for (event, component), q in sorted(by_event.items())]
    idle_key = ("IDLE_BASELINE", "idle_baseline")
    event_provenance[idle_key]["sources"].update({str(idle_source)} if idle_source else set())
    event_provenance[idle_key]["statuses"].add(str(idle_status))
    component_provenance["idle_baseline"].update({str(idle_source)} if idle_source else set())
    for row in event_rows:
        if row["event"] == idle_key[0] and row["component"] == idle_key[1]:
            row["current_status"] = [str(idle_status)]
            row["provenance"] = sorted({str(idle_source)} if idle_source else set())
    daily_period = period_s
    modeled_mah_day = charge_mah * 24 * 3600 / daily_period if daily_period else None
    daily_provenance = ({
        "value": modeled_mah_day,
        "unit": "mAh/day",
        "status": "SIMULATED_EXTRAPOLATION_FROM_DECLARED_REPEAT_PERIOD",
        "source": "simulated interval-load integration extrapolated using the declared repeat period",
        "repeat_period": {"value": period_s, "unit": period_unit,
                          "status": period_status, "source": period_source},
        "trace": trace_provenance,
        "load_sources": sorted({source for sources in component_provenance.values()
                                 for source in sources}),
    } if modeled_mah_day is not None else None)
    ideal_capacity = _ideal_capacity_division(assumptions, modeled_mah_day, daily_provenance)
    rail_v = (_value(assumptions, "regulator_output_v")
              if "regulator_output_v" in assumptions else None)
    energy_status = ("SIMULATED_AT_ASSUMED_REGULATOR_OUTPUT_VOLTAGE" if rail_v is not None
                     else "NOT_CALCULATED_NO_REGULATOR_OUTPUT_VOLTAGE")
    event_rows = [{**row,
                   "energy_mj": row["charge_uah"] * rail_v * 3.6 if rail_v is not None else None,
                   "energy_status": energy_status}
                  for row in event_rows]
    energy_by_event = defaultdict(float)
    for row in event_rows:
        if row["energy_mj"] is not None:
            energy_by_event[row["event"]] += row["energy_mj"]
    wake_energy_mj = sleep_energy_mj = 0.0
    for segment in timeline:
        energy_mj = (segment["load_current_ma"] * segment["duration_s"] * rail_v
                     if rail_v is not None else None)
        active_states = {r["state"] for r in rows
                         if r["timestamp_s"] < segment["timestamp_end_s"] and r["end_s"] > segment["timestamp_start_s"]}
        if energy_mj is None:
            continue
        if active_states == {"SLEEP"}:
            sleep_energy_mj += energy_mj
        elif active_states:
            wake_energy_mj += energy_mj
    return {"status": "SIMULATED", "input_status": "ASSUMED_OR_TRACE_DERIVED_NOT_MEASURED",
            "modeled_window_s": total_end, "trace_window_s": window_end - window_start,
            "period_s": period_s,
            "total_charge_mah_window": charge_mah,
            "mAh_per_day": modeled_mah_day,
            "mAh_per_day_period_s": daily_period,
            "mAh_per_day_provenance": daily_provenance,
            "repeat_period_provenance": ({"value": period_s, "unit": period_unit,
                                           "status": period_status, "source": period_source}
                                          if period_s is not None else None),
            "trace_provenance": trace_provenance,
            "trace_event_coverage": trace_event_coverage,
            "mAh_per_day_status": ("SIMULATED_EXTRAPOLATION_FROM_DECLARED_REPEAT_PERIOD" if period_s else
                                   "NOT_REPORTED_NO_REPEAT_PERIOD"),
            "ideal_capacity_division": ideal_capacity["value_days"],
            "ideal_capacity_division_status": ideal_capacity["status"],
            "ideal_capacity_division_details": ideal_capacity,
            "event_charge": event_rows,
            "component_charge_uah": {k: v * 1000 for k, v in sorted(by_component.items())},
            "component_provenance": {k: sorted(v) for k, v in sorted(component_provenance.items())},
            "energy_by_event_mj": dict(sorted(energy_by_event.items())),
            "energy_provenance": {"load_sources": sorted({source for entry in event_provenance.values()
                                                               for source in entry["sources"]}),
                                  "rail_voltage_assumption": assumptions.get("regulator_output_v")},
            "energy_tx_mj": energy_by_event.get("TX", energy_by_event.get("TX_START", 0.0)),
            "energy_wake_mj": wake_energy_mj if rail_v is not None else None,
            "energy_sleep_mj": sleep_energy_mj if rail_v is not None else None,
            "energy_status": energy_status,
            "model_provenance": {key: value for key, value in assumptions.items()
                                 if isinstance(value, dict) and "value" in value},
            "idle_current_ma_assumed": idle,
            "timeline": timeline}


def pwl_points(rows: list[dict], assumptions: dict, period_s: float | None = None) -> list[tuple[float, float]]:
    idle = _value(assumptions, "idle_current_ma")
    window_start, window_end = _trace_window(rows)
    period_s = _validated_period(rows, period_s)
    total_end = period_s if period_s is not None else window_end
    bounds = sorted({window_start, total_end, *(r["timestamp_s"] for r in rows), *(r["end_s"] for r in rows)})
    edge = _value(assumptions, "pwl_edge_s")
    def current_at(time_s: float) -> float:
        return idle + sum(r["load_current_ma"] for r in rows
                          if r["timestamp_s"] <= time_s < r["end_s"])
    points = [(bounds[0], current_at(bounds[0]) / 1000.0)]
    for time_s in bounds[1:]:
        before = current_at(math.nextafter(time_s, -math.inf)) / 1000.0
        after = current_at(time_s) / 1000.0
        # Preserve the preceding level to the boundary, then approximate the edge
        # over an explicit microsecond-scale interval (rather than ramping a whole event).
        points.append((time_s, before))
        next_boundary = next((b for b in bounds if b > time_s), total_end)
        transition_end = min(time_s + edge, time_s + (next_boundary - time_s) / 2, next_boundary)
        if transition_end > time_s:
            points.append((transition_end, after))
    if points[-1][0] < total_end:
        points.append((total_end, current_at(total_end) / 1000.0))
    # Ensure the final point is defined and the profile has nonzero duration.
    if len(points) == 1:
        points.append((points[0][0] + 1e-6, points[0][1]))
    return points


def generate_netlist(rows: list[dict], assumptions: dict, period_s: float | None = None,
                     overrides: dict[str, float] | None = None, data_path: str = "power_waveform.dat",
                     fault_profile: str | None = None) -> str:
    overrides = overrides or {}
    period_s = _validated_period(rows, period_s)
    _validate_assumptions(assumptions, overrides)
    vbat = _value(assumptions, "battery_voltage_v", overrides.get("battery_voltage_v"))
    esr = _value(assumptions, "battery_esr_ohm", overrides.get("battery_esr_ohm"))
    vreg = _value(assumptions, "regulator_output_v")
    efficiency = _value(assumptions, "regulator_efficiency", overrides.get("regulator_efficiency"))
    iq = _value(assumptions, "regulator_quiescent_ma") / 1000
    rout = _value(assumptions, "regulator_output_resistance_ohm")
    cap = _value(assumptions, "output_capacitance_f", overrides.get("output_capacitance_f"))
    brownout = _value(assumptions, "brownout_threshold_v")
    dropout = _value(assumptions, "regulator_dropout_v", overrides.get("regulator_dropout_v"))
    profiles = {"voltage_drop", "high_esr", "regulator_instability"}
    if fault_profile is not None and fault_profile not in profiles:
        raise ValueError(f"unsupported electrical fault profile: {fault_profile}")
    if fault_profile == "high_esr":
        esr = 40.0  # synthetic assumed stress injection, not cell characterization
    drop_v = 1.2 if fault_profile == "voltage_drop" else 0.0
    instability_v = 0.8 if fault_profile == "regulator_instability" else 0.0
    # The transient models the observed firmware window only. A declared repeat
    # period belongs to the analytical daily extrapolation, not a 900-second
    # electrical transient with no additional observed events.
    points = pwl_points(rows, assumptions)
    pwl = " ".join(f"{t:.12g} {i:.12g}" for t, i in points)
    max_time = points[-1][0]
    step = max(min(max_time / 10000, 1e-3), 1e-7)
    # Convert rail power to average battery current using assumed converter efficiency.
    ibat_points = " ".join(f"{t:.12g} {i*vreg/(vbat*efficiency)+iq:.12g}" for t, i in points)
    tx_rows = [r for r in rows if str(r.get("event", "")).upper().startswith("TX")]
    fault_start = max(1e-6, min((float(r["timestamp_s"]) for r in tx_rows), default=0.0))
    fault_end = max((float(r["end_s"]) for r in tx_rows), default=max_time)
    battery_source = (f"PWL(0 {vbat:.12g} {fault_start:.12g} {vbat:.12g} "
                      f"{fault_start+1e-6:.12g} {vbat-drop_v:.12g} "
                      f"{fault_end:.12g} {vbat-drop_v:.12g} {fault_end+1e-6:.12g} {vbat:.12g} "
                      f"{max_time:.12g} {vbat:.12g})") if drop_v else "DC {VBAT}"
    return f"""* RIOSE MVP2 power twin -- SIMULATED; all component/regulator/cell values ASSUMED.
* Causally coupled averaged rail/headroom model; not switching-regulator or physical validation.
.param VBAT={vbat:.12g} RBAT={esr:.12g} VREG={vreg:.12g} RREG={rout:.12g} COUT={cap:.12g}
.param BROWNOUT={brownout:.12g} EFF={efficiency:.12g}
Vcell cell_src 0 {battery_source}
Rcell cell_src battery {{RBAT}}
Ibattery battery 0 PWL({ibat_points})
* Synthetic profile={fault_profile or 'NONE'}; drop={drop_v:.6g} V, regulator perturbation={instability_v:.6g} V.
* The regulator holds VREG only while battery input has the assumed dropout headroom.
Breg reg_src 0 V={{min(VREG,max(0,v(battery)-{dropout:.12g}+{instability_v:.12g}*u(i(Iload)-0.01)*sin(2*pi*100*time)))}}
Rreg reg_src rail {{RREG}}
Cout rail 0 {{COUT}} IC={{VREG}}
Iload rail 0 PWL({pwl})
.control
set noaskquit
set wr_singlescale
tran {step:.12g} {max_time:.12g} 0 {step:.12g} uic
meas tran rail_min MIN v(rail)
meas tran rail_max MAX v(rail)
meas tran battery_min MIN v(battery)
meas tran battery_current_peak MIN i(Vcell)
wrdata {data_path} v(rail) v(battery) i(Vcell)
quit
.endc
.end
"""


def _run_ngspice(deck: Path, binary: str | None, rows: list[dict],
                 fault_profile: str | None = None) -> dict:
    waveform = deck.parent / "power_waveform.dat"
    electrical_csv = deck.parent / "electrical_trace.csv"
    waveform.unlink(missing_ok=True)
    electrical_csv.unlink(missing_ok=True)
    executable = binary or shutil.which("ngspice")
    if not executable:
        return {"status": "TOOL_UNAVAILABLE", "detail": "ngspice absent; netlist generated but not executed"}
    try:
        run = subprocess.run([executable, "-b", deck.name], cwd=deck.parent, capture_output=True,
                             text=True, check=False, timeout=1800)
    except (FileNotFoundError, PermissionError) as exc:
        (deck.parent / "ngspice.log").write_text(str(exc) + "\n")
        return {"status": "TOOL_UNAVAILABLE", "detail": f"ngspice executable unavailable: {exc}",
                "log": "ngspice.log", "trace": None}
    except (OSError, subprocess.TimeoutExpired) as exc:
        (deck.parent / "ngspice.log").write_text(str(exc) + "\n")
        return {"status": "SIMULATION_FAILED", "detail": f"ngspice could not complete: {exc}",
                "log": "ngspice.log", "trace": None}
    log = run.stdout + run.stderr
    (deck.parent / "ngspice.log").write_text(log)
    import re
    def measurement(name):
        found = re.search(rf"{name}\s*=\s*([-+0-9.eE]+)", log, re.I)
        return float(found.group(1)) if found else None
    status = "PASS" if run.returncode == 0 else "SIMULATION_FAILED"
    rail_min = measurement("rail_min")
    rail_max = measurement("rail_max")
    # Deck execution is also used independently of the repository path, so infer the
    # steady-state rail target from the netlist's VREG parameter.
    netlist_text = deck.read_text()
    vreg_match = re.search(r"VREG=([-+0-9.eE]+)", netlist_text)
    vreg = float(vreg_match.group(1)) if vreg_match else None
    recovery = None
    waveform_points = []
    validation_error = None
    try:
        if not waveform.is_file():
            raise ValueError("ngspice waveform output is missing")
        for line_number, line in enumerate(waveform.read_text().splitlines(), start=1):
            fields = line.split()
            if not fields:
                continue
            if len(fields) != 4:
                raise ValueError(f"waveform row {line_number} must have exactly four columns")
            values = [float(value) for value in fields[:4]]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("waveform contains a non-finite sample")
            waveform_points.append((values[0], values[1], values[2], values[3]))
        # ngspice preserves discontinuities as multiple samples at the same
        # timestamp. Those rows are meaningful (the load can change instantly),
        # so require nondecreasing time with at least two distinct timestamps.
        if not _has_valid_time_axis(waveform_points):
            raise ValueError("waveform has fewer than two increasing-time samples")
        tran_match = re.search(r"^\s*\.?(?:tran)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", netlist_text,
                               re.I | re.M)
        if not tran_match:
            raise ValueError("netlist is missing a valid transient stop time")
        step_s, expected_stop_s = (float(value) for value in tran_match.groups())
        if not math.isfinite(step_s) or step_s <= 0 or not math.isfinite(expected_stop_s) or expected_stop_s <= 0:
            raise ValueError("netlist transient interval is invalid")
        if waveform_points[0][0] > step_s * 1.01:
            raise ValueError("waveform does not cover the start of the transient")
        if waveform_points[-1][0] < expected_stop_s - step_s * 1.01:
            raise ValueError("waveform is truncated before the transient stop time")
        observed_min = min(point[1] for point in waveform_points)
        observed_max = max(point[1] for point in waveform_points)
        observed_battery_min = min(point[2] for point in waveform_points)
        measured_min = measurement("rail_min")
        measured_max = measurement("rail_max")
        tolerance = max(1e-6, abs(vreg or 1.0) * 1e-5)
        if (measured_min is not None and abs(measured_min - observed_min) > tolerance or
                measured_max is not None and abs(measured_max - observed_max) > tolerance):
            raise ValueError("waveform rail extrema disagree with ngspice measurements")
        measured_battery_min = measurement("battery_min")
        if measured_battery_min is not None and abs(measured_battery_min - observed_battery_min) > tolerance:
            raise ValueError("waveform battery extrema disagree with ngspice measurements")
        if vreg is not None:
            schedule_end = _trace_window(rows)[1]
        recovery = next((t - schedule_end for t, voltage, _, _ in waveform_points
                             if t >= schedule_end and voltage >= 0.99 * vreg), None)
    except (ValueError, OSError) as exc:
        validation_error = str(exc)
    required = {"rail_min": rail_min, "rail_max": rail_max,
                "battery_min": measurement("battery_min"),
                "battery_current_peak": measurement("battery_current_peak")}
    missing = [name for name, value in required.items()
               if value is None or not math.isfinite(value)]
    if (required["rail_min"] is not None and required["rail_max"] is not None and
            required["rail_min"] > required["rail_max"]):
        validation_error = validation_error or "rail_min exceeds rail_max"
    if required["battery_min"] is not None and required["battery_min"] <= 0:
        validation_error = validation_error or "battery voltage is nonpositive"
    convergence_failure = any(marker in log.lower() for marker in (
        "convergence failed", "timestep too small", "tran analysis failed", "singular matrix",
        "no convergence", "failed to converge"))
    if convergence_failure:
        status = "NON_CONVERGED"
    elif run.returncode == 0 and (missing or validation_error):
        status = "INVALID_OUTPUT"
    details = []
    if run.returncode != 0:
        details.append(f"ngspice exited {run.returncode}")
    if missing:
        details.append("missing/invalid measurements: " + ", ".join(missing))
    if validation_error:
        details.append(validation_error)
    if convergence_failure:
        details.append("ngspice reported a convergence/analysis failure")
    battery_min = required["battery_min"]
    battery_current_peak = (abs(required["battery_current_peak"])
                            if required["battery_current_peak"] is not None else None)
    if status != "PASS":
        rail_min = rail_max = battery_min = battery_current_peak = None
        recovery = None
    result = {"status": status, "return_code": run.returncode,
            "rail_min_v": rail_min, "rail_max_v": rail_max,
            "battery_min_v": battery_min,
            "battery_current_peak_a": battery_current_peak,
            "voltage_droop_v": (vreg - rail_min) if rail_min is not None and vreg is not None else None,
            "recovery_to_99pct_s": recovery,
            "recovery_status": ("RECOVERED_TO_99PCT" if recovery is not None else
                                "NOT_RECOVERED_WITHIN_SIMULATED_WINDOW" if status == "PASS" else
                                "NOT_AVAILABLE_WITHOUT_VALID_SIMULATION"),
            "functional_margin_v": None,
            "detail": "; ".join(details) if status != "PASS" else None,
            "log": "ngspice.log", "trace": "power_waveform.dat",
            "note": "Average regulator model; recovery is found from the raw waveform after the last schedule interval."}
    if status == "PASS":
        netlist_sha256 = hashlib.sha256(deck.read_bytes()).hexdigest()
        waveform_sha256 = hashlib.sha256(waveform.read_bytes()).hexdigest()
        simulation_id = hashlib.sha256((netlist_sha256 + waveform_sha256).encode()).hexdigest()
        write_csv(deck.parent / "electrical_trace.csv", [
            {"simulation_id": simulation_id, "netlist_sha256": netlist_sha256,
             "fault_profile": fault_profile or "NONE",
             "timestamp_s": t, "rail_voltage_v": voltage,
             "battery_terminal_voltage_v": battery_v,
             "battery_current_a": abs(current), "status": "SIMULATED"}
            for t, voltage, battery_v, current in waveform_points
        ])
        result["electrical_trace_csv"] = "electrical_trace.csv"
        result["simulation_id"] = simulation_id
        result["netlist_sha256"] = netlist_sha256
        result["waveform_sha256"] = waveform_sha256
    return result


def _temperature_parameters(assumptions: dict, temperature_c: float) -> tuple[dict, dict] | None:
    """Apply supplied linear sensitivities without inferring physical coefficients."""
    model = assumptions.get("temperature_model")
    if model is None:
        return None
    if not isinstance(model, dict) or model.get("kind") != "LINEAR_PARAMETER_SENSITIVITY":
        raise ValueError("temperature_model requires kind LINEAR_PARAMETER_SENSITIVITY")
    records = ("reference_temperature_c", "minimum_temperature_c", "maximum_temperature_c")
    temperatures = {}
    for key in records:
        record = model.get(key)
        if (not isinstance(record, dict) or record.get("unit") != "degC"
                or record.get("status") not in ("ASSUMED", "DATASHEET", "SIMULATED")
                or not isinstance(record.get("source"), str) or not record["source"].strip()):
            raise ValueError(f"temperature_model {key} requires degC and status/source provenance")
        temperatures[key] = _value(model, key)
    low, reference, high = (temperatures[key] for key in (
        "minimum_temperature_c", "reference_temperature_c", "maximum_temperature_c"))
    if low <= -273.15 or not low <= reference <= high or low == high:
        raise ValueError("temperature_model requires an ordered physical temperature range containing reference")
    if isinstance(temperature_c, bool) or not math.isfinite(temperature_c) or not low <= temperature_c <= high:
        raise ValueError("temperature scenario outside declared temperature_model range; extrapolation forbidden")
    coefficient_units = {"battery_esr_ohm": "ohm/degC", "regulator_efficiency": "fraction/degC",
                         "output_capacitance_f": "F/degC", "regulator_dropout_v": "V/degC"}
    coefficients = model.get("coefficients")
    if not isinstance(coefficients, dict) or not coefficients:
        raise ValueError("temperature_model requires nonempty coefficients")
    resolved = {}
    nonzero = False
    for key, record in coefficients.items():
        if key not in coefficient_units:
            raise ValueError(f"unsupported temperature coefficient: {key}")
        if (not isinstance(record, dict) or record.get("unit") != coefficient_units[key]
                or record.get("status") not in ("ASSUMED", "DATASHEET", "SIMULATED")
                or not isinstance(record.get("source"), str) or not record["source"].strip()):
            raise ValueError(f"temperature coefficient {key} requires unit and status/source provenance")
        coefficient = _value(coefficients, key)
        nonzero |= coefficient != 0
        resolved[key] = _value(assumptions, key) + coefficient * (temperature_c - reference)
    if not nonzero:
        raise ValueError("temperature_model requires a nonzero sensitivity; unchanged decks are not a model")
    _validate_assumptions(assumptions, resolved)
    provenance = {"kind": model["kind"], "status": "SIMULATED_PARAMETER_SENSITIVITY_NOT_CHARACTERIZED",
                  "temperature_c": temperature_c, "definition": model,
                  "formula": "parameter(T) = parameter(reference) + coefficient * (T - reference)",
                  "reference_parameters": {key: assumptions[key] for key in coefficients},
                  "resolved_parameters": resolved}
    return resolved, provenance


def parameter_sweep(rows: list[dict], assumptions: dict, period_s: float | None = None) -> list[dict]:
    """Generate deterministic sensitivity cases and their ngspice deck parameters."""
    axes = {
        "battery_voltage_v": [3.0, 3.3, 3.6],
        "battery_esr_ohm": [0.1, 0.25, 0.5],
        "regulator_efficiency": [0.7, 0.85, 0.95],
        "output_capacitance_f": [float(assumptions["output_capacitance_f"]["value"]) * x for x in (0.8, 1.0, 1.2)],
        "temperature_c_assumption": [-10.0, 25.0, 50.0],
        "tx_current_scale": [0.8, 1.0, 1.2],
    }
    # One-factor-at-a-time avoids an unmanageable Cartesian product while retaining named sensitivity axes.
    cases = [{"case": "BASELINE", "overrides": {}}]
    for key, values in axes.items():
        for value in values:
            cases.append({"case": f"{key}={value:g}", "overrides": {key: value}})
    output = []
    for case in cases:
        overrides = case["overrides"]
        adjusted = [dict(r) for r in rows]
        if "tx_current_scale" in overrides:
            scale = overrides["tx_current_scale"]
            adjusted = [dict(r, load_current_ma=r["load_current_ma"] * scale)
                        if str(r["component"]).lower() in ("radio", "sx1262", "tx") or "tx" in str(r["event"]).lower()
                        else r for r in adjusted]
        electrical = {k: v for k, v in overrides.items() if k in (
            "battery_voltage_v", "battery_esr_ohm", "regulator_efficiency", "output_capacitance_f")}
        override_provenance = {
            key: {"value": value, "status": "ASSUMED",
                  "source": f"MVP2 one-factor-at-a-time sensitivity axis: {key}"}
            for key, value in overrides.items()
        }
        temperature_only = "temperature_c_assumption" in overrides
        temperature_model = (_temperature_parameters(assumptions, overrides["temperature_c_assumption"])
                             if temperature_only else None)
        unmodeled = temperature_only and temperature_model is None
        if temperature_model:
            electrical, provenance = temperature_model
            override_provenance["temperature_c_assumption"]["unit"] = "degC"
        else:
            provenance = None
        netlist = None if unmodeled else generate_netlist(adjusted, assumptions, period_s, electrical)
        if provenance:
            netlist = ("* Declared temperature sensitivity: " + json.dumps(provenance, sort_keys=True)
                       + "\n" + netlist)
        output.append({"case": case["case"], "overrides": overrides,
                       "override_provenance": override_provenance,
                       "temperature_status": ("ASSUMED_SCENARIO_ONLY_NO_TEMPERATURE_MODEL" if unmodeled
                                              else "SIMULATED_WITH_DECLARED_PARAMETER_MODEL" if temperature_only else None),
                       "temperature_model": provenance,
                       "netlist": netlist,
                       "status": "NOT_MODELED_NO_TEMPERATURE_DEPENDENCY" if unmodeled else "PENDING_EXECUTION"})
    return output


def execute_parameter_sweep(rows: list[dict], assumptions: dict, output_dir: Path,
                            period_s: float | None = None, binary: str | None = None) -> dict:
    """Run every electrical sensitivity deck independently; never reuse artifacts."""
    cases = parameter_sweep(rows, assumptions, period_s)
    statuses = []
    for index, case in enumerate(cases):
        if case["netlist"] is None:
            statuses.append(case["status"])
            continue
        case_dir = output_dir / f"{index:02d}-{case['case'].replace('=', '-') }"
        case_dir.mkdir(parents=True, exist_ok=True)
        deck = case_dir / "power_trace.cir"
        deck.write_text(case["netlist"])
        result = _run_ngspice(deck, binary, rows)
        assumptions_limit = _value(assumptions, "brownout_threshold_v")
        if result["status"] == "PASS" and result["rail_min_v"] is not None:
            result["functional_margin_v"] = result["rail_min_v"] - assumptions_limit
            result["functional_margin_status"] = "SIMULATED_VS_ASSUMED_FUNCTIONAL_LIMIT"
        else:
            result["functional_margin_v"] = None
            result["functional_margin_status"] = "NOT_AVAILABLE_WITHOUT_VALID_SIMULATION"
        case["status"] = result["status"]
        case["ngspice"] = result
        case["netlist_path"] = str(deck.relative_to(output_dir))
        del case["netlist"]
        statuses.append(result["status"])
    unmodeled_axes = (["temperature_c_assumption"]
                      if "NOT_MODELED_NO_TEMPERATURE_DEPENDENCY" in statuses else [])
    modeled = [status for status in statuses if status != "NOT_MODELED_NO_TEMPERATURE_DEPENDENCY"]
    if modeled and all(status == "PASS" for status in modeled):
        status = "PASS_WITH_TEMPERATURE_AXIS_NOT_MODELED" if unmodeled_axes else "PASS"
    elif "TOOL_UNAVAILABLE" in modeled:
        status = "TOOL_UNAVAILABLE"
    elif "NON_CONVERGED" in modeled:
        status = "NON_CONVERGED"
    elif "INVALID_OUTPUT" in modeled:
        status = "INVALID_OUTPUT"
    elif "SIMULATION_FAILED" in modeled:
        status = "SIMULATION_FAILED"
    else:
        status = "FAILED"
    return {"status": status, "case_count": len(cases),
            "attempted_case_count": len(modeled),
            "executed_case_count": sum(case["status"] == "PASS" for case in cases),
            "not_modeled_axes": unmodeled_axes, "cases": cases}


def _ngspice_version(binary: str | None) -> str | None:
    executable = binary or shutil.which("ngspice")
    if not executable:
        return None
    try:
        version = subprocess.run([executable, "--version"], capture_output=True,
                                 text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = version.stdout + version.stderr
    for line in output.splitlines():
        if "ngspice-" in line.lower():
            return line.strip().lstrip("*").strip()
    return None


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def clear_run_outputs(output: Path) -> None:
    """Remove only this tool's generated outputs so a failed run cannot reuse old results."""
    names = ("summary.json", "power.csv", "event_energy.csv", "sweep.json",
             "power_trace.cir", "power_waveform.dat", "electrical_trace.csv", "ngspice.log")
    for name in names:
        (output / name).unlink(missing_ok=True)
    sweep_dir = output / "sweeps"
    if sweep_dir.is_dir():
        for case_dir in sweep_dir.iterdir():
            if case_dir.is_dir():
                for name in ("power_trace.cir", "power_waveform.dat", "electrical_trace.csv", "ngspice.log"):
                    (case_dir / name).unlink(missing_ok=True)


def _write_failure_summary(output: Path, status: str, detail: str) -> None:
    (output / "summary.json").write_text(json.dumps({
        "status": status, "result_class": "SIMULATED", "detail": detail,
        "electrical_metrics_status": "NOT_AVAILABLE",
    }, indent=2) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schedule", type=Path, help="CSV, JSON array/object, or JSONL interval schedule")
    parser.add_argument("--assumptions", type=Path, default=DEFAULT_ASSUMPTIONS)
    parser.add_argument("--period-s", help="Declared repeat period in seconds; enables simulated mAh/day extrapolation")
    parser.add_argument("--period-source", help="Provenance for the declared repeat period")
    parser.add_argument("--period-status", help="Evidence status for the declared repeat period")
    parser.add_argument("--period-unit", default="s", help="Repeat period unit (the trace API uses seconds)")
    parser.add_argument("--output", type=Path, default=Path("results/mvp2/power"))
    parser.add_argument("--ngspice", help="ngspice executable; auto-detected when omitted")
    parser.add_argument("--fault-profile", choices=("voltage_drop", "high_esr", "regulator_instability"),
                        help="synthetic ASSUMED electrical stress coupled into the battery-to-rail model")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    clear_run_outputs(args.output)
    try:
        period_s = None if args.period_s is None else float(args.period_s)
        if period_s is not None and (not args.period_source or not args.period_status):
            raise ValueError("--period-s requires --period-source and --period-status")
        if period_s is None and (args.period_source or args.period_status):
            raise ValueError("period provenance requires --period-s")
        rows = load_schedule(args.schedule)
        assumptions = json.loads(args.assumptions.read_text())
        _validate_assumptions(assumptions)
        if args.fault_profile is None and assumptions.get("temperature_model") is not None:
            for temperature_c in (-10.0, 25.0, 50.0):
                _temperature_parameters(assumptions, temperature_c)
        result = analyze_schedule(rows, assumptions, period_s, args.period_source,
                                  args.period_status, args.period_unit)
        deck = args.output / "power_trace.cir"
        deck.write_text(generate_netlist(rows, assumptions, period_s, fault_profile=args.fault_profile))
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        _write_failure_summary(args.output, "INVALID_INPUT", str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result["fault_profile"] = args.fault_profile
    result["fault_profile_provenance"] = ({
        "status": "ASSUMED",
        "source": "synthetic fault injection used to test causal rail feedback; not a component characterization",
        "voltage_drop_amplitude_v": 1.2 if args.fault_profile == "voltage_drop" else 0.0,
        "high_esr_value_ohm": 40.0 if args.fault_profile == "high_esr" else None,
        "regulator_perturbation_amplitude_v": 0.8 if args.fault_profile == "regulator_instability" else 0.0,
        "regulator_perturbation_frequency_hz": 100.0 if args.fault_profile == "regulator_instability" else None,
    } if args.fault_profile else None)
    result["ngspice"] = _run_ngspice(deck, args.ngspice, rows, args.fault_profile)
    result["ngspice"].update({
        "brownout_threshold_v_assumed": _value(assumptions, "brownout_threshold_v"),
        "brownout_threshold_provenance": assumptions["brownout_threshold_v"],
        "version": _ngspice_version(args.ngspice),
    })
    if result["ngspice"]["status"] == "PASS" and result["ngspice"]["rail_min_v"] is not None:
        result["ngspice"]["functional_margin_v"] = (
            result["ngspice"]["rail_min_v"] - result["ngspice"]["brownout_threshold_v_assumed"])
        result["ngspice"]["functional_margin_status"] = "SIMULATED_VS_ASSUMED_FUNCTIONAL_LIMIT"
    else:
        result["ngspice"]["functional_margin_status"] = "NOT_AVAILABLE_WITHOUT_VALID_SIMULATION"
    result["sweep"] = (execute_parameter_sweep(
        rows, assumptions, args.output / "sweeps", period_s, args.ngspice)
        if args.fault_profile is None else {
            "status": "NOT_RUN_FOR_FAULT_INTEGRATION", "case_count": 0,
            "attempted_case_count": 0, "executed_case_count": 0,
            "not_modeled_axes": [], "cases": []})
    for case in result["sweep"]["cases"]:
        if "ngspice" in case:
            case["ngspice"]["version"] = result["ngspice"]["version"]
    result["result_status"] = "SIMULATED"
    write_csv(args.output / "power.csv", result["timeline"])
    write_csv(args.output / "event_energy.csv", result["event_charge"])
    (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.output / "sweep.json").write_text(json.dumps(result["sweep"], indent=2) + "\n")
    print(f"status=SIMULATED ngspice={result['ngspice']['status']} window_s={result['modeled_window_s']:.6g}")
    print(f"charge_uah={result['total_charge_mah_window']*1000:.9g} mAh_per_day={result['mAh_per_day']}")
    print(f"outputs={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
