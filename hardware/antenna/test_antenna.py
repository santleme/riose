import csv
import json
import os
import sys
import types

import numpy as np
import pytest

from hardware.antenna import SCENARIOS
from hardware.antenna import run as antenna_run
from hardware.antenna.capabilities import detect_capabilities
from hardware.antenna.openems_adapter import (
    DEFAULT_MAX_ESTIMATED_MEMORY_MIB,
    ESTIMATED_BYTES_PER_CELL,
    MeshBudgetExceeded,
    SOLVER_RESOLUTIONS_MM,
    _capture_native_output,
    _fdtd_energy_criterion_reached,
    _mesh_budget,
    _scenario_domain,
    _solid_or_shell,
    _solid_or_shell_description,
    describe_candidate,
)


def test_openems_mesh_budget_allows_existing_two_mm_candidate_envelope(monkeypatch):
    monkeypatch.delenv("RIOSE_OPENEMS_MAX_ESTIMATED_MEMORY_MIB", raising=False)
    budget = _mesh_budget({"x": 505_760, "y": 1, "z": 1})
    assert DEFAULT_MAX_ESTIMATED_MEMORY_MIB == 128.0
    assert budget["cells"] == 505_760
    assert budget["estimated_bytes_per_cell"] == ESTIMATED_BYTES_PER_CELL == 128
    assert budget["estimated_memory_mib"] < budget["max_estimated_memory_mib"]
    assert budget["memory_estimate_is_hard_rss_limit"] is False


def test_openems_mesh_budget_rejects_known_one_mm_pilot_mesh(monkeypatch):
    monkeypatch.delenv("RIOSE_OPENEMS_MAX_ESTIMATED_MEMORY_MIB", raising=False)
    with pytest.raises(MeshBudgetExceeded, match="before openEMS field allocation") as exc_info:
        _mesh_budget({"x": 2_322_540, "y": 1, "z": 1})
    assert "283.5 MiB" in str(exc_info.value)
    assert "128.0 MiB cap" in str(exc_info.value)


def test_openems_mesh_budget_env_override_is_in_mib(monkeypatch):
    monkeypatch.setenv("RIOSE_OPENEMS_MAX_ESTIMATED_MEMORY_MIB", "300")
    budget = _mesh_budget({"x": 2_322_540, "y": 1, "z": 1})
    assert budget["max_estimated_memory_mib"] == 300.0


def test_openems_adapter_rejects_fdtd_run_that_hits_timestep_limit():
    assert _fdtd_energy_criterion_reached(
        "RunFDTD: end-criteria of -40.00dB reached after 28968 timesteps (-40.69dB)"
    )
    assert not _fdtd_energy_criterion_reached(
        "RunFDTD: Warning: Max. number of timesteps was reached before the end-criteria of -40dB was reached"
    )


def test_native_solver_output_is_preserved_in_mesh_log(tmp_path):
    log_path = tmp_path / "solver.log"
    with _capture_native_output(log_path):
        os.write(1, b"native solver trace\n")
    assert log_path.read_text() == "native solver trace\n"


