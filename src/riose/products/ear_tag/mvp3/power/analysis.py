"""Run the existing MVP2 trace-to-load and power pipeline for an MVP3 trace.

Firmware trace timestamps and event boundaries are inputs from simulation.
All current values and unresolved event durations remain ASSUMED, and the
resulting charge integration is SIMULATED. ngspice status is reported
separately because it is an optional electrical-model execution.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ...digital_twin.paths import DEFAULT_SPEC, ROOT, resolve_user_path
from ...digital_twin.power import _power_assumptions, _power_load_profile
from ...digital_twin.spec import dump_json, load_spec


class PowerAnalysisError(RuntimeError):
    """An input or MVP2 analysis step failed; no successful summary was made."""


def run_power_analysis(firmware_trace: Path, output_dir: Path, *,
                       spec_path: Path | None = None,
                       ngspice: str | None = None,
                       trace_duration_s: float | None = None) -> dict[str, Any]:
    """Analyze a `riose.firmware.trace/v1` JSONL file and write MVP3 artifacts.

    Artifacts are `assumed_load_profile.json`, `assumptions.json`,
    `schedule.jsonl`, native MVP2 outputs, and `power_summary.json` in
    ``output_dir``. A trace without compatible, explicitly handled events fails
    closed through the existing MVP2 adapter.

    This function does not infer activity from Gazebo motion or create firmware
    events. The trace must come from a real/simulated firmware execution.
    """
    trace_path = resolve_user_path(Path(firmware_trace))
    out = resolve_user_path(Path(output_dir))
    canonical_spec = resolve_user_path(Path(spec_path)) if spec_path else DEFAULT_SPEC.resolve()
    if not trace_path.is_file():
        raise PowerAnalysisError(f"firmware trace does not exist: {trace_path}")
    if not (ROOT / "hardware" / "spice" / "trace_adapter.py").is_file():
        raise PowerAnalysisError("MVP2 trace adapter is missing from this checkout")
    if not (ROOT / "hardware" / "spice" / "mvp2_power.py").is_file():
        raise PowerAnalysisError("MVP2 power analyzer is missing from this checkout")

    spec, spec_sha256 = load_spec(canonical_spec)
    out.mkdir(parents=True, exist_ok=True)
    loads_path = out / "assumed_load_profile.json"
    assumptions_path = out / "assumptions.json"
    schedule_path = out / "schedule.jsonl"
    native_out = out / "mvp2"
    dump_json(loads_path, _power_load_profile(spec))
    dump_json(assumptions_path, _power_assumptions(spec))

    adapter_cmd = [sys.executable, str(ROOT / "hardware/spice/trace_adapter.py"),
                   str(trace_path), "--loads", str(loads_path), "--output", str(schedule_path)]
    if trace_duration_s is not None:
        adapter_cmd.extend(["--trace-duration-s", str(trace_duration_s)])
    adapted = subprocess.run(adapter_cmd, cwd=ROOT, capture_output=True, text=True)
    if adapted.returncode != 0 or not schedule_path.is_file():
        raise PowerAnalysisError(
            "MVP2 trace adapter failed "
            f"(exit {adapted.returncode}): {(adapted.stderr or adapted.stdout).strip()}"
        )

    power_cmd = [sys.executable, str(ROOT / "hardware/spice/mvp2_power.py"),
                 str(schedule_path), "--assumptions", str(assumptions_path),
                 "--output", str(native_out)]
    if ngspice:
        power_cmd.extend(["--ngspice", ngspice])
    analyzed = subprocess.run(power_cmd, cwd=ROOT, capture_output=True, text=True)
    native_summary_path = native_out / "summary.json"
    if analyzed.returncode != 0 or not native_summary_path.is_file():
        detail = (analyzed.stderr or analyzed.stdout).strip()
        raise PowerAnalysisError(
            f"MVP2 power analysis failed (exit {analyzed.returncode}): {detail}"
        )
    native = json.loads(native_summary_path.read_text(encoding="utf-8"))

    # Keep a compact integration artifact while preserving all detailed native
    # output beneath mvp2/. Do not imply measured current, a real battery life,
    # or circuit validation from successful schedule integration.
    summary: dict[str, Any] = {
        "schema_version": "riose.mvp3.power_summary/v1",
        "status": "COMPLETED",
        "result_class": "SIMULATED",
        "trace": {
            "path": str(trace_path),
            "status": "SIMULATED",
            "schema_version": "riose.firmware.trace/v1",
            "trace_provenance": native.get("trace_provenance"),
        },
        "assumptions": {
            "status": "ASSUMED",
            "spec_path": str(canonical_spec),
            "spec_sha256": spec_sha256,
            "load_profile": str(loads_path),
            "load_profile_status": "ASSUMED",
            "current_measurements_available": False,
            "event_duration_policy": "trace intervals when resolvable; explicit ASSUMED fallback only where the MVP2 adapter requires it",
        },
        "modeled_window_s": native.get("modeled_window_s"),
        "modeled_charge_uah": (native.get("total_charge_mah_window") * 1000
                                if isinstance(native.get("total_charge_mah_window"), (int, float)) else None),
        "charge_status": native.get("energy_status", "SIMULATED"),
        "energy_by_event_mj": native.get("energy_by_event_mj", {}),
        "ngspice": native.get("ngspice", {"status": "NOT_REPORTED"}),
        "electrical_metrics_status": native.get("ngspice", {}).get(
            "status", "NOT_REPORTED"),
        "native_summary": str(native_summary_path),
        "outputs": {"schedule": str(schedule_path), "native_power_dir": str(native_out)},
        "limitations": [
            "Firmware events do not provide current measurements; load currents come from ASSUMED/DATASHEET values in hardware/spec.yaml.",
            "The estimate covers only the declared firmware trace window; no daily repeat period or battery lifetime is inferred.",
            "An ngspice PASS is a result from the assumed equivalent-circuit model, not validation of physical hardware.",
        ],
    }
    dump_json(out / "power_summary.json", summary)
    return summary
