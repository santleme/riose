#!/usr/bin/env python3
"""Evidence-based visual gate for Phase B; requires actual Gazebo camera captures."""
from __future__ import annotations

import csv
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
MVP3 = ROOT / "src/riose/products/ear_tag/mvp3"
CAMERAS = ("overview", "follow", "head", "ear_tag_macro", "anchor", "ground_low")


def image_ok(path: Path) -> bool:
    image = cv2.imread(str(path)) if path.is_file() else None
    return image is not None and image.size > 0 and image.shape[0] >= 240 and image.shape[1] >= 320


def gate(experiment: Path) -> dict:
    checks: dict[str, dict] = {}
    world = ET.parse(MVP3 / "gazebo/worlds/riose_mvp3.sdf").getroot()
    sensor_names = {sensor.attrib.get("name") for sensor in world.iter("sensor")}
    sensor_names.update(
        sensor.attrib.get("name")
        for sensor in ET.parse(MVP3 / "gazebo/models/riose_cow/model.sdf").getroot().iter("sensor")
    )
    needed = {f"camera_{name}" for name in CAMERAS}
    needed.discard("camera_follow")
    needed.add("camera_follow")
    checks["ogre2_camera_rig"] = {"pass": needed <= sensor_names and "ogre2" in
        (MVP3 / "gazebo/worlds/riose_mvp3.sdf").read_text(encoding="utf-8").lower(),
        "cameras_present": sorted(needed & sensor_names), "required": sorted(needed)}

    cow = MVP3 / "gazebo/models/riose_cow"
    tag = MVP3 / "gazebo/models/riose_ear_tag"
    cow_sdf = (cow / "model.sdf").read_text(encoding="utf-8")
    tag_sdf = (tag / "model.sdf").read_text(encoding="utf-8")
    pbr = all(mark in cow_sdf and (cow / "meshes" / texture).is_file()
              for mark, texture in (("<albedo_map>", "cow_coat.png"),
                                    ("<normal_map>", "cow_normal.png"),
                                    ("<roughness_map>", "cow_roughness.png")))
    tag_material = ("<ambient>0.48 0.56 0.59 1</ambient>" in tag_sdf
                    and "<diffuse>0.72 0.80 0.82 1</diffuse>" in tag_sdf
                    and "<roughness>0.82</roughness>" in tag_sdf)
    checks["pbr_assets"] = {"pass": pbr and tag_material,
                            "cow_maps": ["albedo", "normal", "roughness"],
                            "tag_material": "explicit cool-slate PBR base color + scalar roughness (MVP2 STL has no UVs)"}

    manifest_path = experiment / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    capture_path = experiment / "media/capture_manifest.json"
    captures = json.loads(capture_path.read_text(encoding="utf-8")) if capture_path.is_file() else {}
    capture_counts = {}
    capture_pass = bool(manifest.get("visual_presentation", {}).get("renderer") == "OGRE2")
    for camera in CAMERAS:
        clip = experiment / "media" / f"{camera}.mp4"
        stamp_file = experiment / "media" / f"{camera}_timestamps.csv"
        count = 0
        if stamp_file.is_file():
            with stamp_file.open(encoding="utf-8", newline="") as stream:
                count = sum(1 for _ in csv.DictReader(stream))
        capture_counts[camera] = {"frames": count, "clip_bytes": clip.stat().st_size if clip.is_file() else 0}
        capture_pass = capture_pass and count >= 10 and clip.is_file() and clip.stat().st_size > 1000
    checks["live_ogre2_camera_capture"] = {"pass": capture_pass, "evidence": capture_counts,
                                             "capture_manifest": bool(captures)}

    media = ROOT / "results/mvp3/media"
    before, after, close = (media / name for name in ("before.png", "overview.png", "ear_tag_closeup.png"))
    screenshots = {path.name: {"valid": image_ok(path), "bytes": path.stat().st_size if path.is_file() else 0}
                   for path in (before, after, close)}
    checks["before_after_and_product_closeup"] = {"pass": all(v["valid"] for v in screenshots.values()),
                                                   "evidence": screenshots}
    passed = all(check["pass"] for check in checks.values())
    return {"schema_version": "riose.mvp3.phase-b-gate/v1",
            "gate": "PHASE_B_VISUAL_REALISM_PASS" if passed else "PHASE_B_VISUAL_REALISM_INCOMPLETE",
            "status": "SIMULATED", "checks": checks}


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} EXPERIMENT_DIR")
    experiment = Path(sys.argv[1]).resolve()
    result = gate(experiment)
    out = experiment / "phase-b-gate.json"
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["gate"] == "PHASE_B_VISUAL_REALISM_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
