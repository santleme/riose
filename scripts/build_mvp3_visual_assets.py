#!/usr/bin/env python3
"""Generate deterministic, procedural visual meshes for the MVP3 Gazebo twin.

Cow geometry is authored by RIOSE for this simulation (no external asset or
license dependency). Tag enclosure geometry is regenerated from the MVP2
mechanical specification with CadQuery; both sources are documented in the
MVP3 asset README.
"""
from __future__ import annotations

import math
import random
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
MVP3 = ROOT / "src/riose/products/ear_tag/mvp3"
COW = MVP3 / "gazebo/models/riose_cow/meshes"
TAG = MVP3 / "gazebo/models/riose_ear_tag/meshes"
SIZE = 512


def coat_texture(path: Path, base=(226, 222, 210), patches=True) -> None:
    image = Image.new("RGB", (SIZE, SIZE // 2), base)
    draw = ImageDraw.Draw(image)
    if patches:
        # Fixed irregular Holstein-like coat islands on the torso UV, not a
        # photographic claim. Seeded coordinates keep regeneration stable.
        for cx, cy, rx, ry, seed in ((175, 220, 100, 175, 3), (370, 95, 115, 90, 7),
                                     (690, 335, 130, 185, 12), (900, 125, 80, 105, 19)):
            points = []
            for i in range(28):
                angle = 2 * math.pi * i / 28
                wobble = 0.78 + 0.19 * math.sin((i + seed) * 2.31) + 0.08 * math.cos(i * 3.7 + seed)
                points.append((cx + rx * wobble * math.cos(angle), cy + ry * wobble * math.sin(angle)))
            draw.polygon(points, fill=(39, 39, 42))
    # Low-amplitude seeded coat variation avoids a perfectly flat albedo.
    px = image.load()
    for y in range(image.height):
        for x in range(image.width):
            d = ((x * 37 + y * 101 + (x*y) * 3) % 13) - 6
            r, g, b = px[x, y]
            px[x, y] = (max(0,min(255,r+d)), max(0,min(255,g+d)), max(0,min(255,b+d)))
    image.save(path)


def surface_maps(directory: Path, stem: str, *, color=(96, 125, 65)) -> None:
    normal = Image.new("RGB", (256, 256))
    albedo = Image.new("RGB", (256, 256), color)
    roughness = Image.new("L", (256, 256), 218)
    np, ap, rp = normal.load(), albedo.load(), roughness.load()
    for y in range(256):
        for x in range(256):
            grain = ((x * 17 + y * 29 + x*y*7) % 17) - 8
            np[x,y] = (126 + grain//3, 128 + ((x*11+y*7)%9)-4, 255)
            r,g,b = color
            ap[x,y] = (max(0,min(255,r+grain)), max(0,min(255,g+grain)), max(0,min(255,b+grain)))
            rp[x,y] = 205 + ((x*7+y*13)%28)
    albedo.save(directory / f"{stem}_albedo.png")
    normal.save(directory / f"{stem}_normal.png")
    roughness.save(directory / f"{stem}_roughness.png")


def grass_mesh(path: Path) -> None:
    rng = random.Random(20261005)
    vertices, uvs, faces = [], [], []
    for _ in range(1100):
        x, y = rng.uniform(-14, 14), rng.uniform(-11, 11)
        # Keep the animal path and camera foreground uncluttered.
        if -1.3 <= y <= 1.3 and -1.0 <= x <= 9.0:
            continue
        for blade in range(3):
            bx, by = x + rng.uniform(-.12,.12), y + rng.uniform(-.12,.12)
            height, width = rng.uniform(.12,.32), rng.uniform(.018,.038)
            lean = rng.uniform(-.10,.10)
            base = len(vertices) + 1
            vertices.extend(((bx-width,by,0),(bx+width,by,0),(bx+lean,by+height*.12,height)))
            uvs.extend(((0,0),(1,0),(.5,1)))
            faces.append((base,base+1,base+2))
    write_obj(path, vertices, uvs, faces)
    # Separate low-cost albedo/normal maps for the single batched grass mesh.
    surface_maps(path.parent, "grass", color=(72,105,48))
    (path.parent / f"{path.stem}.mtl").write_text(
        "newmtl coat\nKa 0.24 0.34 0.18\nKd 0.32 0.48 0.20\nKs 0.02 0.02 0.02\nNs 8\nmap_Kd grass_albedo.png\n",
        encoding="ascii")


def soil_ground(path: Path) -> None:
    # A finite rendered mesh replaces Gazebo's checkerboard plane appearance;
    # the collision surface remains the existing broad stable physics plane.
    verts = [(-100,-100,0),(100,-100,0),(100,100,0),(-100,100,0)]
    with path.open("w", encoding="ascii") as out:
        out.write("mtllib soil_ground.mtl\nusemtl soil\ns 1\n")
        for x,y,z in verts: out.write(f"\nv {x} {y} {z}")
        out.write("\nvt 0 0\nvt 24 0\nvt 24 24\nvt 0 24\nvn 0 0 1\nf 1/1/1 2/2/1 3/3/1 4/4/1\n")
    surface_maps(path.parent, "soil", color=(117,105,76))
    (path.parent / "soil_ground.mtl").write_text(
        "newmtl soil\nKa 0.36 0.32 0.22\nKd 0.46 0.41 0.29\nKs 0.02 0.02 0.02\nNs 8\nmap_Kd soil_albedo.png\n",
        encoding="ascii")


def mesh_x_loft(path: Path, sections: list[tuple[float, float, float, float]], sides=48) -> None:
    """Write a smooth longitudinal elliptical loft with OBJ UV coordinates."""
    verts, uvs, faces = [], [], []
    for index, (x, ry, rz, zc) in enumerate(sections):
        u = index / (len(sections) - 1)
        for side in range(sides + 1):
            angle = 2 * math.pi * side / sides
            verts.append((x, ry * math.cos(angle), zc + rz * math.sin(angle)))
            uvs.append((u, side / sides))
    for ring in range(len(sections) - 1):
        for side in range(sides):
            a = ring * (sides + 1) + side + 1
            b = a + 1
            c = (ring + 1) * (sides + 1) + side + 1
            d = c + 1
            faces.extend(((a, c, b), (b, c, d)))
    write_obj(path, verts, uvs, faces)


def mesh_z_loft(path: Path, sections: list[tuple[float, float, float, float]], sides=40) -> None:
    """Write rings along Z as (z, rx, ry, x_center)."""
    verts, uvs, faces = [], [], []
    for index, (z, rx, ry, xc) in enumerate(sections):
        u = index / (len(sections) - 1)
        for side in range(sides + 1):
            angle = 2 * math.pi * side / sides
            verts.append((xc + rx * math.cos(angle), ry * math.sin(angle), z))
            uvs.append((u, side / sides))
    for ring in range(len(sections) - 1):
        for side in range(sides):
            a = ring * (sides + 1) + side + 1
            b = a + 1
            c = (ring + 1) * (sides + 1) + side + 1
            d = c + 1
            faces.extend(((a, c, b), (b, c, d)))
    write_obj(path, verts, uvs, faces)


def write_obj(path: Path, vertices, uvs, faces) -> None:
    normals = [[0.0, 0.0, 0.0] for _ in vertices]
    for face in faces:
        a, b, c = (vertices[index - 1] for index in face[:3])
        u = (b[0]-a[0], b[1]-a[1], b[2]-a[2])
        v = (c[0]-a[0], c[1]-a[1], c[2]-a[2])
        normal = (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
        for vertex in face:
            for axis in range(3):
                normals[vertex - 1][axis] += normal[axis]
    smooth_normals = []
    for normal in normals:
        length = math.sqrt(sum(component * component for component in normal)) or 1.0
        smooth_normals.append(tuple(component / length for component in normal))
    with path.open("w", encoding="ascii") as out:
        out.write(f"mtllib {path.stem}.mtl\nusemtl coat\ns 1\n")
        out.writelines(f"v {x:.7f} {y:.7f} {z:.7f}\n" for x, y, z in vertices)
        out.writelines(f"vt {u:.7f} {v:.7f}\n" for u, v in uvs)
        out.writelines(f"vn {x:.7f} {y:.7f} {z:.7f}\n" for x, y, z in smooth_normals)
        out.writelines("f " + " ".join(f"{v}/{v}/{v}" for v in face) + "\n" for face in faces)
    (path.parent / f"{path.stem}.mtl").write_text(
        "newmtl coat\nKa 0.7 0.7 0.7\nKd 0.9 0.9 0.9\nKs 0.05 0.05 0.05\nNs 12\n", encoding="ascii")


def build_cow() -> None:
    COW.mkdir(parents=True, exist_ok=True)
    coat_texture(COW / "cow_coat.png")
    coat_texture(COW / "cow_skin.png", (183, 112, 104), patches=False)
    surface_maps(COW, "cow", color=(226,222,210))
    mesh_x_loft(COW / "torso.obj", [(-.72,.025,.035,0),(-.63,.19,.25,0),(-.45,.27,.34,0),(-.18,.30,.38,0),(.16,.29,.37,0),(.43,.25,.34,0),(.62,.18,.27,0),(.72,.025,.04,0)])
    mesh_z_loft(COW / "neck.obj", [(-.52,.06,.07,0),(-.43,.15,.15,0),(-.20,.22,.21,0),(0,.25,.23,0),(.20,.20,.19,0),(.40,.14,.14,0),(.52,.05,.06,0)])
    mesh_x_loft(COW / "head.obj", [(-.28,.035,.05,0),(-.21,.14,.19,0),(-.08,.20,.25,0),(.08,.20,.22,0),(.22,.17,.16,-.02),(.35,.19,.12,-.07),(.43,.10,.075,-.09),(.47,.025,.025,-.09)])
    # Anatomical leg loft: taper at the fetlock, fuller upper limb and a mild
    # hock/knee bend. Collision remains the existing stable cylinder proxy.
    mesh_z_loft(COW / "leg.obj", [(-.62,.050,.050,.025),(-.55,.055,.055,.015),
                                  (-.43,.070,.065,-.015),(-.30,.082,.072,-.028),
                                  (-.16,.105,.090,0),(-.03,.115,.095,.015)])
    # Low-poly rounded cloven-hoof silhouette, shared by the four visual legs.
    mesh_z_loft(COW / "hoof.obj", [(-.05,.073,.057,0),(-.035,.082,.064,0),
                                   (.025,.082,.064,0),(.05,.060,.052,0)], sides=24)
    # Leaf-shaped ear mesh extends along local +Y from its physical hinge.
    verts, uvs, faces = [], [], []
    stations = [(0,.045),(.035,.085),(.085,.115),(.14,.095),(.19,.012)]
    sides = 20
    for i, (y, half) in enumerate(stations):
        for j in range(sides + 1):
            a = 2 * math.pi * j / sides
            verts.append((-.035 + half * math.cos(a), y, .012 + .018 * math.sin(a)))
            uvs.append((j/sides, i/(len(stations)-1)))
    for i in range(len(stations)-1):
        for j in range(sides):
            a=i*(sides+1)+j+1; b=a+1; c=(i+1)*(sides+1)+j+1; d=c+1
            faces.extend(((a,c,b),(b,c,d)))
    write_obj(COW / "ear.obj", verts, uvs, faces)
    for stem, texture in (("torso", "cow_coat.png"), ("neck", "cow_coat.png"),
                          ("head", "cow_coat.png"), ("leg", "cow_coat.png"),
                          ("ear", "cow_skin.png")):
        material = COW / f"{stem}.mtl"
        material.write_text(
            f"newmtl coat\nKa 0.62 0.60 0.56\nKd 0.88 0.86 0.82\nKs 0.025 0.025 0.025\nNs 9\nmap_Kd {texture}\n",
            encoding="ascii")
    soil_ground(COW / "soil_ground.obj")


def build_tag_mesh() -> None:
    """Export only the external CadQuery enclosure solid as Gazebo STL."""
    try:
        import cadquery as cq
    except ImportError as exc:
        raise SystemExit("CadQuery is required; run scripts/setup_mvp3_visual.sh with the MVP2 toolchain") from exc
    report_path = ROOT / "results/mvp2/mechanical/geometry.json"
    if not report_path.is_file():
        raise SystemExit(f"MVP2 geometry report missing: {report_path}")
    import json
    report = json.loads(report_path.read_text(encoding="utf-8"))
    w, h, t = (float(report["envelope_mm"][key]) for key in ("width", "height", "thickness"))
    cavity = report["internal_cavity_mm"]
    outer = cq.Workplane("XY").box(w, h, t, centered=(True, True, False))
    inner = cq.Workplane("XY").box(float(cavity["width"]), float(cavity["height"]), float(cavity["thickness"]), centered=(True, True, False)).translate((0, 0, (t-float(cavity["thickness"]))/2))
    hole = report["mounting_hole"]
    cutter = cq.Workplane("XY").circle(float(hole["diameter_mm"])/2).extrude(t+2).translate((float(hole["center_mm"]["x"]), float(hole["center_mm"]["y"]), -1))
    shell = outer.cut(inner).cut(cutter)
    TAG.mkdir(parents=True, exist_ok=True)
    cq.exporters.export(shell, str(TAG / "ear_tag_assumed.stl"), exportType="STL")
    # A cool slate polymer tone keeps the enclosure legible in the dim paddock
    # and distinguishes it from the cow's dark coat in product close-ups.
    surface_maps(TAG, "polymer", color=(126,151,158))
    logo = ROOT / "src/riose/products/livestock_tracking/adapters/static/assets/riose-mark.png"
    if logo.is_file():
        shutil.copy2(logo, TAG / "riose-mark.png")
    (TAG / "geometry-provenance.txt").write_text(
        "Source: hardware/mechanical/model.py current MVP2 geometry report\n"
        "Inputs: results/mvp2/mechanical/geometry.json\n"
        "Author: RIOSE project contributors; generated deterministically by scripts/build_mvp3_visual_assets.py\n"
        "Reference: local repository source; no third-party mesh or texture URL was used.\n"
        "License: no standalone SPDX license found in this checkout; generated project asset, rights remain unspecified.\n"
        "Modifications: external enclosure shell exported from MVP2 CadQuery geometry; cool-slate PBR base color and scalar roughness used because STL has no UVs.\n"
        "Dimensions/materials/mass remain ASSUMED; physical validation not performed.\n",
        encoding="utf-8")


def main() -> int:
    build_cow()
    build_tag_mesh()
    grass_mesh(COW / "grass_patch.obj")
    print(f"Generated procedural bovine and MVP2 enclosure meshes under {MVP3 / 'gazebo/models'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
