"""Simulation-only openEMS adapter for the explicitly assumed MVP2 candidate.

This module intentionally models a reproducible toy geometry, not a released
or physically characterized ear tag.  The scenario names describe isolated
parametric sensitivity cases.  In particular, the PCB, candidate cell and
enclosure do not currently fit together as one mechanically verified assembly.
Every successful result is numerical simulation evidence for these assumptions
only; it is not antenna design validation or measurement evidence.
"""
from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import numpy as np

SOLVER_RESOLUTIONS_MM = (4.0, 2.0)
S11_CONVERGENCE_DB = 1.0
RESONANCE_CONVERGENCE_FRACTION = 0.02
FREQUENCY_POINTS = 101
FREQUENCY_MIN_RATIO = 0.2
FREQUENCY_MAX_RATIO = 2.5
AIR_MARGIN_MM = 48.0
FDTD_END_CRITERIA = 1e-4
FDTD_END_CRITERIA_DB = -40.0
# Planning estimate for field, update-coefficient, material and boundary arrays.
# This is intentionally conservative, but is not a hard bound on native RSS.
ESTIMATED_BYTES_PER_CELL = 128
DEFAULT_MAX_ESTIMATED_MEMORY_MIB = 128.0
MAX_ESTIMATED_MEMORY_ENV = "RIOSE_OPENEMS_MAX_ESTIMATED_MEMORY_MIB"


class MeshBudgetExceeded(RuntimeError):
    """Raised before openEMS field allocation when a mesh exceeds its budget."""


def _mesh_budget(axis_cells: dict[str, int], *, limit_mib: float | None = None) -> dict[str, Any]:
    """Estimate mesh memory and fail closed before ``fdtd.Run`` if over budget.

    The estimate is a planning guard, not a process-memory guarantee. The cap
    defaults to 128 MiB and can be overridden in MiB with
    ``RIOSE_OPENEMS_MAX_ESTIMATED_MEMORY_MIB``.
    """
    if set(axis_cells) != {"x", "y", "z"}:
        raise ValueError("mesh budget requires x, y, and z cell counts")
    counts: dict[str, int] = {}
    for axis, value in axis_cells.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"mesh cell count for {axis} must be a positive integer")
        counts[axis] = value

    cells = math.prod(counts.values())
    estimate_bytes = cells * ESTIMATED_BYTES_PER_CELL
    estimate_mib = estimate_bytes / (1024 ** 2)
    if limit_mib is None:
        raw_limit = os.environ.get(MAX_ESTIMATED_MEMORY_ENV)
        try:
            limit_mib = (DEFAULT_MAX_ESTIMATED_MEMORY_MIB if raw_limit is None
                         else float(raw_limit))
        except ValueError as exc:
            raise ValueError(f"{MAX_ESTIMATED_MEMORY_ENV} must be a positive number of MiB") from exc
    if isinstance(limit_mib, bool) or not math.isfinite(float(limit_mib)) or float(limit_mib) <= 0:
        raise ValueError(f"{MAX_ESTIMATED_MEMORY_ENV} must be a positive number of MiB")
    limit_mib = float(limit_mib)

    budget = {
        **counts,
        "cells": cells,
        "estimated_memory_bytes": estimate_bytes,
        "estimated_memory_mib": estimate_mib,
        "estimated_bytes_per_cell": ESTIMATED_BYTES_PER_CELL,
        "max_estimated_memory_mib": limit_mib,
        "memory_estimate_is_hard_rss_limit": False,
    }
    if estimate_mib > limit_mib:
        raise MeshBudgetExceeded(
            "mesh budget rejected before openEMS field allocation: "
            f"{cells:,} cells estimate {estimate_mib:.1f} MiB "
            f"({ESTIMATED_BYTES_PER_CELL} estimated bytes/cell), above the "
            f"{limit_mib:.1f} MiB cap from {MAX_ESTIMATED_MEMORY_ENV}; "
            "estimate is conservative planning data, not a hard RSS limit"
        )
    return budget


