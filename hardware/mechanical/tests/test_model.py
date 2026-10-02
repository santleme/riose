from pathlib import Path
import json

import pytest

from hardware.mechanical.model import SpecError, build_report, load_spec, main


ROOT = Path(__file__).parent


def test_fitting_assumed_spec_reports_mass_and_center_of_mass():
    report = build_report(load_spec(ROOT / "fixtures/fitting_spec.yaml"))
    assert report["fit"]["fits"] is True
    assert report["gate"] == "CONDITIONALLY_READY_PENDING_THRESHOLD_APPROVAL"
    assert report["mass_estimate_g"]["total"] > 0
    assert set(report["center_of_mass_mm"]) == {"x_mm", "y_mm", "z_mm"}
    assert report["mounting_hole"]["status"] == "ASSUMED"
    assert "MEASURED" not in report["specification_statuses"]
    assert not any(len(warning) == 1 for warning in report["warnings"])


def test_missing_material_density_is_reported_as_assumed_fallback():
    spec = load_spec(ROOT / "fixtures/fitting_spec.yaml")
    del spec["materials"]["pcb_density_g_cm3"]
    report = build_report(spec)
    assert "pcb density uses an ASSUMED fallback" in report["warnings"]
    assert not any(len(warning) == 1 for warning in report["warnings"])


def test_real_candidate_reports_clear_assumed_battery_fit_without_rewriting_inputs():
    spec_path = ROOT.parent.parent / "spec.yaml"
    report = build_report(load_spec(spec_path))
    assert report["fit"]["fits"] is True
    assert report["fit"]["issues"] == []
    assert {part["name"] for part in report["parts"]} >= {"antenna", "antenna_keepout"}
    assert report["envelope_mm"] == {"width": 38.0, "height": 68.0, "thickness": 18.5}
    assert report["mass_estimate_g"]["status"] == "ASSUMED"
    assert report["gate"] == "CONDITIONALLY_READY_PENDING_THRESHOLD_APPROVAL"


def test_candidate_layout_keeps_antenna_and_cell_envelopes_in_the_cavity():
    report = build_report(load_spec(ROOT.parent.parent / "spec.yaml"))
    issues = report["fit"]["issues"]
    parts = {part["name"]: part for part in report["parts"]}

    # The selected candidate keeps the 32 mm keepout inside the 35.6 mm cavity.
    assert parts["pcb"]["x"] == pytest.approx(-1.8)
    assert parts["pcb"]["y"] == pytest.approx(-7.5)
    assert "antenna_keepout envelope exceeds enclosure cavity" not in issues
    assert parts["antenna_keepout"]["bounds_mm"]["x"] == pytest.approx([-17.8, 14.2])

    # Keepout may extend off the PCB into empty cavity; the antenna conductor
    # itself must fit the board and mounted parts must remain outside the zone.
    assert parts["antenna"]["bounds_mm"]["x"] == pytest.approx([-15.8, 12.2])
    assert not any("violates antenna keepout" in issue for issue in issues)
    assert parts["battery"]["x"] == pytest.approx(-4.7)
    assert parts["battery"]["bounds_mm"]["x"] == pytest.approx([-17.3, 7.9])
    assert parts["battery"]["bounds_mm"]["y"] == pytest.approx([17.0, 31.5])
    assert report["mounting_hole"]["center_mm"]["x"] == pytest.approx(15.0)
    assert report["mounting_hole"]["center_mm"]["y"] == pytest.approx(30.8)


def test_battery_pcb_clearance_near_miss_is_reported():
    spec = load_spec(ROOT.parent.parent / "spec.yaml")
    spec["mechanical"]["layout_candidate"]["battery_center_y_mm"]["value"] = 23.85
    report = build_report(spec)
    assert "battery violates minimum clearance to pcb envelope" in report["fit"]["issues"]
    assert report["fit"]["fits"] is False


def test_mounting_hole_clearance_near_miss_is_reported():
    spec = load_spec(ROOT.parent.parent / "spec.yaml")
    spec["mechanical"]["enclosure"]["mounting_hole_center_x_mm"]["value"] = 10.0
    report = build_report(spec)
    assert "mounting hole violates clearance to battery envelope" in report["fit"]["issues"]
    assert report["fit"]["fits"] is False


def test_mounting_hole_must_remain_inside_enclosure_width():
    spec = load_spec(ROOT.parent.parent / "spec.yaml")
    spec["mechanical"]["enclosure"]["mounting_hole_center_x_mm"]["value"] = 18.0
    report = build_report(spec)
    assert "mounting hole is outside the enclosure envelope" in report["fit"]["issues"]
    assert report["fit"]["fits"] is False


def test_measured_dimension_is_rejected():
    spec = load_spec(ROOT / "fixtures/fitting_spec.yaml")
    spec["mechanical"]["enclosure"]["width_mm"]["status"] = "MEASURED"
    with pytest.raises(SpecError, match="MEASURED"):
        build_report(spec)


def test_missing_required_dimensions_are_not_silently_defaulted():
    with pytest.raises(SpecError, match="mechanical.enclosure.width_mm"):
        build_report({"mechanical": {}})


def test_cli_spec_is_explicitly_unavailable_when_absent():
    with pytest.raises(SpecError, match="Hardware spec not found"):
        load_spec(ROOT / "fixtures/not-a-spec.yaml")


def test_cli_success_means_analysis_completed_even_when_fit_is_blocked(tmp_path):
    spec_path = ROOT.parent.parent / "spec.yaml"
    output = tmp_path / "geometry.json"
    assert main(["--spec", str(spec_path), "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["fit"]["fits"] is True
    assert result["gate"] == "CONDITIONALLY_READY_PENDING_THRESHOLD_APPROVAL"
