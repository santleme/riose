from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from riose.products.ear_tag.mvp3.bridge.gazebo_to_resd import (
    convert_trace,
    normalize_trace,
    parse_trace,
)
from riose.products.ear_tag.mvp3.cli import _scenario_assets
from riose.products.ear_tag.mvp3.scenarios import get_scenario


MVP3 = Path(__file__).parents[2] / "src/riose/products/ear_tag/mvp3"


def _xml(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def test_tag_attachment_is_a_cow_scoped_joint_to_nested_tag_link() -> None:
    world = _xml(MVP3 / "gazebo/worlds/riose_mvp3.sdf").find("world")
    cow = _xml(MVP3 / "gazebo/models/riose_cow/model.sdf").find("model")
    assert world is not None and cow is not None

    world_joints = world.findall("joint")
    assert not any(joint.get("name") == "riose_ear_tag_attachment" for joint in world_joints)
    tag_includes = [
        include for include in cow.findall("include")
        if include.findtext("uri") == "model://riose_ear_tag"
    ]
    assert len(tag_includes) == 1
    assert tag_includes[0].findtext("name") == "riose_ear_tag"

    attachments = [
        joint for joint in cow.findall("joint")
        if joint.get("name") == "riose_ear_tag_attachment"
    ]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.get("type") == "revolute"
    assert attachment.findtext("parent") == "ear_left"
    assert attachment.findtext("child") == "riose_ear_tag::tag_link"


def test_tag_sensor_is_simulated_50_hz_imu_with_stable_topic() -> None:
    tag = _xml(MVP3 / "gazebo/models/riose_ear_tag/model.sdf").find("model")
    assert tag is not None
    sensor, = tag.findall(".//sensor[@name='imu']")
    assert sensor.get("type") == "imu"
    assert sensor.findtext("update_rate") == "50"
    assert sensor.findtext("topic") == "/riose/mvp3/imu/data"


def test_tag_rounded_visual_mesh_is_packaged_without_replacing_collision_proxy(tmp_path: Path) -> None:
    source_model = MVP3 / "gazebo/models/riose_ear_tag/model.sdf"
    tag = _xml(source_model).find("model")
    assert tag is not None
    mesh_uri = tag.findtext("link/visual[@name='rounded_housing']/geometry/mesh/uri")
    assert mesh_uri == "model://riose_ear_tag/meshes/tag_housing.dae"
    mesh = MVP3 / "gazebo/models/riose_ear_tag/meshes/tag_housing.dae"
    assert mesh.is_file() and mesh.stat().st_size > 10_000
    dae = _xml(mesh)
    namespace = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
    assert dae.findtext(".//c:library_images/c:image/c:init_from", namespaces=namespace) == \
        "../materials/textures/yellow_polymer_albedo.png"
    assert dae.find(".//c:profile_COMMON/c:technique/c:phong/c:diffuse/c:texture", namespace) is not None
    uv_input = dae.find(".//c:triangles/c:input[@semantic='TEXCOORD']", namespace)
    assert uv_input is not None and uv_input.get("set") == "0"
    uv_binding = dae.find(".//c:bind_vertex_input[@input_semantic='TEXCOORD']", namespace)
    assert uv_binding is not None and uv_binding.get("input_set") == "0"
    assert tag.find("link/visual[@name='rounded_housing']/material") is None
    source_obj = MVP3 / "gazebo/models/riose_ear_tag/meshes/tag_housing.obj"
    assert source_obj.is_file()
    vertices = [
        tuple(map(float, line.split()[1:4]))
        for line in source_obj.read_text(encoding="utf-8").splitlines()
        if line.startswith("v ")
    ]
    assert len(vertices) == 1024
    assert min(point[0] for point in vertices) == pytest.approx(-0.034, abs=0.0004)
    assert max(point[0] for point in vertices) == pytest.approx(0.004, abs=0.0004)
    assert min(point[1] for point in vertices) == pytest.approx(-0.0648, abs=0.0004)
    assert max(point[1] for point in vertices) == pytest.approx(0.0032, abs=0.0004)

    collisions = tag.findall("link/collision")
    assert len(collisions) == 4
    copied_assets = _scenario_assets(get_scenario("standing"), tmp_path)
    assert (copied_assets / "riose_ear_tag/meshes/tag_housing.dae").is_file()
    assert (copied_assets / "riose_ear_tag/materials/textures/yellow_polymer_albedo.png").is_file()


def test_world_has_physical_paddock_context_without_changing_ground_physics() -> None:
    world = _xml(MVP3 / "gazebo/worlds/riose_mvp3.sdf").find("world")
    assert world is not None
    assert world.findtext("gravity") == "0 0 -9.81"
    assert world.findtext("physics/max_step_size") == "0.001"

    models = {model.get("name"): model for model in world.findall("model")}
    assert {"ground_plane", "paddock_fence", "water_trough", "field_shed", "riose_anchor"} <= models.keys()
    ground = models["ground_plane"]
    assert ground is not None
    assert ground.find("link/collision/geometry/plane") is not None

    fence = models["paddock_fence"]
    assert fence is not None
    collisions = {collision.get("name") for collision in fence.findall("link/collision")}
    visuals = {visual.get("name") for visual in fence.findall("link/visual")}
    for rail in ("rail_lower", "rail_upper", "far_rail_lower", "far_rail_upper",
                 "end_rail_left_lower", "end_rail_left_upper",
                 "end_rail_right_lower", "end_rail_right_upper"):
        assert rail in visuals
        assert f"{rail}_collision" in collisions


def test_heavy_tag_scenario_changes_the_copied_runtime_asset(tmp_path: Path) -> None:
    scenario = get_scenario("06_heavy_tag")
    model_root = _scenario_assets(scenario, tmp_path)
    tag = _xml(model_root / "riose_ear_tag/model.sdf").find("model")
    cow = _xml(model_root / "riose_cow/model.sdf").find("model")
    assert tag is not None and cow is not None
    assert float(tag.findtext("link/inertial/mass", "0")) == pytest.approx(0.04)
    include_pose = cow.find("include/pose")
    assert include_pose is not None
    assert tuple(map(float, include_pose.text.split()[:3])) == scenario.attachment_position_m


def test_heavy_tag_scenario_has_rest_after_movement_for_firmware_to_settle() -> None:
    scenario = get_scenario("06_heavy_tag")
    assert scenario.phases[-1].behavior == "STANDING"
    assert scenario.phases[-1].duration_s >= 2.0


def test_gazebo_bridge_uses_sim_time_and_converts_si_acceleration(tmp_path: Path) -> None:
    source = tmp_path / "gazebo.jsonl"
    messages = [
        {"header": {"stamp": {"sec": "4", "nsec": "100000000"}},
         "linearAcceleration": {"x": 9.80665, "y": 0, "z": -9.80665}},
        {"header": {"stamp": {"sec": "4", "nsec": "120000000"}},
         "linearAcceleration": {"x": 0, "y": 4.903325, "z": 9.80665}},
    ]
    source.write_text("".join(json.dumps(message) + "\n" for message in messages))

    rows = parse_trace(source)
    rate, acceleration_g = normalize_trace(rows, None)
    assert rate == Decimal(50)
    assert acceleration_g == [
        (Decimal(1), Decimal(0), Decimal(-1)),
        (Decimal(0), Decimal("0.5"), Decimal(1)),
    ]

    manifest_path = convert_trace(source, tmp_path / "converted")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["provenance"] == "SIMULATED"
    assert manifest["datasets"][0]["name"] == "GAZEBO"
    assert Decimal(manifest["datasets"][0]["source_time_origin_s"]) == Decimal("4.1")
    assert manifest["datasets"][0]["sample_rate_hz"] == 50.0
    assert "not measured" in manifest["description"].lower()


def test_gazebo_bridge_rejects_nonuniform_timestamps_instead_of_claiming_fixed_rate() -> None:
    irregular = [
        (Decimal("2.321"), (Decimal(0), Decimal(0), Decimal(9))),
        (Decimal("2.340"), (Decimal(0), Decimal(0), Decimal(9))),
        (Decimal("2.360"), (Decimal(0), Decimal(0), Decimal(9))),
    ]
    with pytest.raises(ValueError, match="timestamp interval"):
        normalize_trace(irregular, None)


def test_gazebo_bridge_marks_interpolated_cadence_when_resampling(tmp_path: Path) -> None:
    source = tmp_path / "jittered-gazebo.jsonl"
    messages = [
        {"header": {"stamp": {"sec": "2", "nsec": str(nanos)}},
         "linearAcceleration": {"x": float(index), "y": 0, "z": 9.80665}}
        for index, nanos in enumerate((321_000_000, 340_000_000, 360_000_000))
    ]
    source.write_text("".join(json.dumps(message) + "\n" for message in messages))

    manifest_path = convert_trace(source, tmp_path / "resampled", resample_rate_hz=Decimal(50))
    manifest = json.loads(manifest_path.read_text())
    dataset = manifest["datasets"][0]
    assert dataset["sample_rate_hz"] == 50.0
    assert dataset["transformations"] == [{
        "method": "linear_interpolation_to_uniform_cadence",
        "status": "INTERPOLATED",
        "source_samples": 3,
        "source_sample_interval_min_s": "0.019",
        "source_sample_interval_max_s": "0.02",
        "target_sample_rate_hz": 50.0,
    }]


def test_gazebo_bridge_accepts_omitted_zero_seconds_in_protobuf_json(tmp_path: Path) -> None:
    source = tmp_path / "first-second.jsonl"
    source.write_text("".join(json.dumps({
        "header": {"stamp": {"nsec": nanos}},
        "linearAcceleration": {"x": 0, "y": 0, "z": 9.80665},
    }) + "\n" for nanos in (0, 20_000_000)))
    rows = parse_trace(source)
    assert rows[0][0] == Decimal(0)
    assert rows[1][0] == Decimal("0.02")