@contextmanager
def _capture_native_output(path: Path) -> Iterator[None]:
    """Capture native openEMS stdout/stderr as durable per-mesh evidence."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout, saved_stderr = os.dup(1), os.dup(2)
    log_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            ctypes.CDLL(None).fflush(None)
        except (AttributeError, OSError):
            pass
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)


def _fdtd_energy_criterion_reached(log_text: str) -> bool:
    """Return true only when openEMS reports its field-energy stop criterion."""
    criterion = re.escape(f"{FDTD_END_CRITERIA_DB:.2f}")
    return re.search(rf"end-criteria of {criterion}dB reached after \d+ timesteps", log_text) is not None


def describe_candidate(spec: dict[str, Any] | None = None,
                      candidate_path: Path | None = None) -> dict[str, Any]:
    """Load/validate the separate candidate model and return provenance."""
    del spec  # The candidate is kept separate so its assumptions do not stale prior spec-derived reports.
    source_path = (candidate_path or Path(__file__).with_name("candidate_model.json")).resolve()
    raw = source_path.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("candidate_model.json must use schema_version 1")
    candidate = document.get("candidate_model")
    if not isinstance(candidate, dict):
        raise ValueError("candidate_model.json is missing candidate_model mapping")
    for name, entry in candidate.items():
        if name == "limitations":
            continue
        if not isinstance(entry, dict) or not {"value", "unit", "source", "status"} <= entry.keys():
            raise ValueError(f"candidate input {name} must include value, unit, source, status")
        if str(entry.get("status", "")).upper() != "ASSUMED":
            raise ValueError(f"candidate_model.{name} must remain ASSUMED")
        if not isinstance(entry.get("source"), str) or not entry["source"].strip():
            raise ValueError(f"candidate_model.{name} requires a non-empty source")
    values = {name: entry["value"] for name, entry in candidate.items() if isinstance(entry, dict)}
    points = values["trace_centerline_mm"]
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError("trace_centerline_mm must contain at least two XY points")
    parsed = []
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("trace_centerline_mm points must be [x, y]")
        coords = [float(value) for value in point]
        if not all(math.isfinite(value) for value in coords):
            raise ValueError("trace_centerline_mm must be finite")
        parsed.append(coords)
    length = sum(math.dist(a, b) for a, b in zip(parsed, parsed[1:]))
    expected = float(values["element_length_mm"])
    if abs(length - expected) > max(0.01, expected * 0.005):
        raise ValueError(f"candidate trace length {length:.3f} mm does not match assumed {expected:.3f} mm")
    feed = values["feed_point_mm"]
    if list(map(float, feed)) != parsed[0]:
        raise ValueError("candidate feed_point_mm must coincide with first trace point")
    ground = values["ground_plane_mm"]
    if len(ground) != 2 or any(float(value) <= 0 for value in ground):
        raise ValueError("ground_plane_mm must contain positive [width, height]")
    trace_width = float(values["trace_width_mm"])
    board = list(map(float, values["board_mm"]))
    if abs(board[0] - float(ground[0])) > 1e-9 or abs(board[1] - float(ground[1])) > 1e-9:
        raise ValueError("ground plane and assumed PCB envelope must agree")
    try:
        repo_root = source_path.parents[2]
        relative_path = source_path.relative_to(repo_root).as_posix()
    except ValueError:
        relative_path = str(source_path)
    return {
        "name": str(values["name"]),
        "center_frequency_hz": float(values["center_frequency_hz"]),
        "candidate_model_provenance": {
            "path": relative_path,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "status": "ASSUMED",
        },
        "trace_centerline_mm": parsed,
        "trace_length_mm": length,
        "element_length_mm": expected,
        "trace_width_mm": trace_width,
        "feed_point_mm": list(map(float, feed)),
        "board_mm": board,
        "board_origin_mm": list(map(float, values["board_origin_mm"])),
        "ground_plane_mm": list(map(float, ground)),
        "pcb_relative_permittivity": float(values["pcb_relative_permittivity"]),
        "pcb_loss_tangent": float(values["pcb_loss_tangent"]),
        "battery_dimensions_mm": list(map(float, values["battery_dimensions_mm"])),
        "battery_offset_mm": list(map(float, values["battery_offset_mm"])),
        "enclosure_dimensions_mm": list(map(float, values["enclosure_dimensions_mm"])),
        "enclosure_offset_mm": list(map(float, values["enclosure_offset_mm"])),
        "enclosure_wall_mm": float(values["enclosure_wall_mm"]),
        "enclosure_relative_permittivity": float(values["enclosure_relative_permittivity"]),
        "enclosure_conductivity_s_m": float(values["enclosure_conductivity_s_m"]),
        "animal_relative_permittivity": float(values["animal_relative_permittivity"]),
        "animal_conductivity_s_m": float(values["animal_conductivity_s_m"]),
        "animal_slab_thickness_mm": float(values["animal_slab_thickness_mm"]),
        "animal_slab_air_gap_mm": float(values["animal_slab_air_gap_mm"]),
        "animal_slab_dimensions_mm": list(map(float, values["animal_slab_dimensions_mm"])),
        "animal_slab_offset_mm": list(map(float, values["animal_slab_offset_mm"])),
        "copper_model": str(values["copper_model"]),
        "battery_model": str(values["battery_model"]),
        "enclosure_shape": str(values["enclosure_shape"]),
        "animal_shape": str(values["animal_shape"]),
        "limitations": list(candidate.get("limitations", [])),
    }


def _box(prop: Any, low: tuple[float, float, float], high: tuple[float, float, float], priority: int = 10) -> None:
    prop.AddBox(start=list(low), stop=list(high), priority=priority)


def _solid_or_shell(csx: Any, candidate: dict[str, Any], scenario: str) -> dict[str, Any]:
    """Construct one isolated scenario from ASSUMED dimensions and material surrogates."""
    width, height, board_t = candidate["board_mm"]
    ground = csx.AddMetal("finite_reference_ground")
    board_x, board_y, board_z = candidate["board_origin_mm"]
    _box(ground, (board_x-width / 2, board_y-height / 2, board_z),
         (board_x+width / 2, board_y+height / 2, board_z))

    if scenario != "ANTENNA_FREE_SPACE":
        eps_r = candidate["pcb_relative_permittivity"]
        tan_delta = candidate["pcb_loss_tangent"]
        frequency = candidate["center_frequency_hz"]
        epsilon_0_f_m = 8.8541878128e-12
        dielectric_kappa = 2 * math.pi * frequency * epsilon_0_f_m * eps_r * tan_delta
        substrate = csx.AddMaterial("assumed_generic_fr4", epsilon=eps_r, kappa=dielectric_kappa)
        _box(substrate, (board_x-width/2, board_y-height/2, board_z),
             (board_x+width/2, board_y+height/2, board_z+board_t), priority=1)

    trace = csx.AddMetal("assumed_pec_antenna_trace")
    half = candidate["trace_width_mm"] / 2
    z = board_z + board_t
    points = candidate["trace_centerline_mm"]
    for start, end in zip(points, points[1:]):
        x1, y1 = start
        x2, y2 = end
        if abs(y2 - y1) < 1e-9:
            low = (min(x1, x2), y1 - half, z)
            high = (max(x1, x2), y1 + half, z)
        elif abs(x2 - x1) < 1e-9:
            low = (x1 - half, min(y1, y2), z)
            high = (x1 + half, max(y1, y2), z)
        else:
            raise ValueError("candidate trace must use axis-aligned segments")
        _box(trace, low, high)

    battery_meta = None
    if scenario == "ANTENNA_WITH_BATTERY":
        diameter, length, depth = candidate["battery_dimensions_mm"]
        offset_x, offset_y, bottom = candidate["battery_offset_mm"]
        battery = csx.AddMetal("assumed_solid_pec_battery_surrogate")
        _box(battery, (offset_x, offset_y, bottom),
             (offset_x + diameter, offset_y + length, bottom + depth))
        battery_meta = {"shape": "rectangular PEC cuboid surrogate", "dimensions_mm": [diameter, length, depth],
                        "offset_mm": [offset_x, offset_y, bottom], "physical_cell_structure_modeled": False}

    enclosure_meta = None
    if scenario == "ANTENNA_WITH_ENCLOSURE":
        ew, eh, et = candidate["enclosure_dimensions_mm"]
        wall = candidate["enclosure_wall_mm"]
        eps = candidate["enclosure_relative_permittivity"]
        sigma = candidate["enclosure_conductivity_s_m"]
        shell = csx.AddMaterial("assumed_generic_enclosure_polymer", epsilon=eps, kappa=sigma)
        ox, oy, oz = candidate["enclosure_offset_mm"]
        x2, y2, z2 = ox+ew, oy+eh, oz+et
        _box(shell, (ox, oy, oz), (x2, y2, oz+wall))
        _box(shell, (ox, oy, z2-wall), (x2, y2, z2))
        _box(shell, (ox, oy, oz+wall), (ox+wall, y2, z2-wall))
        _box(shell, (x2-wall, oy, oz+wall), (x2, y2, z2-wall))
        _box(shell, (ox+wall, oy, oz+wall), (x2-wall, oy+wall, z2-wall))
        _box(shell, (ox+wall, y2-wall, oz+wall), (x2-wall, y2, z2-wall))
        enclosure_meta = {"shape": "six-wall rectangular dielectric shell", "outer_dimensions_mm": [ew, eh, et],
                          "wall_mm": wall, "material": "generic assumed dielectric", "openings_modeled": False}

    animal_meta = None
    if scenario == "ANTENNA_NEAR_ANIMAL_APPROXIMATION":
        eps = candidate["animal_relative_permittivity"]
        sigma = candidate["animal_conductivity_s_m"]
        thickness = candidate["animal_slab_thickness_mm"]
        slab = csx.AddMaterial("assumed_homogeneous_animal_sensitivity_slab", epsilon=eps, kappa=sigma)
        # Slab offset and gap are explicit model inputs.
        slab_x, slab_y, slab_z = candidate["animal_slab_offset_mm"]
        slab_width, slab_height, slab_depth = candidate["animal_slab_dimensions_mm"]
        _box(slab, (slab_x, slab_y, slab_z),
             (slab_x+slab_width, slab_y+slab_height, slab_z+slab_depth), priority=1)
        animal_meta = {"shape": "homogeneous dielectric slab", "relative_permittivity": eps,
                       "conductivity_s_m": sigma, "thickness_mm": thickness,
                       "offset_mm": [slab_x, slab_y, slab_z],
                       "nominal_air_gap_mm": candidate["animal_slab_air_gap_mm"],
                       "tissue_validation": False}
    return {"trace": trace, "ground": ground, "battery": battery_meta,
            "enclosure": enclosure_meta, "animal": animal_meta}


def _scenario_domain(candidate: dict[str, Any], scenario: str) -> tuple[np.ndarray, np.ndarray]:
    width, height, _ = candidate["board_mm"]
    x_half = width/2 + AIR_MARGIN_MM
    y_half = height/2 + AIR_MARGIN_MM
    z_min, z_max = -AIR_MARGIN_MM, candidate["board_mm"][2] + AIR_MARGIN_MM
    if scenario == "ANTENNA_WITH_BATTERY":
        z_min = -candidate["board_mm"][2] - 20
    if scenario == "ANTENNA_WITH_ENCLOSURE":
        ew, eh, et = candidate["enclosure_dimensions_mm"]
        x_half = max(x_half, ew/2 + AIR_MARGIN_MM)
        y_half = max(y_half, eh/2 + AIR_MARGIN_MM)
        z_max = et + AIR_MARGIN_MM
    if scenario == "ANTENNA_NEAR_ANIMAL_APPROXIMATION":
        x_half += candidate["animal_slab_thickness_mm"]
    return np.array([-x_half, -y_half, z_min]), np.array([x_half, y_half, z_max])


def _mesh_count(csx: Any) -> dict[str, int]:
    grid = csx.GetGrid()
    return {axis: int(len(grid.GetLines(index)) - 1) for index, axis in enumerate(("x", "y", "z"))}


def _run_resolution(candidate: dict[str, Any], scenario: str, out_dir: Path,
                    resolution_mm: float) -> dict[str, Any]:
    from CSXCAD import ContinuousStructure
    from openEMS import openEMS
    from openEMS.physical_constants import C0

    from . import SCENARIOS
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown antenna scenario: {scenario}")
    freq0 = candidate["center_frequency_hz"]
    f = np.linspace(freq0 * FREQUENCY_MIN_RATIO,
                    freq0 * FREQUENCY_MAX_RATIO, FREQUENCY_POINTS)
    root = out_dir / f"mesh_{resolution_mm:g}mm"
    root.mkdir(parents=True, exist_ok=True)
    sim_dir = root / "openems"
    sim_dir.mkdir(parents=True, exist_ok=True)

    fdtd = openEMS(NrTS=160000, EndCriteria=FDTD_END_CRITERIA)
    fdtd.SetGaussExcite(freq0, freq0 * 0.9)
    fdtd.SetBoundaryCond(["PML_8"] * 6)
    csx = ContinuousStructure()
    fdtd.SetCSX(csx)
    grid = csx.GetGrid()
    grid.SetDeltaUnit(1e-3)  # all geometry is specified in mm
    minimum, maximum = _scenario_domain(candidate, scenario)
    for axis_index, axis in enumerate(("x", "y", "z")):
        grid.AddLine(axis, np.arange(minimum[axis_index], maximum[axis_index] + resolution_mm,
                                     resolution_mm).tolist())
    geom = _solid_or_shell(csx, candidate, scenario)
    fdtd.AddEdges2Grid(dirs="xyz", properties=geom["trace"], metal_edge_res=0.25)
    fdtd.AddEdges2Grid(dirs="xy", properties=geom["ground"])
    grid.SmoothMeshLines("all", resolution_mm, 1.4)

    feed_x, feed_y = candidate["feed_point_mm"]
    board_z = candidate["board_origin_mm"][2]
    port = fdtd.AddLumpedPort(1, 50, [feed_x, feed_y, board_z],
                              [feed_x, feed_y, board_z+candidate["board_mm"][2]], "z", 1.0,
                              priority=20, edges2grid="xyz")
    # The near-field box is created only after the final mesh and boundary setup.
    nf2ff = fdtd.CreateNF2FFBox()
    # Count the final smoothed grid before Run(), where openEMS allocates its
    # large native FDTD field arrays. Keep both actual dimensions and the
    # explicitly estimated (not guaranteed) memory footprint in evidence.
    mesh_budget = _mesh_budget(_mesh_count(csx))
    solver_log_path = root / "solver.log"
    with _capture_native_output(solver_log_path):
        fdtd.Run(str(sim_dir), cleanup=True, verbose=0, numThreads=1)
    solver_log = solver_log_path.read_text(errors="replace")
    if not _fdtd_energy_criterion_reached(solver_log):
        tail = " ".join(solver_log.splitlines()[-4:])[-600:]
        raise RuntimeError(
            f"openEMS did not reach the {FDTD_END_CRITERIA_DB:.0f} dB field-energy criterion "
            f"before its timestep limit; log={solver_log_path}; tail: {tail}"
        )
    port.CalcPort(str(sim_dir), f)
    s11 = (np.asarray(port.uf_ref) / np.asarray(port.uf_inc)).reshape(-1)
    zin = (np.asarray(port.uf_tot) / np.asarray(port.if_tot)).reshape(-1)
    s11_db = 20 * np.log10(np.maximum(np.abs(s11), 1e-15))
    idx = int(np.nanargmin(s11_db))
    if idx == 0 or idx == len(f) - 1:
        raise RuntimeError("S11 minimum lies on the sweep boundary; resonance is not bracketed")
    resonant = float(f[idx])
    theta = np.arange(0, 181, 5, dtype=float)
    phi = np.array([0.0, 90.0, 180.0, 270.0])
    ff = nf2ff.CalcNF2FF(str(sim_dir), resonant, theta, phi, center=[0, 0, 0])
    prad = float(np.asarray(ff.Prad).reshape(-1)[0])
    pacc = float(np.asarray(port.P_acc)[idx])
    efficiency = prad / pacc if pacc > 0 else math.nan
    if not math.isfinite(efficiency) or not 0 < efficiency <= 1.05:
        raise RuntimeError(f"solver-derived radiation efficiency is invalid: {efficiency!r}")
    directivity_dbi = 10 * math.log10(float(np.asarray(ff.Dmax).reshape(-1)[0]))
    gain_dbi = directivity_dbi + 10 * math.log10(efficiency) if efficiency > 0 else math.nan
    vswr = (1 + abs(s11[idx])) / max(1 - abs(s11[idx]), 1e-15)

    s11_path = root / "s11.csv"
    with s11_path.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["frequency_hz", "s11_real", "s11_imag", "s11_db"])
        writer.writerows((float(freq), float(np.real(gamma)), float(np.imag(gamma)), float(db))
                         for freq, gamma, db in zip(f, s11, s11_db))
    impedance_path = root / "input_impedance.csv"
    with impedance_path.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["frequency_hz", "real_ohm", "imag_ohm"])
        writer.writerows((float(freq), float(z.real), float(z.imag)) for freq, z in zip(f, zin))
    pattern_path = root / "radiation_pattern.csv"
    with pattern_path.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["theta_deg", "phi_deg", "gain_dbi"])
        for theta_index, angle in enumerate(theta):
            for phi_index, phi_angle in enumerate(phi):
                field = float(np.asarray(ff.E_norm[0])[theta_index, phi_index])
                field_gain = (20 * math.log10(max(field / max(float(np.max(ff.E_norm[0])), 1e-30), 1e-30))
                              + directivity_dbi + 10 * math.log10(efficiency))
                writer.writerow([float(angle), float(phi_angle), field_gain])
    raw_nf2ff = Path(ff.fn)
    if not raw_nf2ff.is_file():
        raise RuntimeError("openEMS NF2FF raw result file is missing")
    shutil.copy2(raw_nf2ff, root / "radiation_pattern_raw.h5")
    # Retain all solver outputs (FDTD XML, probe files, field dumps and logs)
    # beneath mesh_<resolution>mm/openems rather than reducing to CSV summaries.
    files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    return {
        "resonant_frequency_hz": resonant,
        "s11_min_db": float(s11_db[idx]),
        "input_impedance_real_ohm": float(np.real(zin[idx])),
        "input_impedance_imag_ohm": float(np.imag(zin[idx])),
        "vswr_min": float(vswr),
        "efficiency_fraction": float(efficiency),
        "gain_dbi": float(gain_dbi),
        "s11_curve_path": s11_path.relative_to(out_dir).as_posix(),
        "radiation_pattern_path": pattern_path.relative_to(out_dir).as_posix(),
        "_mesh": {**mesh_budget, "resolution_mm": resolution_mm},
        "_raw_files": [f"{root.name}/{path}" for path in files],
        "_pattern_source": str(raw_nf2ff),
    }


def simulate(*, spec: dict[str, Any], scenario: str, output_dir: str) -> dict[str, Any]:
    """Run two actual openEMS meshes and fail closed unless their S11 agrees."""
    try:
        candidate = describe_candidate(spec)
        root = Path(output_dir) / scenario
        root.mkdir(parents=True, exist_ok=True)
        geometry = _solid_or_shell_description(candidate, scenario)
        (root / "assumed_geometry.json").write_text(json.dumps(geometry, indent=2, sort_keys=True) + "\n")
        coarse = _run_resolution(candidate, scenario, root, SOLVER_RESOLUTIONS_MM[0])
        fine = _run_resolution(candidate, scenario, root, SOLVER_RESOLUTIONS_MM[1])
        s11_delta = abs(coarse["s11_min_db"] - fine["s11_min_db"])
        resonance_delta = abs(coarse["resonant_frequency_hz"] - fine["resonant_frequency_hz"]) / fine["resonant_frequency_hz"]
        converged = s11_delta <= S11_CONVERGENCE_DB and resonance_delta <= RESONANCE_CONVERGENCE_FRACTION
        evidence = {
            "solver_version": _openems_version(),
            "geometry_hash": _geometry_hash(geometry),
            "geometry_status": "ASSUMED_UNVALIDATED",
            "candidate_model_provenance": candidate["candidate_model_provenance"],
            "geometry": geometry,
            "mesh": {"coarse": coarse["_mesh"], "fine": fine["_mesh"],
                     "comparison": {"s11_min_delta_db": s11_delta,
                                    "resonant_frequency_relative_delta": resonance_delta,
                                    "criteria": {"s11_min_max_delta_db": S11_CONVERGENCE_DB,
                                                 "resonance_max_relative_delta": RESONANCE_CONVERGENCE_FRACTION}}},
            "converged": converged,
            "raw_solver_files": coarse["_raw_files"] + fine["_raw_files"],
            "model_limitations": candidate["limitations"],
        }
        clean_metrics = {field: fine[field] for field in (
            "resonant_frequency_hz", "s11_min_db", "input_impedance_real_ohm",
            "input_impedance_imag_ohm", "vswr_min", "efficiency_fraction", "gain_dbi",
            "s11_curve_path", "radiation_pattern_path")}
        if not converged:
            return {"status": "FAILED", "detail": "mesh refinement did not meet declared numerical convergence criteria",
                    "metrics": clean_metrics, "evidence": evidence}
        return {"status": "COMPLETED", "detail": "openEMS simulated ASSUMED candidate; numerical mesh comparison passed; no physical validation",
                "metrics": clean_metrics, "evidence": evidence}
    except Exception as exc:
        return {"status": "FAILED", "detail": f"openEMS candidate simulation failed closed: {type(exc).__name__}: {exc}"}


def _openems_version() -> str:
    from openEMS import __version__
    return str(__version__)


def _geometry_hash(geometry: dict[str, Any]) -> str:
    payload = json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _solid_or_shell_description(candidate: dict[str, Any], scenario: str) -> dict[str, Any]:
    model: dict[str, Any] = {
        "candidate": candidate["name"],
        "scenario": scenario,
        "candidate_model_provenance": candidate["candidate_model_provenance"],
        "trace_centerline_mm": candidate["trace_centerline_mm"],
        "trace_width_mm": candidate["trace_width_mm"],
        "feed_point_mm": candidate["feed_point_mm"],
        "board_mm": candidate["board_mm"],
        "board_origin_mm": candidate["board_origin_mm"],
        "ground_plane_mm": candidate["ground_plane_mm"],
        "substrate": {"epsilon_r": candidate["pcb_relative_permittivity"],
                      "loss_tangent": candidate["pcb_loss_tangent"], "status": "ASSUMED"},
        "conductor_model": "PEC",
        "status": "ASSUMED_UNVALIDATED",
        "limitations": candidate["limitations"],
    }
    if scenario == "ANTENNA_WITH_BATTERY":
        model["battery_surrogate"] = {"shape": "PEC cuboid",
                                       "dimensions_mm": candidate["battery_dimensions_mm"],
                                       "offset_mm": candidate["battery_offset_mm"], "status": "ASSUMED"}
    elif scenario == "ANTENNA_WITH_ENCLOSURE":
        model["enclosure_surrogate"] = {
            "shape": "closed six-wall rectangular shell",
            "outer_dimensions_mm": candidate["enclosure_dimensions_mm"],
            "offset_mm": candidate["enclosure_offset_mm"],
            "wall_mm": candidate["enclosure_wall_mm"],
            "epsilon_r": candidate["enclosure_relative_permittivity"],
            "conductivity_s_m": candidate["enclosure_conductivity_s_m"],
            "omitted_features": ["cap seams", "holes", "ribs", "molding details"],
            "status": "ASSUMED",
        }
    elif scenario == "ANTENNA_NEAR_ANIMAL_APPROXIMATION":
        thickness = candidate["animal_slab_thickness_mm"]
        model["animal_slab_surrogate"] = {
            "shape": "homogeneous rectangular slab",
            "epsilon_r": candidate["animal_relative_permittivity"],
            "conductivity_s_m": candidate["animal_conductivity_s_m"],
            "thickness_mm": thickness,
            "offset_mm": [candidate["board_mm"][0]/2 + candidate["animal_slab_air_gap_mm"],
                          -candidate["board_mm"][1]/2, -thickness/2],
            "air_gap_mm": candidate["animal_slab_air_gap_mm"],
            "tissue_validation": False,
            "status": "ASSUMED",
        }
    return model