def test_missing_openems_writes_five_null_metric_scenarios(tmp_path, monkeypatch):
    monkeypatch.setattr(antenna_run, "_openems_available", lambda: (False, None))
    out = tmp_path / "antenna"
    result = antenna_run.run_experiments(None, out)
    assert result["status"] == "NOT_AVAILABLE"
    assert result["result_class"] == "NO_SIMULATION_RESULT"
    assert [row["scenario"] for row in result["scenarios"]] == list(SCENARIOS)
    assert all(row["status"] == "NOT_AVAILABLE" for row in result["scenarios"])
    assert all(row["resonant_frequency_hz"] is None for row in result["scenarios"])
    saved = json.loads((out / "antenna_experiments.json").read_text())
    assert saved["schema_version"] == antenna_run.SCHEMA_VERSION
    with (out / "antenna.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 5
    assert rows[0]["s11_min_db"] == ""


def test_assumed_defaults_and_explicit_non_measurement(tmp_path, monkeypatch):
    monkeypatch.setattr(antenna_run, "_openems_available", lambda: (False, None))
    result = antenna_run.run_experiments(None, tmp_path)
    params = result["antenna_parameters"]
    assert params["center_frequency_hz"]["value"] == 915_000_000
    assert params["center_frequency_hz"]["status"] == "ASSUMED"
    assert params["element_length_mm"]["value"] == 82.0


def test_spec_rejects_measured_status(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    spec = tmp_path / "spec.yaml"
    spec.write_text("""antenna:\n  center_frequency_hz: {value: 915000000, unit: Hz, source: candidate, status: MEASURED}\n""")
    monkeypatch.setattr(antenna_run, "_openems_available", lambda: (False, None))
    with pytest.raises(ValueError, match="cannot be MEASURED"):
        antenna_run.run_experiments(spec, tmp_path / "out")


def test_spec_values_and_provenance_are_preserved(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    spec = tmp_path / "spec.yaml"
    spec.write_text("""antenna:\n  center_frequency_hz: {value: 868000000, unit: Hz, source: datasheet candidate, status: ASSUMED}\n  topology: {value: monopole, unit: text, source: design note, status: ASSUMED}\n  element_length_mm: {value: 86, unit: mm, source: initial geometry, status: ASSUMED}\n  feed_mm: {value: 1.5, unit: mm, source: initial feed, status: ASSUMED}\n  clearance_mm: {value: 3, unit: mm, source: placeholder, status: ASSUMED}\n""")
    monkeypatch.setattr(antenna_run, "_openems_available", lambda: (False, None))
    result = antenna_run.run_experiments(spec, tmp_path / "out", [SCENARIOS[0]])
    row = result["scenarios"][0]
    assert row["center_frequency_hz"] == 868000000
    assert row["center_frequency_status"] == "ASSUMED"
    assert row["topology"] == "monopole"
    assert result["spec_provenance"]["sha256"]


def test_gpu_capabilities_report_is_optional_and_machine_readable():
    report = detect_capabilities()
    assert {"GPU_AVAILABLE", "GPU_TYPE", "CUDA_AVAILABLE", "SIONNA_AVAILABLE"} <= report.keys()
    assert report["experiment"] == "OPTIONAL_GPU_EXPERIMENT"
    assert report["result_status"] == "ENVIRONMENT_CAPABILITY_ONLY"


def test_openems_adapter_cannot_mark_incomplete_metrics_as_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(antenna_run, "_openems_available", lambda: (True, None))
    monkeypatch.setenv("RIOSE_OPENEMS_ADAPTER", "openems_test_adapter")
    adapter = types.SimpleNamespace(simulate=lambda **kwargs: {"status": "COMPLETED", "metrics": {}})
    monkeypatch.setitem(sys.modules, "openems_test_adapter", adapter)
    result = antenna_run.run_experiments(None, tmp_path, [SCENARIOS[0]])
    row = result["scenarios"][0]
    assert row["status"] == "FAILED"
    assert row["s11_min_db"] is None
    assert "incomplete openEMS result/provenance" in row["detail"]


def test_openems_adapter_requires_solver_and_mesh_evidence_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(antenna_run, "_openems_available", lambda: (True, None))
    monkeypatch.setenv("RIOSE_OPENEMS_ADAPTER", "openems_test_adapter")

    def simulate(**kwargs):
        output = __import__("pathlib").Path(kwargs["output_dir"])
        scenario_root = output / SCENARIOS[0]
        coarse = scenario_root / "mesh_4mm"
        fine = scenario_root / "mesh_2mm"
        coarse.mkdir(parents=True)
        fine.mkdir(parents=True)
        (coarse / "solver.log").write_text("coarse log\n")
        (fine / "solver.log").write_text("fine log\n")
        (coarse / "raw.h5").write_bytes(b"coarse raw")
        (fine / "raw.h5").write_bytes(b"fine raw")
        (fine / "s11.csv").write_text("frequency_hz,s11_db\n915000000,-3\n")
        (fine / "pattern.csv").write_text("theta,phi,gain_dbi\n0,0,1\n")
        return {
            "status": "COMPLETED", "metrics": {
                "resonant_frequency_hz": 915000000, "s11_min_db": -3,
                "input_impedance_real_ohm": 50, "input_impedance_imag_ohm": 0,
                "vswr_min": 1.2, "efficiency_fraction": 0.4, "gain_dbi": 1.0,
                "s11_curve_path": "mesh_2mm/s11.csv", "radiation_pattern_path": "mesh_2mm/pattern.csv",
            }, "evidence": {"solver_version": "test", "geometry_hash": "a" * 64,
                            "mesh": {
                                "coarse": {"resolution_mm": 4.0, "x": 5, "y": 4, "z": 5,
                                           "cells": 100},
                                "fine": {"resolution_mm": 2.0, "x": 10, "y": 4, "z": 5,
                                         "cells": 200},
                                "comparison": {
                                    "s11_min_delta_db": 0.3,
                                    "resonant_frequency_relative_delta": 0.01,
                                    "criteria": {"s11_min_max_delta_db": 1.0,
                                                 "resonance_max_relative_delta": 0.02},
                                },
                            }, "converged": True,
                            "candidate_model_provenance": {
                                "path": "candidate_model.json", "sha256": "b" * 64,
                                "status": "ASSUMED",
                            },
                            "raw_solver_files": ["mesh_4mm/raw.h5", "mesh_2mm/raw.h5"]},
        }

    monkeypatch.setitem(sys.modules, "openems_test_adapter", types.SimpleNamespace(simulate=simulate))
    result = antenna_run.run_experiments(None, tmp_path, [SCENARIOS[0]])
    assert result["status"] == "COMPLETED"
    assert result["scenarios"][0]["solver_evidence"]["converged"] is True
    assert result["scenarios"][0]["s11_curve_path"] == "mesh_2mm/s11.csv"


def test_openems_adapter_rejects_unstructured_mesh_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(antenna_run, "_openems_available", lambda: (True, None))
    monkeypatch.setenv("RIOSE_OPENEMS_ADAPTER", "openems_incomplete_mesh_adapter")

    def simulate(**kwargs):
        output = __import__("pathlib").Path(kwargs["output_dir"])
        (output / "s11.csv").write_text("frequency_hz,s11_db\n915000000,-3\n")
        (output / "pattern.csv").write_text("theta,phi,gain_dbi\n0,0,1\n")
        return {
            "status": "COMPLETED",
            "metrics": {
                "resonant_frequency_hz": 915000000, "s11_min_db": -3,
                "input_impedance_real_ohm": 50, "input_impedance_imag_ohm": 0,
                "vswr_min": 1.2, "efficiency_fraction": 0.4, "gain_dbi": 1.0,
                "s11_curve_path": "s11.csv", "radiation_pattern_path": "pattern.csv",
            },
            "evidence": {"solver_version": "test", "geometry_hash": "a" * 64,
                         "mesh": {"cells": 100}, "converged": True,
                         "candidate_model_provenance": {"path": "candidate.json",
                                                         "sha256": "b" * 64,
                                                         "status": "ASSUMED"},
                         "raw_solver_files": ["present.h5"]},
        }

    (tmp_path / "present.h5").write_bytes(b"raw")
    monkeypatch.setitem(sys.modules, "openems_incomplete_mesh_adapter",
                        types.SimpleNamespace(simulate=simulate))
    result = antenna_run.run_experiments(None, tmp_path, [SCENARIOS[0]])
    row = result["scenarios"][0]
    assert row["status"] == "FAILED"
    assert "evidence.mesh.coarse" in row["detail"]


def test_assumed_openems_candidate_geometry_is_explicit_and_reproducible():
    pytest.importorskip("yaml")
    from pathlib import Path
    import yaml

    spec = yaml.safe_load(Path("hardware/spec.yaml").read_text())
    geometry = describe_candidate(spec)
    assert geometry["name"] == "planar_four_run_monopole_v1"
    assert geometry["trace_length_mm"] == pytest.approx(82.0, abs=0.01)
    assert geometry["trace_width_mm"] == 1.0
    assert geometry["board_mm"] == [30.0, 48.0, 1.6]
    assert geometry["candidate_model_provenance"]["path"] == "hardware/antenna/candidate_model.json"
    assert len(geometry["candidate_model_provenance"]["sha256"]) == 64
    assert any("not a released routed antenna" in note for note in geometry["limitations"])


def test_candidate_adapter_rejects_unlabeled_or_non_assumed_geometry(tmp_path):
    pytest.importorskip("yaml")
    from pathlib import Path
    import json
    import yaml

    spec = yaml.safe_load(Path("hardware/spec.yaml").read_text())
    candidate_path = Path("hardware/antenna/candidate_model.json")
    candidate = json.loads(candidate_path.read_text())
    candidate["candidate_model"]["trace_centerline_mm"]["status"] = "SIMULATED"
    malformed_path = tmp_path / "candidate_model.json"
    malformed_path.write_text(json.dumps(candidate))
    with pytest.raises(ValueError, match="must remain ASSUMED"):
        describe_candidate(spec, malformed_path)


def test_candidate_geometry_constructor_covers_all_five_scenarios_without_solver():
    pytest.importorskip("yaml")
    from pathlib import Path
    import yaml

    class Primitive:
        def __init__(self, name):
            self.name = name
            self.boxes = []

        def AddBox(self, **box):
            self.boxes.append(box)

    class FakeCSX:
        def __init__(self):
            self.properties = {}

        def AddMetal(self, name):
            prop = Primitive(name)
            self.properties[name] = prop
            return prop

        def AddMaterial(self, name, **kwargs):
            prop = Primitive(name)
            prop.material = kwargs
            self.properties[name] = prop
            return prop

    spec = yaml.safe_load(Path("hardware/spec.yaml").read_text())
    candidate = describe_candidate(spec)
    constructions = {}
    for scenario in SCENARIOS:
        csx = FakeCSX()
        geom = _solid_or_shell(csx, candidate, scenario)
        description = _solid_or_shell_description(candidate, scenario)
        constructions[scenario] = csx
        assert geom["trace"].boxes
        assert geom["ground"].boxes
        assert description["scenario"] == scenario
        assert description["status"] == "ASSUMED_UNVALIDATED"
    assert "assumed_generic_fr4" not in constructions["ANTENNA_FREE_SPACE"].properties
    assert "assumed_generic_fr4" in constructions["ANTENNA_WITH_PCB"].properties
    csx = constructions["ANTENNA_WITH_BATTERY"]
    assert "assumed_solid_pec_battery_surrogate" in csx.properties
    csx = constructions["ANTENNA_WITH_ENCLOSURE"]
    assert len(csx.properties["assumed_generic_enclosure_polymer"].boxes) == 6
    csx = constructions["ANTENNA_NEAR_ANIMAL_APPROXIMATION"]
    assert "assumed_homogeneous_animal_sensitivity_slab" in csx.properties


def test_scenario_mesh_domains_leave_room_for_openems_pml_layers():
    candidate = describe_candidate()
    for scenario in SCENARIOS:
        minimum, maximum = _scenario_domain(candidate, scenario)
        for resolution in SOLVER_RESOLUTIONS_MM:
            # PML_8 consumes nine mesh lines at each boundary. OpenEMS needs
            # more lines than those two boundary regions on every axis.
            counts = [len(np.arange(low, high + resolution, resolution))
                      for low, high in zip(minimum, maximum, strict=True)]
            assert all(count > 18 for count in counts), (scenario, resolution, counts)
