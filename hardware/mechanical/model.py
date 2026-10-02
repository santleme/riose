"""Generate an auditable mechanical envelope report from hardware/spec.yaml.

CadQuery is an optional export backend.  The geometry and fit report use only
the Python standard library and remain available when CadQuery is not installed.
All input dimensions retain their source/status and are rejected if marked
MEASURED: MVP 2 has no physical measurements.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - reported by CLI when spec is YAML
    yaml = None


class SpecError(ValueError):
    """The hardware specification is missing or invalid."""


@dataclass(frozen=True)
class Parameter:
    value: float
    unit: str
    source: str
    status: str


@dataclass(frozen=True)
class Box:
    name: str
    x: float
    y: float
    z: float
    width: float
    height: float
    thickness: float
    material: str
    source: str
    status: str

    def bounds(self) -> dict[str, tuple[float, float]]:
        return {
            "x": (self.x - self.width / 2, self.x + self.width / 2),
            "y": (self.y - self.height / 2, self.y + self.height / 2),
            "z": (self.z, self.z + self.thickness),
        }

    def volume_mm3(self) -> float:
        return self.width * self.height * self.thickness


def _node(spec: dict[str, Any], path: str) -> Any:
    current: Any = spec
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _parameter(spec: dict[str, Any], paths: list[str], *, name: str) -> Parameter:
    for path in paths:
        raw = _node(spec, path)
        if raw is None:
            continue
        if not isinstance(raw, dict) or not {"value", "unit", "source", "status"} <= raw.keys():
            raise SpecError(f"{path} must contain value, unit, source and status")
        try:
            value = float(raw["value"])
        except (TypeError, ValueError) as exc:
            raise SpecError(f"{path}.value must be numeric") from exc
        if not math.isfinite(value) or value <= 0:
            raise SpecError(f"{path}.value must be finite and positive")
        status = str(raw["status"]).upper()
        if status == "MEASURED":
            raise SpecError(f"{path} is MEASURED, which is forbidden in MVP 2")
        if status not in {"DATASHEET", "ASSUMED", "SIMULATED"}:
            raise SpecError(f"{path}.status must be DATASHEET, ASSUMED or SIMULATED")
        return Parameter(value, str(raw["unit"]), str(raw["source"]), status)
    raise SpecError(f"Missing required parameter {name}; expected one of: {', '.join(paths)}")


def _mm(parameter: Parameter, name: str) -> Parameter:
    factors = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4}
    unit = parameter.unit.strip().lower()
    if unit not in factors:
        raise SpecError(f"{name} must use a length unit (mm, cm, m, in); got {parameter.unit!r}")
    return Parameter(parameter.value * factors[unit], "mm", parameter.source, parameter.status)


def _get_mm(spec: dict[str, Any], path: str, *aliases: str) -> Parameter:
    return _mm(_parameter(spec, [path, *aliases], name=path), path)


def _coordinate_mm(spec: dict[str, Any], path: str, fallback: float) -> Parameter:
    """Read an optional assumed coordinate, allowing the meaningful value 0."""
    raw = _node(spec, path)
    if raw is None:
        return Parameter(fallback, "mm", "derived candidate placement", "ASSUMED")
    if not isinstance(raw, dict) or not {"value", "unit", "source", "status"} <= raw.keys():
        raise SpecError(f"{path} must contain value, unit, source and status")
    try:
        value = float(raw["value"])
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{path}.value must be numeric") from exc
    if not math.isfinite(value):
        raise SpecError(f"{path}.value must be finite")
    parameter = Parameter(value, str(raw["unit"]), str(raw["source"]), str(raw["status"]).upper())
    if parameter.status == "MEASURED":
        raise SpecError(f"{path} is MEASURED, which is forbidden in MVP 2")
    if parameter.status not in {"DATASHEET", "ASSUMED", "SIMULATED"}:
        raise SpecError(f"{path}.status must be DATASHEET, ASSUMED or SIMULATED")
    return _mm(parameter, path)


def _density(spec: dict[str, Any], key: str, fallback: float) -> tuple[float, str, str, str, list[str]]:
    raw = _node(spec, f"materials.{key}")
    if raw is None:
        label = key.removesuffix("_density_g_cm3")
        return fallback, "g/cm3", "generic engineering estimate; fallback", "ASSUMED", [f"{label} density uses an ASSUMED fallback"]
    p = _parameter(spec, [f"materials.{key}"], name=f"materials.{key}")
    if p.unit.lower() not in {"g/cm3", "g/cm^3", "g/cm³"}:
        raise SpecError(f"materials.{key} must use g/cm3")
    return p.value, p.unit, p.source, p.status, []


def load_spec(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SpecError(f"Hardware spec not found: {path}")
    if yaml is None:
        raise SpecError("PyYAML is required to read hardware/spec.yaml")
    with path.open(encoding="utf-8") as stream:
        spec = yaml.safe_load(stream)
    if not isinstance(spec, dict):
        raise SpecError("hardware/spec.yaml must contain a YAML mapping")
    return spec


def build_report(spec: dict[str, Any]) -> dict[str, Any]:
    """Return fit, envelope, mass and approximate center-of-mass analysis."""
    root = "mechanical"
    ew = _get_mm(spec, f"{root}.enclosure.width_mm", f"{root}.enclosure.width")
    eh = _get_mm(spec, f"{root}.enclosure.height_mm", f"{root}.enclosure.height")
    et = _get_mm(spec, f"{root}.enclosure.thickness_mm", f"{root}.enclosure.thickness")
    wall = _get_mm(spec, f"{root}.enclosure.wall_thickness_mm")
    pw = _get_mm(spec, f"{root}.pcb.width_mm", f"{root}.pcb.width")
    ph = _get_mm(spec, f"{root}.pcb.height_mm", f"{root}.pcb.height")
    pt = _get_mm(spec, f"{root}.pcb.thickness_mm", f"{root}.pcb.thickness")
    bd = _get_mm(spec, f"{root}.battery.diameter_mm", f"{root}.battery.width_mm")
    bl = _get_mm(spec, f"{root}.battery.length_mm", f"{root}.battery.height_mm")
    hole_d = _get_mm(spec, f"{root}.enclosure.mounting_hole_diameter_mm")
    aw = _get_mm(spec, f"{root}.antenna.width_mm")
    ah = _get_mm(spec, f"{root}.antenna.height_mm")
    at = _get_mm(spec, f"{root}.antenna.thickness_mm")
    ak = _get_mm(spec, f"{root}.antenna.keepout_mm")
    clearance_path = f"{root}.minimum_clearance_mm"
    clearance = (_get_mm(spec, clearance_path) if _node(spec, clearance_path) is not None
                 else Parameter(0.5, "mm", "generic assumed candidate clearance fallback", "ASSUMED"))
    hole_x = _coordinate_mm(spec, f"{root}.enclosure.mounting_hole_center_x_mm", 0.0)
    dims: dict[str, tuple[Parameter, Parameter, Parameter]] = {
        "mcu": tuple(_get_mm(spec, f"{root}.components.mcu.{axis}_mm") for axis in ("width", "height", "thickness")),
        "radio": tuple(_get_mm(spec, f"{root}.components.radio.{axis}_mm") for axis in ("width", "height", "thickness")),
        "imu": tuple(_get_mm(spec, f"{root}.components.imu.{axis}_mm") for axis in ("width", "height", "thickness")),
    }
    for label, p in (("wall thickness", wall),):
        if p.value * 2 >= min(ew.value, eh.value, et.value):
            raise SpecError(f"Enclosure {label} leaves no internal cavity")

    warnings: list[str] = [
        "Bounding-box placement and mass properties are estimates; validate clearances and retention in a reviewed design.",
        "Battery cylinder is approximated by a rectangular envelope with length along X.",
        "Part locations are a parameterized assumed candidate layout, not a reviewed mechanical drawing.",
        "Antenna keepout may extend beyond the PCB edge into empty cavity; mounted-package intersections remain fit blockers.",
        "Mounting hole is assumed near the upper end; validate its position and retention interface.",
    ]
    den_shell = _density(spec, "enclosure_density_g_cm3", 1.2)
    den_pcb = _density(spec, "pcb_density_g_cm3", 1.85)
    den_batt = _density(spec, "battery_density_g_cm3", 2.0)
    den_parts = _density(spec, "component_density_g_cm3", 2.0)
    for density in (den_shell, den_pcb, den_batt, den_parts):
        warnings.extend(density[4])

    # Body represented as a hollow rectangular shell. Other parts are simple
    # envelopes; PCB-mounted component mass is not subtracted from the PCB.
    inner_w, inner_h, inner_t = ew.value - 2 * wall.value, eh.value - 2 * wall.value, et.value - 2 * wall.value
    # The explicitly assumed candidate packs the PCB above the cell in Y;
    # the cell's long axis is X. Optional coordinates support small fixture
    # specs while the product spec records the reviewed candidate positions.
    default_pcb_top = inner_h / 2 - hole_d.value - clearance.value
    pcb_x = _coordinate_mm(spec, f"{root}.layout_candidate.pcb_center_x_mm", 0.0)
    pcb_y = _coordinate_mm(spec, f"{root}.layout_candidate.pcb_center_y_mm", default_pcb_top - ph.value / 2)
    battery_x = _coordinate_mm(spec, f"{root}.layout_candidate.battery_center_x_mm", 0.0)
    battery_y = _coordinate_mm(spec, f"{root}.layout_candidate.battery_center_y_mm", -inner_h / 2 + bd.value / 2 + clearance.value)
    boxes: list[Box] = [
        Box("enclosure", 0, 0, 0, ew.value, eh.value, et.value, "enclosure", ew.source, ew.status),
        Box("pcb", pcb_x.value, pcb_y.value, wall.value, pw.value, ph.value, pt.value, "pcb", pw.source, pw.status),
        Box("battery", battery_x.value, battery_y.value, wall.value, bl.value, bd.value, bd.value, "battery", bl.source, bl.status),
    ]
    hole_y = eh.value / 2 - wall.value - hole_d.value / 2

    board = boxes[1]
    cursor_x = board.x - pw.value / 2
    cursor_y = board.y - ph.value / 2
    row_height = 0.0
    gap = clearance.value
    warnings.append(f"Component placement clearance of {gap:g} mm is an ASSUMED layout rule.")
    for name in ("mcu", "radio", "imu"):
        a, b, c = dims[name]
        if cursor_x + a.value > board.x + pw.value / 2:
            cursor_x = board.x - pw.value / 2
            cursor_y += row_height + gap
            row_height = 0.0
        center_x, center_y = cursor_x + a.value / 2, cursor_y + b.value / 2
        z = wall.value + pt.value
        boxes.append(Box(name, center_x, center_y, z, a.value, b.value, c.value, "component", a.source, a.status))
        cursor_x += a.value + gap
        row_height = max(row_height, b.value)
    antenna = Box("antenna", board.x, board.y + ph.value / 2 - ah.value / 2 - ak.value, wall.value + pt.value,
                  aw.value, ah.value, at.value, "antenna", aw.source, aw.status)
    antenna_keepout = Box("antenna_keepout", antenna.x, antenna.y, antenna.z,
                          aw.value + 2 * ak.value, ah.value + 2 * ak.value,
                          at.value + 2 * ak.value, "keepout", ak.source, ak.status)
    boxes.extend((antenna, antenna_keepout))

    def inside(box: Box) -> bool:
        bounds = box.bounds()
        return (bounds["x"][0] >= -inner_w / 2 - 1e-9 and bounds["x"][1] <= inner_w / 2 + 1e-9
                and bounds["y"][0] >= -inner_h / 2 - 1e-9 and bounds["y"][1] <= inner_h / 2 + 1e-9
                and bounds["z"][0] >= wall.value - 1e-9 and bounds["z"][1] <= et.value - wall.value + 1e-9)

    issues: list[str] = []
    if (hole_d.value <= 0
            or abs(hole_x.value) + hole_d.value / 2 > ew.value / 2 + 1e-9
            or hole_y + hole_d.value / 2 > eh.value / 2 + 1e-9):
        issues.append("mounting hole is outside the enclosure envelope")
    # The cylindrical mounting hole is cut through the complete shell. Check
    # projected XY clearance to every physical package, including the PCB.
    hole_radius_with_clearance = hole_d.value / 2 + clearance.value
    for box in boxes[1:]:
        if box.name == "antenna_keepout":
            continue
        bounds = box.bounds()
        closest_x = min(max(hole_x.value, bounds["x"][0]), bounds["x"][1])
        closest_y = min(max(hole_y, bounds["y"][0]), bounds["y"][1])
        distance = math.hypot(hole_x.value - closest_x, hole_y - closest_y)
        if distance < hole_radius_with_clearance - 1e-9:
            issues.append(f"mounting hole violates clearance to {box.name} envelope")
    if pw.value > inner_w or ph.value > inner_h or pt.value > inner_t:
        issues.append("PCB envelope exceeds enclosure cavity")
    for box in boxes[1:]:
        if not inside(box):
            issues.append(f"{box.name} envelope exceeds enclosure cavity")
    for box in boxes[3:6]:
        b = box.bounds()
        if (b["x"][0] < board.x - board.width / 2 - 1e-9 or b["x"][1] > board.x + board.width / 2 + 1e-9
                or b["y"][0] < board.y - board.height / 2 - 1e-9 or b["y"][1] > board.y + board.height / 2 + 1e-9):
            issues.append(f"{box.name} package exceeds PCB envelope")
    ab = antenna.bounds()
    if (ab["x"][0] < board.x - board.width / 2 - 1e-9 or ab["x"][1] > board.x + board.width / 2 + 1e-9
            or ab["y"][0] < board.y - board.height / 2 - 1e-9 or ab["y"][1] > board.y + board.height / 2 + 1e-9):
        issues.append("antenna footprint exceeds PCB envelope")
    for box in boxes[3:6]:
        if _overlap(box, antenna_keepout):
            issues.append(f"{box.name} package violates antenna keepout")
    # Check the only separately packed pair using the declared layout rule;
    # a positive gap is required even when their boxes do not intersect.
    pcb_bounds, battery_bounds = board.bounds(), boxes[2].bounds()
    dx = max(0.0, pcb_bounds["x"][0] - battery_bounds["x"][1], battery_bounds["x"][0] - pcb_bounds["x"][1])
    dy = max(0.0, pcb_bounds["y"][0] - battery_bounds["y"][1], battery_bounds["y"][0] - pcb_bounds["y"][1])
    if math.hypot(dx, dy) < clearance.value - 1e-9:
        issues.append("battery violates minimum clearance to pcb envelope")
    # Check solid part overlaps, with PCB/component contact intentionally allowed.
    physical = [box for box in boxes if box.name not in {"enclosure", "antenna_keepout"}]
    for i, first in enumerate(physical):
        for second in physical[i + 1:]:
            if _overlap(first, second):
                issues.append(f"unexpected envelope overlap: {first.name} / {second.name}")
    if not (ew.value > 0 and eh.value > 0 and et.value > 0):
        issues.append("invalid external envelope")

    densities = {"enclosure": den_shell[0], "pcb": den_pcb[0], "battery": den_batt[0], "component": den_parts[0]}
    # Shell volume is outer minus inner cavity. Components contribute their full
    # box volumes; this deliberately overestimates solid component packaging.
    volumes = {
        "enclosure": (ew.value * eh.value * et.value - inner_w * inner_h * inner_t),
        "pcb": boxes[1].volume_mm3(),
        "battery": math.pi * (bd.value / 2) ** 2 * bl.value,
    }
    for part in (*boxes[3:6], antenna):
        volumes["components"] = volumes.get("components", 0.0) + part.volume_mm3()
    masses = {
        "enclosure": volumes["enclosure"] * densities["enclosure"] / 1000,
        "pcb": volumes["pcb"] * densities["pcb"] / 1000,
        "battery": volumes["battery"] * densities["battery"] / 1000,
        "components": volumes.get("components", 0.0) * densities["component"] / 1000,
    }
    mass_total = sum(masses.values())
    # Approximate body and cylinder centroids; remaining parts use bbox centers.
    weighted = [(masses["enclosure"], 0, 0, et.value / 2),
                (masses["pcb"], board.x, board.y, board.z + pt.value / 2),
                (masses["battery"], boxes[2].x, boxes[2].y, boxes[2].z + bd.value / 2)]
    for box in (*boxes[3:6], antenna):
        m = box.volume_mm3() * densities["component"] / 1000
        weighted.append((m, box.x, box.y, box.z + box.thickness / 2))
    cg = {axis: sum(m * coords[i] for m, *coords in weighted) / mass_total for i, axis in enumerate(("x_mm", "y_mm", "z_mm"))}

    parameter_inputs = {
        "mechanical.enclosure.width_mm": ew, "mechanical.enclosure.height_mm": eh,
        "mechanical.enclosure.thickness_mm": et, "mechanical.enclosure.wall_thickness_mm": wall,
        "mechanical.enclosure.mounting_hole_diameter_mm": hole_d,
        "mechanical.enclosure.mounting_hole_center_x_mm": hole_x,
        "mechanical.pcb.width_mm": pw, "mechanical.pcb.height_mm": ph, "mechanical.pcb.thickness_mm": pt,
        "mechanical.battery.diameter_mm": bd, "mechanical.battery.length_mm": bl,
        "mechanical.minimum_clearance_mm": clearance,
        "mechanical.antenna.width_mm": aw, "mechanical.antenna.height_mm": ah,
        "mechanical.antenna.thickness_mm": at, "mechanical.antenna.keepout_mm": ak,
        "mechanical.layout_candidate.pcb_center_x_mm": pcb_x,
        "mechanical.layout_candidate.pcb_center_y_mm": pcb_y,
        "mechanical.layout_candidate.battery_center_x_mm": battery_x,
        "mechanical.layout_candidate.battery_center_y_mm": battery_y,
    }
    for name, group in dims.items():
        for axis, parameter in zip(("width_mm", "height_mm", "thickness_mm"), group):
            parameter_inputs[f"mechanical.components.{name}.{axis}"] = parameter
    statuses = {p.status for p in parameter_inputs.values()}
    if "MEASURED" in statuses:
        raise SpecError("MVP 2 mechanical inputs cannot have status MEASURED")
    return {
        "schema_version": "1.0",
        "analysis": "SIMULATED_GEOMETRY_ESTIMATE",
        "gate": "NOT_READY_FOR_PHYSICAL_PROTOTYPE" if issues else "CONDITIONALLY_READY_PENDING_THRESHOLD_APPROVAL",
        "gate_note": "Mechanical dimensions, antenna and fit require explicit review before READY_FOR_PHYSICAL_PROTOTYPE.",
        "specification_statuses": sorted(statuses),
        "input_parameters": {name: asdict(parameter) for name, parameter in parameter_inputs.items()},
        "envelope_mm": {"width": ew.value, "height": eh.value, "thickness": et.value},
        "internal_cavity_mm": {"width": inner_w, "height": inner_h, "thickness": inner_t},
        "parts": [asdict(box) | {"volume_mm3": box.volume_mm3(), "bounds_mm": {k: list(v) for k, v in box.bounds().items()}} for box in boxes],
        "fit": {"fits": not issues, "issues": issues},
        "volume_mm3": volumes,
        "mass_estimate_g": {"parts": masses, "total": mass_total, "status": "ASSUMED"},
        "center_of_mass_mm": cg,
        "materials": {
            "enclosure": {"density_g_cm3": den_shell[0], "source": den_shell[2], "status": den_shell[3]},
            "pcb": {"density_g_cm3": den_pcb[0], "source": den_pcb[2], "status": den_pcb[3]},
            "battery": {"density_g_cm3": den_batt[0], "source": den_batt[2], "status": den_batt[3]},
            "component": {"density_g_cm3": den_parts[0], "source": den_parts[2], "status": den_parts[3]},
        },
        "mounting_hole": {"diameter_mm": hole_d.value, "center_mm": {"x": hole_x.value, "y": hole_y}, "axis": "z", "source": hole_x.source, "status": hole_x.status},
        "warnings": sorted(set(warnings)),
        "cadquery_available": _cadquery_available(),
    }


def _overlap(a: Box, b: Box) -> bool:
    ab, bb = a.bounds(), b.bounds()
    return all(min(ab[axis][1], bb[axis][1]) - max(ab[axis][0], bb[axis][0]) > 1e-9 for axis in ("x", "y", "z"))


def _cadquery_available() -> bool:
    try:
        import cadquery  # noqa: F401
        return True
    except ImportError:
        return False


def export_cad(report: dict[str, Any], step_output: Path, stl_output: Path | None = None) -> None:
    try:
        import cadquery as cq
    except ImportError as exc:
        raise SpecError("CadQuery unavailable; install the optional CadQuery runtime to export STEP") from exc
    assembly = cq.Assembly(name="riose_ear_tag_assumed")
    shapes = []
    for part in report["parts"]:
        if part["name"] in {"enclosure", "antenna_keepout"}:
            continue
        shape = cq.Workplane("XY").box(part["width"], part["height"], part["thickness"], centered=(True, True, False))
        shape = shape.translate((part["x"], part["y"], part["z"]))
        assembly.add(shape, name=part["name"])
        shapes.append(shape.val())
    # Hollow open-top envelope keeps the first export simple and editable.
    w, h, t = (report["envelope_mm"][key] for key in ("width", "height", "thickness"))
    cavity = report["internal_cavity_mm"]
    # CadQuery shell/export is generated as a difference of outer/inner boxes.
    outer = cq.Workplane("XY").box(w, h, t, centered=(True, True, False))
    inner = cq.Workplane("XY").box(cavity["width"], cavity["height"], cavity["thickness"], centered=(True, True, False)).translate((0, 0, (t-cavity["thickness"])/2))
    hole = report["mounting_hole"]
    cutter = cq.Workplane("XY").circle(hole["diameter_mm"] / 2).extrude(t + 2).translate((hole["center_mm"]["x"], hole["center_mm"]["y"], -1))
    shell = outer.cut(inner).cut(cutter)
    assembly.add(shell, name="enclosure_shell")
    shapes.append(shell.val())
    step_output.parent.mkdir(parents=True, exist_ok=True)
    cq.exporters.export(cq.Compound.makeCompound(shapes), str(step_output), exportType="STEP")
    if stl_output is not None:
        stl_output.parent.mkdir(parents=True, exist_ok=True)
        cq.exporters.export(cq.Compound.makeCompound(shapes), str(stl_output), exportType="STL")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=Path("hardware/spec.yaml"))
    parser.add_argument("--output", type=Path, default=Path("results/mvp2/mechanical/geometry.json"))
    parser.add_argument("--step", type=Path, help="Optional STEP export (requires CadQuery)")
    parser.add_argument("--stl", type=Path, help="Optional STL export; with --step both formats use the same shapes")
    args = parser.parse_args(argv)
    try:
        report = build_report(load_spec(args.spec))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if args.step or args.stl:
            step_path = args.step or args.stl.with_suffix(".step")
            stl_path = args.stl or step_path.with_suffix(".stl")
            export_cad(report, step_path, stl_path)
    except (SpecError, OSError) as exc:
        print(f"mechanical model error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"report": str(args.output), "gate": report["gate"], "fits": report["fit"]["fits"], "cadquery_available": report["cadquery_available"]}))
    # A completed analysis is success even when it reports a fit failure. The
    # report is the machine-readable result; only invalid inputs/tools return 2.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
