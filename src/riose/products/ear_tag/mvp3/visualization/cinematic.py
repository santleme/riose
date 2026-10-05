"""Gazebo GUI cameras, lighting presets, and repeatable screenshot capture."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ..cli import GAZEBO_DIR, MODEL_PATH, WORLD, _prepare_env, _ros_environment, _gz

VIS_DIR = Path(__file__).parent
CAMERA_FILE = VIS_DIR / "camera_presets.json"
SEQUENCE_FILE = VIS_DIR / "sequences.json"
LIGHT_FILE = VIS_DIR / "lighting_presets.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[6] / "results" / "mvp3" / "visual"
DEFAULT_GUI_CONFIGS = [Path.home() / ".gz" / "sim" / "8" / "gui.config",
                       Path("/opt/ros/jazzy/opt/gz_sim_vendor/share/gz/gz-sim8/gui/gui.config")]
SHOT_PRESETS = ["CAM_TAG_HERO", "CAM_TAG_MACRO", "CAM_CATTLE_TAG_CLOSE",
                "CAM_FIELD_WIDE", "CAM_GATEWAY", "CAM_TECHNICAL"]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _quaternion(position: list[float], target: list[float]) -> tuple[float, float, float, float]:
    dx, dy, dz = (target[i] - position[i] for i in range(3))
    horizontal = math.hypot(dx, dy)
    yaw = math.atan2(dy, dx)
    pitch = math.atan2(-dz, horizontal)
    cr, sr = 1.0, 0.0
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return (sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
            cr * cp * cy + sr * sp * sy)


def _camera_request(camera: dict) -> str:
    p = camera["position"]
    q = _quaternion(p, camera["look_at"])
    return ("pose { position { x: %.9g y: %.9g z: %.9g } "
            "orientation { x: %.12g y: %.12g z: %.12g w: %.12g } }" % (*p, *q))


def _write_gui_config(path: Path, *, width: int, height: int, camera: dict,
                      fov: float, lighting: dict) -> None:
    p = camera["position"]
    q = _quaternion(p, camera["look_at"])
    r, pitch, yaw = _euler(q)
    source = next((candidate for candidate in DEFAULT_GUI_CONFIGS if candidate.is_file()), None)
    if source is None:
        raise RuntimeError("Gazebo's default GUI config is unavailable; it provides the scene manager required to render model entities")
    source_text = source.read_text(encoding="utf-8")
    source_text = re.sub(r"^\s*<\?xml[^>]*\?>", "", source_text, count=1)
    root = ET.fromstring("<gazebo_gui_config>" + source_text + "</gazebo_gui_config>",
                         parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    window = root.find("window")
    if window is None or window.find("width") is None or window.find("height") is None:
        raise RuntimeError(f"Gazebo GUI config has no window dimensions: {source}")
    window.find("width").text = str(width)
    window.find("height").text = str(height)
    minimal_scene = root.find("plugin[@filename='MinimalScene']")
    if minimal_scene is None:
        raise RuntimeError(f"Gazebo GUI config has no MinimalScene plugin: {source}")
    values = {
        "ambient_light": " ".join(f"{v:g}" for v in lighting["ambient"]),
        "background_color": " ".join(f"{v:g}" for v in lighting["background"]),
        "camera_pose": f"{p[0]} {p[1]} {p[2]} {r} {pitch} {yaw}",
        "horizontal_fov": f"{fov:g}",
        "anti_aliasing": "4",
    }
    for name, text in values.items():
        element = minimal_scene.find(name)
        if element is None:
            element = ET.SubElement(minimal_scene, name)
        element.text = text
    # The screenshot API captures the render camera, not the on-screen panels.
    for plugin in root.findall("plugin"):
        if plugin.get("filename") not in {"CameraTracking", "Screenshot"}:
            continue
        gui = plugin.find("gz-gui")
        if gui is None:
            gui = ET.SubElement(plugin, "gz-gui")
        properties = {item.get("key"): item for item in gui.findall("property")}
        for key, value in (("state", "floating"), ("x", "-1000"), ("y", "-1000"),
                           ("width", "1"), ("height", "1")):
            prop = properties.get(key)
            if prop is None:
                prop = ET.SubElement(gui, "property", {"key": key,
                                      "type": "string" if key == "state" else "double"})
            prop.text = value
    fragments = [ET.tostring(child, encoding="unicode") for child in root]
    path.write_text("<?xml version=\"1.0\"?>\n" + "\n".join(fragments) + "\n",
                    encoding="utf-8")


def _euler(q: tuple[float, float, float, float]) -> tuple[float, float, float]:
    x, y, z, w = q
    sinr = 2 * (w * x + y * z)
    cosr = 1 - 2 * (x * x + y * y)
    roll = math.atan2(sinr, cosr)
    sinp = 2 * (w * y - z * x)
    pitch = math.copysign(math.pi / 2, sinp) if abs(sinp) >= 1 else math.asin(sinp)
    siny = 2 * (w * z + x * y)
    cosy = 1 - 2 * (y * y + z * z)
    yaw = math.atan2(siny, cosy)
    return roll, pitch, yaw


def _world_for_lighting(source: Path, target: Path, preset: dict) -> None:
    tree = ET.parse(source, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    world = tree.getroot().find("world")
    if world is None:
        raise RuntimeError(f"world file has no <world>: {source}")
    scene = world.find("scene")
    sun = world.find("light[@name='sun']")
    if scene is None or sun is None:
        raise RuntimeError("lighting presets require <scene> and directional sun in the MVP3 world")
    for key, field in (("ambient", "ambient"), ("background", "background")):
        element = scene.find(field)
        if element is None:
            raise RuntimeError(f"MVP3 world scene is missing <{field}>")
        element.text = " ".join(f"{v:g}" for v in preset[key]) + " 1"
    for key in ("sun_diffuse", "sun_specular", "sun_direction"):
        element = sun.find(key.removeprefix("sun_"))
        if element is None:
            raise RuntimeError(f"MVP3 world sun is missing <{key.removeprefix('sun_')}>")
        element.text = " ".join(f"{v:g}" for v in preset[key]) + (" 1" if key != "sun_direction" else "")
    tree.write(target, encoding="utf-8", xml_declaration=True)


def _run_service(env: dict[str, str], executable: str, service: str, body: str,
                 timeout_ms: int = 3000) -> str:
    command = [executable, "service", "-s", service, "--reqtype", "gz.msgs.GUICamera"
               if service.endswith("/pose") else "gz.msgs.StringMsg",
               "--reptype", "gz.msgs.Boolean", "--timeout", str(timeout_ms), "--req", body]
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=timeout_ms / 1000 + 3)
    if result.returncode:
        raise RuntimeError(f"Gazebo service {service} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def _wait_services(env: dict[str, str], executable: str, process: subprocess.Popen,
                   timeout_s: float = 90) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Gazebo GUI exited early with status {process.returncode}")
        result = subprocess.run([executable, "service", "-l"], env=env, text=True, capture_output=True, timeout=8)
        if result.returncode == 0 and "/gui/screenshot" in result.stdout and "/gui/move_to/pose" in result.stdout:
            return
        time.sleep(1)
    raise RuntimeError("Gazebo camera and screenshot services did not become ready within 90 seconds")


def _capture_file(env: dict[str, str], executable: str, preset_name: str, camera: dict,
                  destination: Path, process: subprocess.Popen) -> dict:
    body = _camera_request(camera)
    response = _run_service(env, executable, "/gui/move_to/pose", body)
    if "data: true" not in response.lower():
        raise RuntimeError(f"Gazebo rejected camera preset {preset_name}: {response}")
    _wait_camera_pose(env, executable, camera, process)
    time.sleep(0.5)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    # The Gazebo 8 Screenshot implementation interprets StringMsg.data as a
    # directory, then writes a timestamped PNG inside it (despite the API page
    # saying the value is a filepath). Keep one private directory per shot.
    capture_dir = destination.parent / ".gazebo_capture" / destination.stem
    capture_dir.mkdir(parents=True, exist_ok=True)
    for previous in capture_dir.glob("*.png"):
        previous.unlink()
    # The first render after a camera move can contain only the sky while the
    # scene is synchronizing. Reject that valid-but-empty frame and request a
    # fresh one. The 12 KiB floor is well below full MVP3 frames but above the
    # observed flat background-only PNG (~5 KiB).
    last_size = 0
    for _attempt in range(12):
        if process.poll() is not None:
            raise RuntimeError(f"Gazebo GUI exited while capturing {preset_name}: {process.returncode}")
        response = _run_service(env, executable, "/gui/screenshot", f'data: "{capture_dir}"')
        if "data: true" not in response.lower():
            raise RuntimeError(f"Gazebo rejected screenshot request for {destination}: {response}")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and process.poll() is None:
            generated = sorted(capture_dir.glob("*.png"), key=lambda item: item.stat().st_mtime_ns,
                               reverse=True)
            if generated and generated[0].is_file():
                last_size = generated[0].stat().st_size
                if last_size >= 12_000:
                    with generated[0].open("rb") as stream:
                        header = stream.read(24)
                        if header[:8] == b"\x89PNG\r\n\x1a\n":
                            image_width, image_height = struct.unpack(">II", header[16:24])
                            os.replace(generated[0], destination)
                            return {"camera": preset_name, "file": str(destination),
                                    "size_bytes": destination.stat().st_size,
                                    "resolution": [image_width, image_height],
                                    "capture_status": "CAPTURED"}
            time.sleep(0.25)
        time.sleep(0.25)
    raise RuntimeError(f"Gazebo did not produce a non-empty rendered frame for {preset_name}; "
                       f"latest PNG size was {last_size} bytes: {destination}")


def _wait_camera_pose(env: dict[str, str], executable: str, camera: dict,
                      process: subprocess.Popen, timeout_s: float = 25) -> None:
    expected_pos = camera["position"]
    expected_q = _quaternion(camera["position"], camera["look_at"])
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Gazebo GUI exited while moving camera: {process.returncode}")
        result = subprocess.run([executable, "topic", "-e", "-t", "/gui/camera/pose",
                                 "-n", "1", "--json-output"],
                                env=env, text=True, capture_output=True, timeout=5)
        if result.returncode == 0:
            try:
                pose = json.loads(result.stdout)
                pos = pose.get("position", {})
                q = pose.get("orientation", {})
                got_pos = [float(pos.get(axis, 0.0)) for axis in ("x", "y", "z")]
                got_q = [float(q.get(axis, 0.0)) for axis in ("x", "y", "z", "w")]
                position_error = math.sqrt(sum((got_pos[i] - expected_pos[i]) ** 2 for i in range(3)))
                orientation_dot = abs(sum(got_q[i] * expected_q[i] for i in range(4)))
                if position_error < 0.02 and orientation_dot > 0.999:
                    return
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        time.sleep(0.2)
    raise RuntimeError(f"Gazebo accepted camera preset but did not reach its pose within {timeout_s:g}s")


def _wait_scene_models(env: dict[str, str], executable: str, process: subprocess.Popen,
                       timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Gazebo exited before scene synchronization: {process.returncode}")
        result = subprocess.run([executable, "topic", "-e", "-t", "/world/riose_mvp3/pose/info",
                                 "-n", "1", "--json-output"],
                                env=env, text=True, capture_output=True, timeout=6)
        if result.returncode == 0:
            try:
                names = {item.get("name") for item in json.loads(result.stdout).get("pose", [])}
                if {"riose_cow", "riose_ear_tag", "riose_anchor"} <= names:
                    # Let the GUI SceneManager finish mirroring those entities.
                    time.sleep(1.5)
                    return
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        time.sleep(0.4)
    raise RuntimeError("MVP3 cow/tag/receiver poses did not appear on the Gazebo world pose topic")


def capture(*, preset_names: list[str], sequence: str | None = None,
            lighting: str = "DAY", output: Path = DEFAULT_OUTPUT,
            width: int = 1920, height: int = 1080, keep_open: bool = False) -> list[dict]:
    cameras = _load(CAMERA_FILE)["presets"]
    sequences = _load(SEQUENCE_FILE)["sequences"]
    lights = _load(LIGHT_FILE)["presets"]
    lighting = lighting.upper()
    if lighting not in lights:
        raise ValueError(f"unknown lighting preset {lighting}; choose {', '.join(lights)}")
    if sequence:
        sequence = sequence.lower()
        if sequence not in sequences:
            raise ValueError(f"unknown sequence {sequence}; choose {', '.join(sequences)}")
        names = [segment["camera"] for segment in sequences[sequence]]
    else:
        names = preset_names
    unknown = sorted(set(names) - set(cameras))
    if unknown:
        raise ValueError("unknown camera preset(s): " + ", ".join(unknown))

    env = _prepare_env(_ros_environment(), MODEL_PATH)
    executable = _gz(env)
    output.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="riose-mvp3-visual-") as scratch:
        scratch_path = Path(scratch)
        visual_world = scratch_path / "riose_mvp3_visual.sdf"
        _world_for_lighting(WORLD, visual_world, lights[lighting])
        for index, name in enumerate(names, 1):
            camera = cameras[name]
            config = scratch_path / f"gui-{index:02d}.config"
            _write_gui_config(config, width=width, height=height, camera=camera,
                              fov=float(camera["fov_deg"]), lighting=lights[lighting])
            command = [executable, "sim", "-r", "-v", "2", "--gui-config", str(config), str(visual_world)]
            log_path = output / f"gazebo_visual_{index:02d}_{name.lower()}.log"
            with log_path.open("w", encoding="utf-8") as log_stream:
                process = subprocess.Popen(command, env=env, stdout=log_stream, stderr=subprocess.STDOUT,
                                           text=True, start_new_session=True)
                try:
                    _wait_services(env, executable, process)
                    _wait_scene_models(env, executable, process)
                    filename = f"{index:02d}_{name.lower().replace('cam_', '')}_{lighting.lower()}.png"
                    shot = _capture_file(env, executable, name, camera, output / filename, process)
                    shot["fov_applied_deg"] = float(camera["fov_deg"])
                    if sequence:
                        shot["planned_duration_s"] = float(sequences[sequence][index - 1]["duration_s"])
                    manifest.append(shot)
                    if index == len(names):
                        (output / "capture_manifest.json").write_text(json.dumps({
                            "schema": "riose.mvp3.visual_capture/v1", "lighting": lighting,
                            "sequence": sequence,
                            "requested_gui_resolution": [width, height],
                            "rendered_resolutions": [list(size) for size in sorted(
                                {tuple(item["resolution"]) for item in manifest})],
                            "fov_applied_deg": sorted({item["fov_applied_deg"] for item in manifest}),
                            "camera_presets": manifest,
                            "limitations": ["Each still reloads the same world and applies that preset's horizontal FOV.",
                                            "Captures are deterministic stills; video encoding and continuous camera motion are not included.",
                                            "The scene remains the functional MVP3 world; product close-ups show the tag attached to the cow."],
                        }, indent=2), encoding="utf-8")
                except Exception:
                    log_stream.flush()
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    tail = "\n".join(lines[-35:])
                    print(f"Gazebo log tail is in {log_path}\n{tail}", file=sys.stderr)
                    raise
                finally:
                    if keep_open and index == len(names) and process.poll() is None:
                        print("Gazebo GUI is open. Press Ctrl-C in this terminal to close it.", flush=True)
                        try:
                            process.wait()
                        except KeyboardInterrupt:
                            process.terminate()
                    elif process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=12)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
        return manifest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="riose mvp3 visual", description=__doc__)
    actions = result.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="list named camera presets and sequences")
    shots = actions.add_parser("capture", help="render named stills through Gazebo GUI")
    choice = shots.add_mutually_exclusive_group(required=True)
    choice.add_argument("--preset", choices=sorted(_load(CAMERA_FILE)["presets"]), action="append")
    choice.add_argument("--all", action="store_true", help="capture the six documented still presets")
    choice.add_argument("--sequence", choices=sorted(_load(SEQUENCE_FILE)["sequences"]))
    shots.add_argument("--lighting", default="DAY", choices=sorted(_load(LIGHT_FILE)["presets"]))
    shots.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    shots.add_argument("--width", type=int, default=1920)
    shots.add_argument("--height", type=int, default=1080)
    shots.add_argument("--keep-open", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.action == "list":
        for name, camera in _load(CAMERA_FILE)["presets"].items():
            print(f"{name}\tFOV {camera['fov_deg']}°\t{camera['description']}")
        for name, sequence in _load(SEQUENCE_FILE)["sequences"].items():
            duration = sum(item["duration_s"] for item in sequence)
            print(f"sequence:{name}\t{duration:g}s\t{len(sequence)} shots")
        return 0
    preset_names = (SHOT_PRESETS if args.all else args.preset or [])
    manifest = capture(preset_names=preset_names, sequence=args.sequence,
                       lighting=args.lighting, output=args.output,
                       width=args.width, height=args.height, keep_open=args.keep_open)
    for item in manifest:
        print(f"{item['capture_status']} {item['file']} ({item['size_bytes']} bytes)")
    print(f"manifest: {args.output / 'capture_manifest.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"MVP3 visual error: {exc}", file=sys.stderr)
        raise SystemExit(2)
