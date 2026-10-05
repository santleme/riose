"""Command-line entry point for the MVP3 digital animal/ear-tag twin."""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import yaml
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import __version__
from .motion import to_gz_joint_trajectory
from .scenarios import SCENARIOS, Scenario, get_scenario


REPO_ROOT = Path(__file__).resolve().parents[5]
MVP3_DIR = Path(__file__).resolve().parent
GAZEBO_DIR = MVP3_DIR / "gazebo"
WORLD = GAZEBO_DIR / "worlds" / "riose_mvp3.sdf"
MODEL_PATH = GAZEBO_DIR / "models"
IMU_TOPIC = "/riose/mvp3/imu/data"
POSE_TOPIC = "/world/riose_mvp3/pose/info"
CONTACT_TOPIC = "/riose/mvp3/ear_tag/contact"
JOINT_TOPIC = "/model/riose_cow/joint_trajectory"
POSE_RECORD_RATE_HZ = 50.0


def _ros_environment() -> dict[str, str]:
    env = os.environ.copy()
    setup = Path("/opt/ros/jazzy/setup.bash")
    if not setup.is_file():
        return env
    command = ["bash", "-c", f"source {setup} >/dev/null 2>&1 && env -0"]
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, env=env)
    for item in result.stdout.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            env[key.decode()] = value.decode(errors="replace")
    return env


def _gz(env: dict[str, str]) -> str:
    executable = shutil.which("gz", path=env.get("PATH"))
    if not executable:
        raise RuntimeError("Gazebo not found. Run scripts/setup_mvp3.sh and install Harmonic per the official guide if needed.")
    result = subprocess.run([executable, "sim", "--version"], text=True, capture_output=True, env=env)
    if result.returncode or "version 8." not in result.stdout:
        raise RuntimeError(f"Expected Gazebo Sim 8 (Harmonic), got: {(result.stdout + result.stderr).strip()}")
    return executable


def _prepare_env(env: dict[str, str], model_path: Path = MODEL_PATH) -> dict[str, str]:
    paths = [str(model_path)]
    if env.get("GZ_SIM_RESOURCE_PATH"):
        paths.append(env["GZ_SIM_RESOURCE_PATH"])
    env["GZ_SIM_RESOURCE_PATH"] = os.pathsep.join(paths)
    env["SDF_PATH"] = os.pathsep.join(paths)
    return env


def _check_assets() -> None:
    required = [WORLD, MODEL_PATH / "riose_cow" / "model.sdf", MODEL_PATH / "riose_ear_tag" / "model.sdf"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("MVP3 Gazebo assets are incomplete: " + ", ".join(missing))


def _tag_attachment_config() -> dict:
    path = MVP3_DIR / "tag_attachment.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("provenance") != "ASSUMED":
        raise RuntimeError(f"invalid tag attachment configuration: {path}")
    return value


_QUALITY = {
    "low": (400, 225, 6),
    "medium": (640, 360, 8),
    "high": (800, 450, 10),
    "presentation": (960, 540, 12),
}


def _apply_camera_quality(root: Path, quality: str, *, enabled: bool = True) -> None:
    width, height, rate = _QUALITY[quality.lower()]
    for path in (root / "riose_cow" / "model.sdf",):
        tree = ET.parse(path)
        for sensor in list(tree.getroot().iter("sensor")):
            if sensor.get("type") != "camera":
                continue
            if not enabled:
                parent = next(node for node in tree.getroot().iter() if sensor in list(node))
                parent.remove(sensor)
                continue
            image = sensor.find("camera/image")
            update = sensor.find("update_rate")
            if image is not None:
                image.find("width").text = str(width)
                image.find("height").text = str(height)
            if update is not None:
                update.text = str(rate if enabled else 0)
            always = sensor.find("always_on")
            if always is not None:
                always.text = str(enabled).lower()
        tree.write(path, encoding="utf-8", xml_declaration=True)


def _scenario_assets(scenario: Scenario, target: Path, *, quality: str = "medium",
                     cameras_enabled: bool = True) -> Path:
    """Copy assets into the experiment and apply explicit tag mass/position inputs."""
    model_root = target / "sim_assets" / "models"
    for name in ("riose_cow", "riose_ear_tag"):
        shutil.copytree(MODEL_PATH / name, model_root / name)
    _apply_camera_quality(model_root, quality, enabled=cameras_enabled)
    tag_sdf = model_root / "riose_ear_tag" / "model.sdf"
    attachment = _tag_attachment_config()
    tag_tree = ET.parse(tag_sdf)
    tag_model = tag_tree.getroot().find("model")
    assert tag_model is not None
    inertial = tag_model.find("link/inertial")
    assert inertial is not None
    original_mass = float(attachment["mass_kg"])
    if original_mass <= 0:
        raise RuntimeError("ear-tag SDF mass must be positive")
    factor = (scenario.tag_mass_g / 1000.0) / original_mass
    inertial.find("mass").text = f"{scenario.tag_mass_g / 1000.0:.9g}"
    inertial_pose = inertial.find("pose")
    com = attachment["center_of_mass_m"]
    inertial_pose.text = " ".join(f"{float(value):.9g}" for value in com) + " 0 0 0"
    inertia = inertial.find("inertia")
    if inertia is not None:
        names = ("ixx", "iyy", "izz")
        for name, value in zip(names, attachment["inertia_kg_m2"]):
            inertia.find(name).text = f"{float(value) * factor:.12g}"
    tag_tree.write(tag_sdf, encoding="utf-8", xml_declaration=True)

    cow_sdf = model_root / "riose_cow" / "model.sdf"
    cow_tree = ET.parse(cow_sdf)
    cow_model = cow_tree.getroot().find("model")
    assert cow_model is not None
    include_pose = cow_model.find("include/pose")
    stud_joint = cow_model.find("joint[@name='ear_tag_stud_fixation']")
    stud_pose = stud_joint.find("pose") if stud_joint is not None else None
    attachment_joint = cow_model.find("joint[@name='riose_ear_tag_attachment']")
    attachment_pose = attachment_joint.find("pose") if attachment_joint is not None else None
    assert include_pose is not None and stud_pose is not None and attachment_pose is not None
    xyz = " ".join(f"{value:.9g}" for value in scenario.attachment_position_m)
    include_pose.text = f"{xyz} 1.570796 0 3.141593"
    stud_pose.text = f"{xyz} 0 0 0"
    attachment_pose.text = "0 0 0 0 0 0"
    axis = attachment_joint.find("axis")
    if axis is None:
        raise RuntimeError("ear-tag attachment joint has no axis")
    limit = axis.find("limit")
    dynamics = axis.find("dynamics")
    if limit is None or dynamics is None:
        raise RuntimeError("ear-tag attachment joint needs limit and dynamics")
    lower, upper = attachment["angular_limits_rad"]
    limit.find("lower").text = f"{max(float(lower), -abs(scenario.attachment_limit_rad)):.9g}"
    limit.find("upper").text = f"{min(float(upper), abs(scenario.attachment_limit_rad)):.9g}"
    dynamics.find("damping").text = f"{scenario.attachment_damping_nm_s_rad:.9g}"
    dynamics.find("spring_stiffness").text = f"{scenario.attachment_stiffness_nm_rad:.9g}"
    cow_tree.write(cow_sdf, encoding="utf-8", xml_declaration=True)
    return model_root


def _scenario_world(target: Path, *, fast_headless: bool, quality: str = "medium",
                    cameras_enabled: bool = True) -> Path:
    world_path = target / "sim_assets" / "riose_mvp3_runtime.sdf"
    tree = ET.parse(WORLD)
    world = tree.getroot().find("world")
    assert world is not None
    realtime = world.find("physics/real_time_factor")
    if realtime is None:
        raise RuntimeError("MVP3 world physics has no real_time_factor")
    if fast_headless:
        realtime.text = "0"
    # The standalone world lives below the experiment directory. Some Gazebo
    # GUI builds resolve model:// mesh URIs in world visuals relative to that
    # copied world before consulting GZ_SIM_RESOURCE_PATH, producing a doubled
    # experiment path. Model includes remain model://; make only these copied
    # world mesh/texture references explicit and reproducible.
    cow_meshes = (target / "sim_assets" / "models" / "riose_cow" / "meshes").resolve()
    cow_mesh_uri = cow_meshes.as_uri() + "/"
    for uri in world.iter("uri"):
        value = uri.text or ""
        prefix = "model://riose_cow/meshes/"
        if value.startswith(prefix):
            uri.text = cow_mesh_uri + value[len(prefix):]
    width, height, rate = _QUALITY[quality]
    for sensor in list(world.iter("sensor")):
        if sensor.get("type") != "camera":
            continue
        if not cameras_enabled:
            parent = next(node for node in world.iter() if sensor in list(node))
            parent.remove(sensor)
            continue
        image = sensor.find("camera/image")
        update = sensor.find("update_rate")
        always = sensor.find("always_on")
        if image is not None:
            image.find("width").text = str(width)
            image.find("height").text = str(height)
        if update is not None:
            update.text = str(rate if cameras_enabled else 0)
        if always is not None:
            always.text = str(cameras_enabled).lower()
    if not cameras_enabled:
        for plugin in list(world.findall("plugin")):
            if plugin.get("name") == "gz::sim::systems::Sensors":
                world.remove(plugin)
    tree.write(world_path, encoding="utf-8", xml_declaration=True)
    return world_path


def capture_camera(name: str, *, output: Path | None = None, quality: str = "high") -> Path:
    aliases = {"ear-tag": "ear_tag_macro", "ear_tag": "ear_tag_macro", "overview": "overview",
               "follow": "follow", "head": "head", "anchor": "anchor", "ground-low": "ground_low"}
    camera = aliases.get(name, name)
    camera_topics = {"overview", "follow", "head", "ear_tag_macro", "anchor", "ground_low"}
    if camera not in camera_topics:
        raise ValueError(f"unknown camera {name!r}; choose from overview, follow, head, ear-tag, anchor, ground-low")
    quality = quality.lower()
    if quality not in _QUALITY:
        raise ValueError(f"quality must be one of {', '.join(_QUALITY)}")
    target = output or (REPO_ROOT / "results" / "mvp3" / "media" / f"{camera}.png")
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="riose-mvp3-camera-") as temporary:
        root = Path(temporary)
        model_root = root / "models"
        for model in ("riose_cow", "riose_ear_tag"):
            shutil.copytree(MODEL_PATH / model, model_root / model)
        _apply_camera_quality(model_root, quality)
        width, height, rate = _QUALITY[quality]
        world_path = root / "riose_mvp3_camera.sdf"
        tree = ET.parse(WORLD)
        for sensor in tree.getroot().iter("sensor"):
            if sensor.get("type") != "camera":
                continue
            image = sensor.find("camera/image")
            update = sensor.find("update_rate")
            if image is not None:
                image.find("width").text = str(width)
                image.find("height").text = str(height)
            if update is not None:
                update.text = str(rate)
        tree.write(world_path, encoding="utf-8", xml_declaration=True)
        env = _prepare_env(_ros_environment(), model_root)
        executable = _gz(env)
        log_path = target.with_suffix(".gazebo.log")
        with log_path.open("w", encoding="utf-8") as log:
            server = subprocess.Popen([executable, "sim", "-s", "-r", "--headless-rendering",
                                       "-v", "2", str(world_path)], env=env,
                                      stdout=log, stderr=subprocess.STDOUT)
            try:
                capture = subprocess.run(["/usr/bin/python3", str(REPO_ROOT / "scripts" / "capture_mvp3_camera.py"),
                                          f"/riose/mvp3/camera/{camera}", str(target), "--timeout", "35"],
                                         env=env, text=True, capture_output=True, timeout=45)
                if capture.returncode:
                    tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-35:])
                    raise RuntimeError(f"camera capture failed: {capture.stderr.strip() or capture.stdout.strip()}\n{tail}")
            finally:
                server.terminate()
                try:
                    server.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=4)
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError(f"Gazebo did not produce camera image {target}")
    return target


def _publish(env: dict[str, str], executable: str, topic: str, msgtype: str, body: str) -> None:
    if topic == JOINT_TOPIC and msgtype == "gz.msgs.JointTrajectory":
        descriptor, filename = tempfile.mkstemp(prefix="riose-mvp3-trajectory-", suffix=".pb.txt")
        os.close(descriptor)
        source = Path(filename)
        try:
            source.write_text(body, encoding="utf-8")
            publisher = Path(__file__).parent / "bridge" / "publish_trajectory.py"
            python = Path("/usr/bin/python3")
            if not python.is_file():
                raise RuntimeError("system Python 3 is required for Gazebo Transport publishing")
            result = subprocess.run([str(python), str(publisher), topic, str(source)],
                                    env=env, text=True, capture_output=True, timeout=20)
            if result.returncode:
                raise RuntimeError(f"Gazebo trajectory publish failed: {result.stderr.strip() or result.stdout.strip()}")
            return
        finally:
            source.unlink(missing_ok=True)
    command = [executable, "topic", "-t", topic, "-m", msgtype, "-p", body]
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f"Gazebo topic publish failed for {topic}: {result.stderr.strip()}")


def _json_object_from_monitor_output(output: str, *, label: str) -> dict:
    """Decode one JSON object from Renode monitor output with command echo/prompt."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"Renode monitor response did not contain a {label} JSON object")


def _monitor_scalar(session, command: str) -> int | bool:
    """Read a scalar monitor value, ignoring the echoed command and prompt."""
    output = session.query(command)
    for line in reversed(output.splitlines()):
        value = line.strip()
        if value == "True":
            return True
        if value == "False":
            return False
        if re.fullmatch(r"0x[0-9a-fA-F]+", value):
            return int(value, 16)
        if re.fullmatch(r"-?[0-9]+", value):
            return int(value)
    raise RuntimeError(f"Renode monitor did not return a scalar value for {command}")


def _live_dashboard_callback(dashboard, session, duration_s: float):
    """Build the low-rate UI sampler; model state current as ASSUMED only."""
    from .rf import AnchorReceiver, logical_events_from_firmware_trace
    from .rf.renode_trace import convert_sx1262_trace
    from ..digital_twin.paths import DEFAULT_SPEC
    from ..digital_twin.power import _power_load_profile
    from ..digital_twin.spec import load_spec

    spec, _ = load_spec(DEFAULT_SPEC)
    loads = _power_load_profile(spec)["loads"]
    state_names = ["BOOT", "SELF_TEST", "SLEEP", "IMU_MONITORING",
                   "RF_TX", "RF_RX", "ALERT", "ERROR_RECOVERY"]
    behavior_names = ["STILL", "NORMAL", "ACTIVE", "ALERT"]
    anchor = AnchorReceiver("riose_anchor")
    last_poll_s = -1.0
    last_tx_done = 0
    previous_state = "BOOT"
    charge_uah = 0.0
    history: list[dict[str, float]] = []
    telemetry: dict[str, Any] = {}
    accepted_count = 0
    accounted_tx_indices: set[int] = set()

    def update(sim_time: Decimal, sample, renode_time: Decimal) -> None:
        nonlocal last_poll_s, last_tx_done, previous_state, charge_uah
        nonlocal telemetry, accepted_count
        if dashboard is None:
            return
        now = float(sim_time)
        if sample is not None:
            history.append({"x_g": float(sample.acceleration_g[0]),
                            "y_g": float(sample.acceleration_g[1]),
                            "z_g": float(sample.acceleration_g[2])})
            del history[:-120]
        status: dict[str, Any] = {
            "simulation_time_s": now,
            "renode_time_s": float(renode_time),
            "clock_error_s": now - float(renode_time),
            "imu_history": history,
            "status": "RUNNING",
            "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if sample is not None:
            status.update({
                "imu_g": dict(zip("xyz", (float(value) for value in sample.acceleration_g))),
                "orientation_xyzw": [float(value) for value in sample.orientation_xyzw],
            })
        if now - last_poll_s >= 0.95 or now >= duration_s - 1e-9:
            gpio = int(_monitor_scalar(session, "sysbus ReadDoubleWord 0x50000014"))
            state_code = ((gpio >> 0) & 1) | (((gpio >> 1) & 1) << 1) | (((gpio >> 3) & 1) << 2)
            state = state_names[state_code] if state_code < len(state_names) else "UNKNOWN"
            tx_count = int(_monitor_scalar(session, "sysbus.spi1.radio TxCount"))
            tx_done = int(_monitor_scalar(session, "sysbus.spi1.radio TxDoneCount"))
            dt = max(0.0, now - last_poll_s) if last_poll_s >= 0 else 0.0
            current_record = loads.get(f"state:{previous_state}", loads["state:BOOT"])["load_current_ma"]
            charge_uah += float(current_record) * dt / 3.6
            status.update({"firmware_state": state,
                           "radio_state": "TX_PENDING" if tx_count > tx_done else "IDLE",
                           "tx_count": tx_done,
                           "modeled_charge_uah": charge_uah,
                           "power_status": "PROVISIONAL_ASSUMED_MCU_STATE_LOADS; TX/IMU EVENT LOADS PENDING"})
            if tx_done != last_tx_done:
                trace_json = _json_object_from_monitor_output(
                    session.query("sysbus.spi1.radio TxTraceJson"), label="live SX1262 TX trace")
                firmware_records = convert_sx1262_trace(trace_json)
                events = logical_events_from_firmware_trace(firmware_records)
                accepted = [anchor.accept_logical_event(event) for event in events]
                accepted_entries = [entry for entry in accepted if entry is not None]
                accepted_count = len(anchor.log)
                for record in trace_json.get("records", []):
                    tx_index = int(record["tx_index"])
                    if tx_index in accounted_tx_indices:
                        continue
                    accounted_tx_indices.add(tx_index)
                    packet = bytes.fromhex(record["payload_hex"])
                    duration = max(0, int(record["tx_done_ns"]) - int(record["tx_start_ns"])) / 1e9
                    tx_load = loads["pair:TX_START:TX_DONE"]["load_current_ma"]
                    charge_uah += float(tx_load) * duration / 3.6
                    telemetry = {
                        "tag_id": int.from_bytes(packet[2:6], "little"),
                        "behavior_estimate": behavior_names[packet[1]] if packet[1] < len(behavior_names) else "UNKNOWN",
                        "battery_mv": int.from_bytes(packet[20:22], "little"),
                        "last_tx_time_s": int(record["tx_start_ns"]) / 1e9,
                    }
                last_tx_done = tx_done
                if accepted_entries:
                    entry = accepted_entries[-1]
                    status["last_anchor_event"] = {
                        "sequence": entry.sequence, "timestamp_s": entry.timestamp_us / 1e6,
                        "tag_id": entry.tag_id,
                    }
            status.update(telemetry)
            status["anchor_accept_count"] = accepted_count
            status["modeled_charge_uah"] = charge_uah
            last_poll_s = now
            previous_state = state
        dashboard.update(status)

    return update


def _unpause(env: dict[str, str], executable: str) -> None:
    """Start simulation only after topics and recorders are ready."""
    command = [executable, "service", "-s", "/world/riose_mvp3/control",
               "--reqtype", "gz.msgs.WorldControl", "--reptype", "gz.msgs.Boolean",
               "--timeout", "5000", "--req", "pause: false"]
    result = subprocess.run(command, env=env, text=True, capture_output=True,
                            timeout=8)
    if result.returncode or "data: true" not in result.stdout:
        raise RuntimeError(f"Could not start Gazebo world: {result.stdout.strip()} {result.stderr.strip()}")


def _pause_and_reset(env: dict[str, str], executable: str) -> None:
    """Establish simulation time zero before preparing a deterministic run.

    Gazebo may advance while its systems and transport topics initialize. A
    control request with only ``pause: true`` therefore leaves an arbitrary
    simulation-time origin. Resetting while paused makes trajectory and
    recording timestamps start from the same reproducible world state.
    """
    command = [executable, "service", "-s", "/world/riose_mvp3/control",
               "--reqtype", "gz.msgs.WorldControl", "--reptype", "gz.msgs.Boolean",
               "--timeout", "5000", "--req", "pause: true, reset: {all: true}"]
    result = subprocess.run(command, env=env, text=True, capture_output=True,
                            timeout=8)
    if result.returncode or "data: true" not in result.stdout:
        raise RuntimeError(f"Could not pause/reset Gazebo world: {result.stdout.strip()} {result.stderr.strip()}")


def _find_topic(env: dict[str, str], executable: str, topic: str, process: subprocess.Popen,
                timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Gazebo exited early with code {process.returncode}; see server.log")
        result = subprocess.run([executable, "topic", "-l"], env=env, text=True,
                                capture_output=True, timeout=5)
        if topic in result.stdout.splitlines():
            return
        time.sleep(0.2)
    raise RuntimeError(f"Gazebo topic was not advertised: {topic}")


def _start_recording(env: dict[str, str], executable: str, output: Path,
                     duration_s: float | None, topic: str, filename: str) -> subprocess.Popen:
    raw = output / filename
    stream = raw.open("w", encoding="utf-8")
    command = [executable, "topic", "-e", "--json-output", "-t", topic]
    if duration_s is not None:
        # Gazebo topic --duration is wall-clock time. A lockstep experiment can
        # take far longer on the host than its virtual duration, so those
        # recorders run until the parent closes them after reaching the end.
        command.extend(["--duration", f"{duration_s + 5.0:.3f}"])
    proc = subprocess.Popen(command, env=env, stdout=stream, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    proc._riose_record_stream = stream  # close the parent file handle after wait
    return proc


def _summarize_attachment_contacts(path: Path) -> dict:
    messages = 0
    tag_contacts = 0
    max_force = 0.0
    try:
        stream = path.open(encoding="utf-8")
    except OSError:
        return {"status": "UNAVAILABLE", "reason": "contact topic recording file is missing"}
    with stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            messages += 1
            contacts = value.get("contact", value.get("contacts", []))
            if isinstance(contacts, dict):
                contacts = [contacts]
            if not isinstance(contacts, list):
                continue
            for contact in contacts:
                if not isinstance(contact, dict):
                    continue
                names = f"{contact.get('collision1', '')} {contact.get('collision2', '')}"
                if "riose_ear_tag" not in names:
                    continue
                tag_contacts += 1
                wrenches = contact.get("wrench", [])
                if isinstance(wrenches, dict):
                    wrenches = [wrenches]
                for wrench in wrenches if isinstance(wrenches, list) else []:
                    for body in (wrench.get("body_1_wrench", {}), wrench.get("body_2_wrench", {})):
                        force = body.get("force", {}) if isinstance(body, dict) else {}
                        if isinstance(force, dict):
                            norm = math.sqrt(sum(float(force.get(axis, 0.0))**2 for axis in "xyz"))
                            max_force = max(max_force, norm)
    if messages == 0:
        return {"status": "UNAVAILABLE", "reason": "contact topic produced no parseable messages"}
    return {"status": "SIMULATED_MEASURED", "message_count": messages,
            "tag_contact_count": tag_contacts,
            "peak_contact_force_n": max_force if tag_contacts else None,
            "force_provenance": "GAZEBO_CONTACT_SENSOR" if tag_contacts else "NO_TAG_CONTACT_OBSERVED"}


def run_experiment(name: str, *, visual: bool, output: Path | None = None,
                   seed: int | None = None, sample_period_s: float = 0.05,
                   tag_mass_g: float | None = None,
                   attachment_position_m: tuple[float, float, float] | None = None,
                   attachment_stiffness_nm_rad: float | None = None,
                   attachment_damping_nm_s_rad: float | None = None,
                   attachment_limit_rad: float | None = None,
                   duration_s: float | None = None,
                   firmware_trace: Path | None = None,
                   fast_headless: bool = False,
                   live_lockstep: bool = False,
                   quality: str = "medium", overlay: bool = True,
                   capture_video: bool = False) -> Path:
    quality = quality.lower()
    if quality not in _QUALITY:
        raise ValueError(f"quality must be one of {', '.join(_QUALITY)}")
    scenario = get_scenario(name)
    if any(value is not None for value in (seed, tag_mass_g, attachment_position_m,
                                           attachment_stiffness_nm_rad,
                                           attachment_damping_nm_s_rad,
                                           attachment_limit_rad)):
        scenario = Scenario(
            name=scenario.name, description=scenario.description, phases=scenario.phases,
            tag_mass_g=tag_mass_g if tag_mass_g is not None else scenario.tag_mass_g,
            attachment_position_m=(attachment_position_m if attachment_position_m is not None
                                  else scenario.attachment_position_m),
            attachment_stiffness_nm_rad=(attachment_stiffness_nm_rad if attachment_stiffness_nm_rad is not None
                                         else scenario.attachment_stiffness_nm_rad),
            attachment_damping_nm_s_rad=(attachment_damping_nm_s_rad if attachment_damping_nm_s_rad is not None
                                         else scenario.attachment_damping_nm_s_rad),
            attachment_limit_rad=(attachment_limit_rad if attachment_limit_rad is not None
                                  else scenario.attachment_limit_rad),
            seed=seed if seed is not None else scenario.seed)
    if duration_s is not None:
        if not math.isfinite(duration_s) or duration_s <= 0 or duration_s > scenario.duration_s:
            raise ValueError(f"duration_s must be finite and in (0, {scenario.duration_s:g}]")
        remaining = duration_s
        phases = []
        for phase in scenario.phases:
            if remaining <= 0:
                break
            phase_duration = min(phase.duration_s, remaining)
            phases.append(phase.__class__(phase_duration, phase.behavior,
                                          phase.forward_speed_m_s, phase.leg_frequency_hz,
                                          phase.head_amplitude_rad, phase.ear_amplitude_rad))
            remaining -= phase_duration
        scenario = Scenario(
            name=scenario.name, description=f"{scenario.description} (truncated to {duration_s:g}s)",
            phases=tuple(phases), tag_mass_g=scenario.tag_mass_g,
            attachment_position_m=scenario.attachment_position_m,
            attachment_stiffness_nm_rad=scenario.attachment_stiffness_nm_rad,
            attachment_damping_nm_s_rad=scenario.attachment_damping_nm_s_rad,
            attachment_limit_rad=scenario.attachment_limit_rad, seed=scenario.seed)
    _check_assets()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = output or (REPO_ROOT / "results" / "mvp3" / f"{scenario.name}-{stamp}")
    target.mkdir(parents=True, exist_ok=False)
    cameras_enabled = visual or capture_video
    asset_root = _scenario_assets(scenario, target, quality=quality,
                                  cameras_enabled=cameras_enabled)
    world_path = _scenario_world(target, fast_headless=fast_headless, quality=quality,
                                 cameras_enabled=cameras_enabled)
    env = _prepare_env(_ros_environment(), asset_root)
    executable = _gz(env)
    renode_session = None
    live_dashboard = None
    if live_lockstep:
        elf = os.environ.get("RIOSE_ZEPHYR_ELF")
        if not elf:
            raise RuntimeError("--live-lockstep requires RIOSE_ZEPHYR_ELF pointing to the built Renode-profile Zephyr ELF")
        from .bridge.live_lockstep import RenodeLiveSession
        renode_session = RenodeLiveSession(REPO_ROOT, Path(elf), target / "renode-live.log")
        from .visualization.live_dashboard import LiveDashboard
        live_dashboard = LiveDashboard(target)
        print(f"Live companion panel (loopback): {live_dashboard.url}", flush=True)
    manifest = {
        "schema_version": "riose.mvp3.experiment/v1", "scenario": scenario.name,
        "description": scenario.description, "seed": scenario.seed,
        "status": "SIMULATED", "provenance": "SIMULATED",
        "clock": {"master": "Gazebo simulation time", "physics_step_s": 0.001,
                  "requested_real_time_factor": None if fast_headless else 1.0,
                  "execution_mode": "FAST_HEADLESS_UNPACED" if fast_headless else ("VISUAL" if visual else "HEADLESS_RTF_1"),
                  "firmware_clock": ("Renode virtual time controlled at each Gazebo IMU timestamp"
                                     if live_lockstep else "Renode virtual time, replayed against the Gazebo IMU trace")},
        "imu_topic": IMU_TOPIC,
        "activity_labels_sent_to_firmware": False,
        "clock_sync": ({"status": "PENDING", "clock_master": "GAZEBO_SIMULATION_TIME",
                        "mode": "GAZEBO_MASTER_PAUSED_STEPPING", "live_lockstep": True}
                       if live_lockstep else {
                           "status": "OFFLINE_REPLAY_ONLY", "clock_master": "GAZEBO_SIMULATION_TIME",
                           "firmware_clock": "RENODE_VIRTUAL_TIME", "live_lockstep": False}),
        "tag_mass_g": scenario.tag_mass_g,
        "visual_presentation": {"quality": quality.upper(), "telemetry_overlay_requested": overlay,
                                "renderer": "OGRE2", "fps": _QUALITY[quality][2],
                                "cameras": ["overview", "follow", "head", "ear_tag_macro", "anchor", "ground_low"]},
        "tag_mass_status": "ASSUMED",
        "attachment_position_m": scenario.attachment_position_m,
        "attachment_dynamics": {"spring_stiffness_nm_rad": scenario.attachment_stiffness_nm_rad,
                                "damping_nm_s_rad": scenario.attachment_damping_nm_s_rad,
                                "limit_rad": scenario.attachment_limit_rad,
                                "status": "ASSUMED"},
        "duration_s": scenario.duration_s,
        "assets": {"world": str(world_path), "cow": str(asset_root / "riose_cow" / "model.sdf"),
                   "tag": str(asset_root / "riose_ear_tag" / "model.sdf"),
                   "source_cow": str(MODEL_PATH / "riose_cow" / "model.sdf"),
                   "source_tag": str(MODEL_PATH / "riose_ear_tag" / "model.sdf")},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (target / "scenario.json").write_text(json.dumps({
        "scenario": scenario.name,
        "phases": [phase.__dict__ for phase in scenario.phases],
    }, indent=2, sort_keys=True) + "\n")
    trajectory = to_gz_joint_trajectory(scenario, sample_period_s=sample_period_s)
    (target / "joint_trajectory.pb.txt").write_text(trajectory + "\n")
    command = [executable, "sim", "--seed", str(scenario.seed), "-v", "2"]
    if not live_lockstep:
        command.extend(["--iterations", str(round((scenario.duration_s + 2.0) / 0.001))])
    if visual:
        command.append(str(world_path))
    else:
        command.extend(["-s", "--headless-rendering", str(world_path)])
    start = time.monotonic()
    with (target / "server.log").open("w", encoding="utf-8") as server_log:
        server = subprocess.Popen(command, env=env, stdout=server_log, stderr=subprocess.STDOUT,
                                  text=True, start_new_session=True)
        recorder = None
        camera_recorder = None
        contact_recorder = None
        imu_subscriber = None
        try:
            _find_topic(env, executable, IMU_TOPIC, server)
            _pause_and_reset(env, executable)
            if live_lockstep:
                from .bridge.live_lockstep import GazeboIMUSubscriber
                imu_subscriber = GazeboIMUSubscriber(env, IMU_TOPIC)
            recorder = _start_recording(env, executable, target,
                                        None if live_lockstep else scenario.duration_s,
                                        IMU_TOPIC, "gazebo_imu.jsonl")
            # A contact sensor may not advertise its topic until the first
            # physics update (and may publish only when touching); subscribe
            # before unpausing instead of failing on discovery here.
            contact_recorder = _start_recording(env, executable, target,
                                                None if live_lockstep else scenario.duration_s,
                                                CONTACT_TOPIC, "ear_tag_contact.jsonl")
            _find_topic(env, executable, POSE_TOPIC, server)
            pose_recorder = _start_recording(env, executable, target,
                                             None if live_lockstep else scenario.duration_s,
                                             POSE_TOPIC, "gazebo_pose.jsonl")
            if capture_video:
                media_dir = target / "media"
                camera_log = (target / "camera_recorder.log").open("w", encoding="utf-8")
                camera_recorder = subprocess.Popen(
                    ["/usr/bin/python3", str(REPO_ROOT / "scripts" / "record_mvp3_cameras.py"),
                     str(media_dir), "--fps", str(_QUALITY[quality][2])],
                    env=env, stdout=camera_log,
                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
                camera_log.close()
                time.sleep(0.4)
            _find_topic(env, executable, JOINT_TOPIC, server)
            _publish(env, executable, JOINT_TOPIC, "gz.msgs.JointTrajectory", trajectory)
            time.sleep(0.1)
            if live_lockstep:
                assert renode_session is not None and imu_subscriber is not None
                from .bridge.live_lockstep import advance_gazebo_and_renode
                live_bridge = advance_gazebo_and_renode(
                    executable=executable, env=env,
                    duration_s=Decimal(str(scenario.duration_s)),
                    monitor=renode_session, subscriber=imu_subscriber,
                    evidence_path=target / "live_clock_evidence.jsonl",
                    status_callback=_live_dashboard_callback(
                        live_dashboard, renode_session, scenario.duration_s))
                if live_dashboard is not None:
                    live_dashboard.update({"status": "SIMULATION_COMPLETE"})
                trace_text = renode_session.query("sysbus.spi1.radio TxTraceJson")
                trace_json = _json_object_from_monitor_output(trace_text, label="SX1262 TX trace")
                if trace_json.get("schema_version") != "riose.renode.sx1262_tx_trace/v1":
                    raise RuntimeError("Renode returned an unsupported SX1262 TX trace schema")
                live_model_evidence = {
                    "schema_version": "riose.mvp3.live_firmware_evidence/v1",
                    "status": "SIMULATED_APPROXIMATE_HIGH_PASS_WU",
                    "sample_injection_count": _monitor_scalar(renode_session, "sysbus.i2c1.imu GazeboSampleInjectionCount"),
                    "wakeup_comparator_sample_count": _monitor_scalar(renode_session, "sysbus.i2c1.imu WakeupComparatorSampleCount"),
                    "imu_output_sample_read_count": _monitor_scalar(renode_session, "sysbus.i2c1.imu OutputSampleReadCount"),
                    "lis2dw12_ctrl1": _monitor_scalar(renode_session, "sysbus.i2c1.imu Control1Configuration"),
                    "lis2dw12_sample_rate_hz": _monitor_scalar(renode_session, "sysbus.i2c1.imu SampleRate"),
                    "wakeup_generated_event_count": _monitor_scalar(renode_session, "sysbus.i2c1.imu WakeupGeneratedEventCount"),
                    "wakeup_event_read_count": _monitor_scalar(renode_session, "sysbus.i2c1.imu WakeupEventReadCount"),
                    "wakeup_source_read_count": _monitor_scalar(renode_session, "sysbus.i2c1.imu WakeupSourceReadCount"),
                    "last_wakeup_source_value": _monitor_scalar(renode_session, "sysbus.i2c1.imu LastWakeupSourceValue"),
                    "wakeup_irq_asserted_at_end": _monitor_scalar(renode_session, "sysbus.i2c1.imu WakeupIRQAsserted"),
                    "radio_tx_count": _monitor_scalar(renode_session, "sysbus.spi1.radio TxCount"),
                    "radio_tx_done_count": _monitor_scalar(renode_session, "sysbus.spi1.radio TxDoneCount"),
                    "state_trace_gpioa_odr": _monitor_scalar(renode_session, "sysbus ReadDoubleWord 0x50000014"),
                    "state_trace_pin_bits": [0, 1, 3],
                    "state_code_map": ["BOOT", "SELF_TEST", "SLEEP", "IMU_MONITORING",
                                       "RF_TX", "RF_RX", "ALERT", "ERROR_RECOVERY"],
                    "clock_evidence": str(target / "live_clock_evidence.jsonl"),
                }
                state_port = live_model_evidence["state_trace_gpioa_odr"]
                live_model_evidence["firmware_final_state_code"] = (
                    ((state_port >> 0) & 1) | (((state_port >> 1) & 1) << 1) |
                    (((state_port >> 3) & 1) << 2))
                live_model_evidence["firmware_final_state"] = live_model_evidence["state_code_map"][
                    live_model_evidence["firmware_final_state_code"]]
                duration = float(live_bridge["simulation_duration_s"])
                comparator_samples = int(live_model_evidence["wakeup_comparator_sample_count"])
                live_model_evidence["wakeup_comparator_sample_rate_hz_observed"] = (
                    comparator_samples / duration if duration > 0 else None)
                live_model_evidence["wakeup_comparator_odr_hz_configured"] = (
                    live_model_evidence["lis2dw12_sample_rate_hz"])
                movement_records = []
                for record in trace_json.get("records", []):
                    payload = bytes.fromhex(record["payload_hex"])
                    timestamp_s = int(record["tx_start_ns"]) / 1_000_000_000
                    behavior = payload[1]
                    if (record["tx_index"] > 1 and behavior >= 2 and
                            _scenario_state(scenario, timestamp_s) in {"WALKING", "RUNNING", "HEAD_SHAKE"}):
                        movement_records.append({"tx_index": record["tx_index"],
                                                 "timestamp_s": timestamp_s,
                                                 "scenario_phase": _scenario_state(scenario, timestamp_s),
                                                 "behavior": behavior})
                live_model_evidence["movement_derived_tx_records"] = movement_records
                live_model_evidence["post_boot_tx_count"] = max(0, int(live_model_evidence["radio_tx_count"]) - 1)
                live_model_evidence["movement_derived_tx_count"] = len(movement_records)
                live_model_evidence["firmware_wake_tx_validated"] = bool(
                    int(live_model_evidence["sample_injection_count"]) ==
                    int(live_bridge["gazebo_imu_samples_injected"])
                    and int(live_model_evidence["lis2dw12_ctrl1"]) == 0x14
                    and int(live_model_evidence["wakeup_generated_event_count"]) > 0
                    and int(live_model_evidence["wakeup_event_read_count"]) > 0
                    and int(live_model_evidence["radio_tx_count"]) == len(trace_json.get("records", []))
                    and int(live_model_evidence["radio_tx_done_count"]) == int(live_model_evidence["radio_tx_count"])
                    and live_model_evidence["post_boot_tx_count"] > 0
                    and live_model_evidence["movement_derived_tx_count"] > 0
                    and live_model_evidence["firmware_final_state"] == "SLEEP"
                    and not live_model_evidence["wakeup_irq_asserted_at_end"])
                (target / "firmware_live_evidence.json").write_text(
                    json.dumps(live_model_evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                tx_trace_path = target / "sx1262_tx_trace.json"
                tx_trace_path.write_text(json.dumps(trace_json, indent=2, sort_keys=True) + "\n",
                                         encoding="utf-8")
                server.terminate()
                server.wait(timeout=10)
            else:
                _unpause(env, executable)
                timeout = max(60.0, scenario.duration_s * 3.0)
                server.wait(timeout=timeout)
            elapsed = time.monotonic() - start
            if server.returncode:
                raise RuntimeError(f"Gazebo exited with code {server.returncode}; inspect {target / 'server.log'}")
        except BaseException:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(10)
                except subprocess.TimeoutExpired:
                    server.kill()
            raise
        finally:
            if camera_recorder is not None:
                if camera_recorder.poll() is None:
                    camera_recorder.terminate()
                try:
                    camera_recorder.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    camera_recorder.kill()
                    camera_recorder.wait(timeout=4)
            if recorder is not None:
                if recorder.poll() is None:
                    recorder.terminate()
                try:
                    recorder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    recorder.kill()
                recorder._riose_record_stream.close()
            if contact_recorder is not None:
                if contact_recorder.poll() is None:
                    contact_recorder.terminate()
                try:
                    contact_recorder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    contact_recorder.kill()
                contact_recorder._riose_record_stream.close()
            if 'pose_recorder' in locals():
                if pose_recorder.poll() is None:
                    pose_recorder.terminate()
                try:
                    pose_recorder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pose_recorder.kill()
                pose_recorder._riose_record_stream.close()
            if imu_subscriber is not None:
                imu_subscriber.close()
            if renode_session is not None:
                renode_session.close()
            if live_dashboard is not None:
                live_dashboard.close()
    manifest.update({"wall_runtime_s": elapsed, "gazebo_exit_code": server.returncode,
                     "visual_mode": visual,
                     "gazebo_start_policy": "PAUSE_AND_RESET_BEFORE_RECORDING",
                     "simulation_time_origin_s": 0.0,
                     "clock_sync": live_bridge if live_lockstep else manifest["clock_sync"]})
    manifest["attachment_contact_measurement"] = _summarize_attachment_contacts(
        target / "ear_tag_contact.jsonl")
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if not (target / "gazebo_imu.jsonl").stat().st_size:
        raise RuntimeError(f"Gazebo did not publish IMU data; inspect {target / 'server.log'}")
    _materialize_recordings(target, scenario.seed, scenario)
    motion_metrics = _animal_motion_metrics(target, scenario)
    attachment_result = json.loads((target / "attachment_metrics.json").read_text(encoding="utf-8"))
    manifest.update({"animal_model_loaded": True,
                     "ear_attachment_validated": attachment_result["result"] == "PASS",
                     "motion_controller_completed": True, "scenario_complete": True,
                     "animal_motion_metrics": motion_metrics,
                     "ear_attachment_evidence": attachment_result})
    if firmware_trace is not None:
        manifest["firmware_trace"] = _ingest_firmware_trace(firmware_trace, target)
        manifest["anchor_receive_count"] = manifest["firmware_trace"]["anchor_logical_accept_count"]
    if live_lockstep:
        assert renode_session is not None
        tx_trace = target / "sx1262_tx_trace.json"
        converted_trace = target / "live_firmware_trace.jsonl"
        from .rf.renode_trace import convert_sx1262_trace_file
        convert_sx1262_trace_file(tx_trace, converted_trace)
        live_firmware = _ingest_firmware_trace(
            converted_trace, target, trace_duration_s=scenario.duration_s)
        live_firmware["source_scope"] = (
            "SX1262 logical trace captured from the same Zephyr/Renode session as Gazebo IMU injection; "
            "Renode and Gazebo virtual clocks were equal at every recorded barrier. The trace has no physical "
            "RF propagation or measured RSSI."
        )
        manifest["firmware_trace"] = live_firmware
        manifest["anchor_receive_count"] = live_firmware["anchor_logical_accept_count"]
        manifest["firmware_clock_sync"] = {
            "status": "SIMULATED", "mode": "GAZEBO_MASTER_PAUSED_STEPPING",
            "live_lockstep": True, "clock_master": "GAZEBO_SIMULATION_TIME",
            "sample_injection_count": live_bridge["gazebo_imu_samples_injected"],
            "max_observed_clock_error_s_at_batch_barriers": live_bridge["max_observed_clock_error_s"],
            "clock_observation": live_bridge["renode_clock_observation"],
            "evidence": str(target / "live_clock_evidence.jsonl"),
        }
        manifest["firmware_live_evidence"] = live_model_evidence
        manifest["firmware_wake_tx_validated"] = live_model_evidence["firmware_wake_tx_validated"]
        manifest["movement_derived_tx_count"] = live_model_evidence["movement_derived_tx_count"]
        manifest["movement_wake_evidence"] = {
            "status": live_model_evidence["status"],
            "wakeup_generated_event_count": live_model_evidence["wakeup_generated_event_count"],
            "wakeup_event_read_count": live_model_evidence["wakeup_event_read_count"],
            "wakeup_source_read_count": live_model_evidence["wakeup_source_read_count"],
            "last_wakeup_source_value": live_model_evidence["last_wakeup_source_value"],
            "post_boot_tx_count": live_model_evidence["post_boot_tx_count"],
            "movement_derived_tx_count": live_model_evidence["movement_derived_tx_count"],
            "post_motion_firmware_state": live_model_evidence["firmware_final_state"],
            "irq_asserted_after_source_read": live_model_evidence["wakeup_irq_asserted_at_end"],
            "last_payload_classifier_behavior": movement_records[-1]["behavior"] if movement_records else None,
        }
        manifest["clock_mapping"] = {
            "schema_version": "riose.mvp3.clock_mapping/v1", "status": "SIMULATED",
            "source_clock": "RENODE_VIRTUAL_TIME", "target_clock": "GAZEBO_SIMULATION_TIME",
            "scale": 1.0, "offset_s": 0.0, "method": "LIVE_GAZEBO_MASTER_LOCKSTEP",
            "live_lockstep": True,
            "alignment_basis": "Gazebo advanced in paused batches; Renode reached each same simulation timestamp before the next batch",
            "barrier_evidence": str((target / "live_clock_evidence.jsonl").resolve()),
        }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if renode_session is not None:
        renode_session.close()
    if os.environ.get("RIOSE_ZEPHYR_ELF") and not live_lockstep:
        from .bridge import replay_in_renode
        replay_in_renode(target)
    from .reporting import report_experiment
    report_experiment(target)
    return target


def _animal_motion_metrics(target: Path, scenario: Scenario) -> dict[str, object]:
    """Compare commanded forward travel with recorded cow root displacement."""
    recording = target / "recording.jsonl"
    if not recording.is_file():
        return {"status": "UNAVAILABLE", "provenance": "SIMULATED"}
    first = last = None
    with recording.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            if first is None:
                first = record
            last = record
    if not first or not last:
        return {"status": "UNAVAILABLE", "provenance": "SIMULATED"}
    start = first.get("animal_pose", {}).get("position", {})
    end = last.get("animal_pose", {}).get("position", {})
    if not isinstance(first.get("animal_pose"), dict) or not isinstance(last.get("animal_pose"), dict):
        return {"status": "PARTIAL_POSE", "provenance": "SIMULATED"}
    # Gazebo's protobuf JSON omits default-valued coordinates, which are zero.
    displacement = math.dist(tuple(float(start.get(axis, 0.0)) for axis in ("x", "y", "z")),
                             tuple(float(end.get(axis, 0.0)) for axis in ("x", "y", "z")))
    expected = sum(phase.forward_speed_m_s * phase.duration_s for phase in scenario.phases)
    return {
        "status": "SIMULATED", "provenance": "SIMULATED",
        "expected_forward_displacement_m": expected,
        "measured_root_displacement_m": displacement,
        "displacement_ratio": displacement / expected if expected > 0 else None,
        "active_forward_duration_s": sum(phase.duration_s for phase in scenario.phases
                                          if phase.forward_speed_m_s > 0),
    }


def _materialize_recordings(target: Path, seed: int, scenario: Scenario) -> None:
    """Create normalized CSV, RESD, and a timestamped IMU recording from Gazebo."""
    from .bridge.gazebo_to_resd import convert_trace

    bridge_dir = target / "bridge"
    # Renode 1.17 schedules this firmware configuration at integer 13 Hz
    # (nearest to the LIS2DW12's 12.5 Hz high-performance ODR). Resample the
    # Gazebo stream to that cadence so replay time and comparator input samples
    # share the same explicit rate and do not silently slow a 50 Hz trace.
    manifest_path = convert_trace(target / "gazebo_imu.jsonl", bridge_dir, seed=seed,
                                  resample_rate_hz=Decimal("13"))
    shutil.copy2(bridge_dir / "gazebo.csv", target / "gazebo_imu.csv")
    converter = REPO_ROOT / "hardware" / "renode" / "scripts" / "dataset_to_resd.py"
    command = [sys.executable, str(converter), "GAZEBO", "--manifest", str(manifest_path),
               "--output", str(target / "gazebo_imu.resd")]
    result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True,
                            timeout=120)
    if result.returncode:
        raise RuntimeError(f"Gazebo-to-RESD conversion failed: {result.stderr.strip()}")
    imu_rows = []
    with (target / "gazebo_imu.csv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            imu_rows.append({"time_from_capture_start_s": float(row["timestamp_s"]),
                             "imu_acceleration_g": {axis: float(row[f"{axis}_g"]) for axis in "xyz"},
                             "source": "GAZEBO_SIMULATED",
                             "acceleration_resampling": "INTERPOLATED"})
    raw_imu = _read_jsonl_samples(target / "gazebo_imu.jsonl")
    raw_poses = _read_pose_samples(
        target / "gazebo_pose.jsonl", max_rate_hz=POSE_RECORD_RATE_HZ,
        focus_names={"riose_cow", "ear_left", "riose_ear_tag"})
    if not raw_imu or not raw_poses:
        raise RuntimeError("Gazebo IMU and pose recordings are both required")
    imu_origin = raw_imu[0][0]
    pose_times = [item[0] for item in raw_poses]
    ear_relative_errors = []
    initial_relative = None
    for _, poses in raw_poses:
        ear, tag = poses.get("ear_left"), poses.get("riose_ear_tag")
        if ear and tag:
            delta = tuple(tag["position"].get(axis, 0.0) - ear["position"].get(axis, 0.0)
                          for axis in "xyz")
            orientation = ear.get("orientation", {})
            qx, qy, qz = (float(orientation.get(axis, 0.0)) for axis in "xyz")
            qw = float(orientation.get("w", 1.0))
            cross = (qy * delta[2] - qz * delta[1],
                     qz * delta[0] - qx * delta[2],
                     qx * delta[1] - qy * delta[0])
            cross2 = (qy * cross[2] - qz * cross[1],
                      qz * cross[0] - qx * cross[2],
                      qx * cross[1] - qy * cross[0])
            relative = tuple(delta[i] - 2 * qw * cross[i] + 2 * cross2[i] for i in range(3))
            if initial_relative is None:
                initial_relative = relative
            ear_relative_errors.append(math.dist(relative, initial_relative))
    attachment_max_drift_m = max(ear_relative_errors, default=float("inf"))
    attachment_validated = bool(ear_relative_errors) and attachment_max_drift_m <= 0.005
    raw_imu_times = [item[0] for item in raw_imu]
    last_kinematics_index = -1
    current_pose_kinematics = None
    initial_attachment_quaternion = None
    attachment_angles: list[float] = []
    with (target / "recording.jsonl").open("w", encoding="utf-8") as stream:
        for row in imu_rows:
            sim_time = imu_origin + row["time_from_capture_start_s"]
            pose_index = min(max(bisect.bisect_left(pose_times, sim_time), 0), len(raw_poses) - 1)
            if pose_index and abs(pose_times[pose_index - 1] - sim_time) < abs(pose_times[pose_index] - sim_time):
                pose_index -= 1
            pose_time, poses = raw_poses[pose_index]
            if pose_index != last_kinematics_index:
                current_pose_kinematics = _pose_kinematics_at(raw_poses, pose_index)
                last_kinematics_index = pose_index
            nearest_imu_index = min(max(bisect.bisect_left(raw_imu_times, sim_time), 0), len(raw_imu) - 1)
            if nearest_imu_index and abs(raw_imu_times[nearest_imu_index - 1] - sim_time) < abs(raw_imu_times[nearest_imu_index] - sim_time):
                nearest_imu_index -= 1
            _, sensor = raw_imu[nearest_imu_index]
            ear_pose = poses.get("ear_left")
            tag_pose = poses.get("riose_ear_tag")
            relative_quaternion = _relative_attachment_quaternion(ear_pose, tag_pose)
            if initial_attachment_quaternion is None and relative_quaternion is not None:
                initial_attachment_quaternion = relative_quaternion
            attachment_angle = _relative_angle_rad(initial_attachment_quaternion, relative_quaternion)
            if attachment_angle is not None:
                attachment_angles.append(attachment_angle)
            record = {**row, "simulation_timestamp_s": sim_time,
                      "cow_state": _scenario_state(scenario, sim_time),
                      "animal_pose": poses.get("riose_cow"),
                      "ear_pose": ear_pose,
                      "tag_pose": tag_pose,
                      "tag_attachment_angle_rad": attachment_angle,
                      "pose_kinematics": current_pose_kinematics,
                      "imu_orientation_xyzw": sensor.get("orientation"),
                      "imu_angular_velocity_rad_s": sensor.get("angularVelocity"),
                      "pose_sample_timestamp_s": pose_time,
                      "pose_join_status": "NEAREST_GAZEBO_POSE"}
            stream.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    (target / "recording_manifest.json").write_text(json.dumps({
        "schema_version": "riose.mvp3.recording/v1", "provenance": "SIMULATED",
        "clock_master": "Gazebo simulation time", "timestamp_origin": "first IMU sample",
        "sample_count": len(imu_rows), "sample_rate_hz": json.loads(manifest_path.read_text())["datasets"][0]["sample_rate_hz"],
        "streams": ["cow_state", "animal_pose", "ear_pose", "tag_pose", "imu_acceleration_g",
                    "imu_orientation_xyzw", "imu_angular_velocity_rad_s", "tag_attachment_angle_rad",
                    "pose_kinematics"],
        "pose_join": "nearest timestamped Gazebo pose sample",
        "pose_recording_processing": {
            "max_rate_hz": POSE_RECORD_RATE_HZ,
            "entities": ["riose_cow", "ear_left", "riose_ear_tag"],
            "status": "SIMULATED_DOWNSAMPLED_FOR_RECORDING_JOIN",
        },
        "pose_kinematics": {
            "method": "finite differences over Gazebo PosePublisher samples",
            "frame": "world",
            "source_timestamp": "Gazebo simulation time",
            "fields": ["linear_velocity_m_s", "linear_acceleration_m_s2",
                       "angular_velocity_rad_s", "angular_acceleration_rad_s2"],
        },
        "firmware_clock_relation": "offline RESD replay; no live lockstep",
        "resampling": json.loads(manifest_path.read_text())["datasets"][0].get("transformations", []),
    }, indent=2, sort_keys=True) + "\n")
    (target / "attachment_metrics.json").write_text(json.dumps({
        "status": "SIMULATED", "attachment_joint": "riose_ear_tag_attachment",
        "relative_pivot_position_drift_max_m": attachment_max_drift_m,
        "relative_hinge_angle_rad_max": max(attachment_angles, default=None),
        "hinge_angle_provenance": "DERIVED_FROM_GAZEBO_POSE_QUATERNIONS",
        "joint_force_measurement": {"status": "UNAVAILABLE",
                                    "reason": "Gazebo joint/contact wrench sensor is not configured"},
        "validation_tolerance_m": 0.005,
        "result": "PASS" if attachment_validated else "FAIL",
        "samples": len(ear_relative_errors),
        "evidence": "Gazebo PosePublisher world poses for ear_left and nested riose_ear_tag",
    }, indent=2, sort_keys=True) + "\n")


def _read_jsonl_samples(path: Path) -> list[tuple[float, dict]]:
    samples = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            value = json.loads(line)
            stamp = value.get("header", {}).get("stamp", {})
            timestamp = int(stamp.get("sec", 0)) + int(stamp.get("nsec", 0)) * 1e-9
            compact = {key: value[key] for key in
                       ("orientation", "angularVelocity", "linearAcceleration") if key in value}
            samples.append((timestamp, compact))
    return samples


def _read_pose_samples(path: Path, *, max_rate_hz: float | None = None,
                       focus_names: set[str] | None = None) -> list[tuple[float, dict[str, dict]]]:
    samples = []
    if max_rate_hz is not None and (not math.isfinite(max_rate_hz) or max_rate_hz <= 0):
        raise ValueError("pose max_rate_hz must be finite and positive")
    minimum_period = 1.0 / max_rate_hz if max_rate_hz else 0.0
    last_kept_time = -math.inf
    last_sample: tuple[float, dict[str, dict]] | None = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            value = json.loads(line)
            stamp = value.get("header", {}).get("stamp", {})
            timestamp = int(stamp.get("sec", 0)) + int(stamp.get("nsec", 0)) * 1e-9
            named = {}
            for pose in value.get("pose", []):
                name = pose.get("name")
                if not name or (focus_names is not None and name not in focus_names):
                    continue
                named[name] = {"name": name}
                for component in ("position", "orientation"):
                    named[name][component] = {
                        axis: float(pose.get(component, {}).get(axis, default))
                        for axis, default in (("x", 0.0), ("y", 0.0),
                                              ("z", 0.0), ("w", 1.0))
                        if axis != "w" or component == "orientation"
                    }
            row = (timestamp, named)
            last_sample = row
            if timestamp - last_kept_time + 1e-12 < minimum_period:
                continue
            if timestamp <= last_kept_time:
                continue
            samples.append(row)
            last_kept_time = timestamp
    if last_sample is not None and last_sample[0] > last_kept_time:
        samples.append(last_sample)
    return samples


def _scenario_state(scenario: Scenario, timestamp_s: float) -> str:
    elapsed = 0.0
    for phase in scenario.phases:
        elapsed += phase.duration_s
        if timestamp_s < elapsed:
            return phase.behavior
    return scenario.phases[-1].behavior if scenario.phases else "UNKNOWN"


def _pose_kinematics(samples: list[tuple[float, dict[str, dict]]]) -> list[dict]:
    """Derive world-frame linear/angular kinematics from recorded poses.

    These values are finite differences of Gazebo PosePublisher output, not
    simulator-reported contact forces or a separate sensor measurement.
    """
    return [_pose_kinematics_at(samples, index) for index in range(len(samples))]


def _pose_kinematics_at(samples: list[tuple[float, dict[str, dict]]], index: int) -> dict:
    """Calculate one pose sample's derivatives without retaining a full-run array."""
    count = len(samples)
    result = {"provenance": "DERIVED_FINITE_DIFFERENCE", "frame": "world", "entities": {}}
    if count < 2 or not 0 <= index < count:
        return result
    timestamp = samples[index][0]
    available_names = set(samples[index][1])
    focus_names = {"riose_cow", "ear_left", "riose_ear_tag"}
    names = available_names & focus_names or available_names
    left, right = (0, 1) if index == 0 else (
        (count - 2, count - 1) if index == count - 1 else (index - 1, index + 1))
    dt = samples[right][0] - samples[left][0]
    for name in names:
        pose = samples[index][1][name]
        def position_at(item_index: int):
            item = samples[item_index][1].get(name)
            return _pose_vector(item, "position", "xyz")

        def quaternion_at(item_index: int):
            return _pose_quaternion(samples[item_index][1].get(name))

        p0, p1 = position_at(left), position_at(right)
        q0, q1 = quaternion_at(left), quaternion_at(right)
        linear_velocity = (tuple((p1[axis] - p0[axis]) / dt for axis in range(3))
                           if dt > 0 and p0 is not None and p1 is not None else None)
        angular_velocity = _quaternion_delta_velocity(q0, q1, dt)
        linear_acceleration = None
        angular_acceleration = None
        if 0 < index < count - 1:
            # Differentiate the neighboring secant velocities at their midpoint.
            prev_dt = timestamp - samples[index - 1][0]
            next_dt = samples[index + 1][0] - timestamp
            before, center, after = position_at(index - 1), position_at(index), position_at(index + 1)
            if prev_dt > 0 and next_dt > 0 and before is not None and center is not None and after is not None:
                linear_acceleration = tuple(2.0 * (
                    (after[axis] - center[axis]) / next_dt
                    - (center[axis] - before[axis]) / prev_dt
                ) / (prev_dt + next_dt) for axis in range(3))
            v_before = _quaternion_delta_velocity(
                quaternion_at(index - 1), quaternion_at(index), prev_dt)
            v_after = _quaternion_delta_velocity(
                quaternion_at(index), quaternion_at(index + 1), next_dt)
            if v_before is not None and v_after is not None:
                angular_acceleration = tuple((v_after[axis] - v_before[axis]) /
                                             ((prev_dt + next_dt) / 2.0) for axis in range(3))
        result["entities"][name] = {
            "linear_velocity_m_s": _vector_dict(linear_velocity),
            "linear_acceleration_m_s2": _vector_dict(linear_acceleration),
            "angular_velocity_rad_s": _vector_dict(angular_velocity),
            "angular_acceleration_rad_s2": _vector_dict(angular_acceleration),
        }
    return result


def _pose_vector(pose: dict | None, key: str, axes: str) -> tuple[float, float, float] | None:
    if pose is None or not isinstance(pose.get(key), dict):
        return None
    value = pose[key]
    return tuple(float(value.get(axis, 0.0)) for axis in axes)  # type: ignore[return-value]


def _pose_quaternion(pose: dict | None) -> tuple[float, float, float, float] | None:
    value = pose.get("orientation") if pose else None
    if not isinstance(value, dict):
        return None
    return tuple(float(value.get(axis, default)) for axis, default in
                 (("x", 0.0), ("y", 0.0), ("z", 0.0), ("w", 1.0)))  # type: ignore[return-value]


def _relative_attachment_quaternion(ear: dict | None, tag: dict | None):
    q_ear, q_tag = _pose_quaternion(ear), _pose_quaternion(tag)
    if q_ear is None or q_tag is None:
        return None
    ex, ey, ez, ew = q_ear
    tx, ty, tz, tw = q_tag
    # inverse(q_ear) * q_tag, with xyzw quaternion storage.
    return (ew*tx - ex*tw - ey*tz + ez*ty,
            ew*ty + ex*tz - ey*tw - ez*tx,
            ew*tz - ex*ty + ey*tx - ez*tw,
            ew*tw + ex*tx + ey*ty + ez*tz)


def _relative_angle_rad(reference, current) -> float | None:
    if reference is None or current is None:
        return None
    dot = abs(sum(a*b for a, b in zip(reference, current)))
    return 2.0 * math.acos(max(-1.0, min(1.0, dot)))


def _quaternion_delta_velocity(q0, q1, dt: float) -> tuple[float, float, float] | None:
    if dt <= 0 or q0 is None or q1 is None:
        return None
    # q_delta = q1 * inverse(q0), giving a world-frame rotation vector.
    x0, y0, z0, w0 = q0
    x1, y1, z1, w1 = q1
    dx = -w1 * x0 + w0 * x1 - y1 * z0 + z1 * y0
    dy = -w1 * y0 + x1 * z0 + w0 * y1 - z1 * x0
    dz = -w1 * z0 - x1 * y0 + y1 * x0 + w0 * z1
    dw = w0 * w1 + x0 * x1 + y0 * y1 + z0 * z1
    if dw < 0:
        dx, dy, dz, dw = -dx, -dy, -dz, -dw
    vector_norm = math.sqrt(dx * dx + dy * dy + dz * dz)
    if vector_norm < 1e-12:
        return (0.0, 0.0, 0.0)
    angle = 2.0 * math.atan2(vector_norm, max(-1.0, min(1.0, dw)))
    scale = angle / (vector_norm * dt)
    return (dx * scale, dy * scale, dz * scale)


def _vector_dict(value: tuple[float, float, float] | None) -> dict[str, float] | None:
    return ({axis: component for axis, component in zip("xyz", value)}
            if value is not None else None)


def _ingest_firmware_trace(source: Path, target: Path, *, trace_duration_s: float | None = None) -> dict:
    """Use existing MVP2 RF and power adapters on caller-provided trace evidence."""
    source = source.resolve()
    if not source.is_file():
        raise RuntimeError(f"firmware trace does not exist: {source}")
    trace_copy = target / "firmware_trace.jsonl"
    shutil.copy2(source, trace_copy)
    from .power import PowerAnalysisError, run_power_analysis
    from .rf import AnchorReceiver, logical_events_from_firmware_trace, read_firmware_trace

    try:
        if trace_duration_s is None:
            power = run_power_analysis(trace_copy, target / "power")
        else:
            power = run_power_analysis(trace_copy, target / "power", trace_duration_s=trace_duration_s)
        records = read_firmware_trace(trace_copy)
        events = logical_events_from_firmware_trace(records)
    except (PowerAnalysisError, ValueError) as exc:
        raise RuntimeError(f"firmware trace integration failed: {exc}") from exc
    anchor = AnchorReceiver("riose_anchor")
    accepted = [anchor.accept_logical_event(event) for event in events]
    (target / "rf_events.jsonl").write_text(
        "".join(json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
                for event in events), encoding="utf-8")
    (target / "anchor_events.jsonl").write_text(
        "".join(json.dumps(entry.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
                for entry in accepted if entry is not None), encoding="utf-8")
    return {"path": str(trace_copy), "source_path": str(source),
            "source_scope": "caller-provided SIMULATED trace; same-run clock correlation is not established",
            "status": "SIMULATED", "power_status": power.get("status"),
            "logical_rf_event_count": len(events), "anchor_logical_accept_count": len(anchor.log),
            "physical_rf_result": "ANTENNA_MODEL_UNVALIDATED", "rssi_dbm": None}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="riose mvp3", description=__doc__)
    parser.add_argument("--version", action="version", version=f"RIOSE MVP3 {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="verify the Gazebo Harmonic runtime")
    commands.add_parser("list", help="list deterministic MVP3 scenarios")
    run = commands.add_parser("run", help="run a physical Gazebo scenario and record IMU samples")
    run.add_argument("scenario", choices=sorted({*SCENARIOS, "standing", "walking", "running", "head-shake", "ear-flick", "lower-head", "raise-head", "mixed", "heavy-tag", "attachment-variation", "radio-event", "long-simulation", "mass-sweep"}))
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--visual", action="store_true", help="open Gazebo GUI")
    mode.add_argument("--headless", action="store_true", help="run Gazebo server without GUI")
    mode.add_argument("--fast-headless", action="store_true", help="disable real-time pacing in a copied headless world")
    run.add_argument("--output", type=Path)
    run.add_argument("--seed", type=int)
    run.add_argument("--tag-mass-g", type=float)
    run.add_argument("--duration-s", type=float,
                     help="truncate a scenario to this virtual duration (for staged long-run validation)")
    run.add_argument("--attachment-position-m", type=float, nargs=3, metavar=("X", "Y", "Z"))
    run.add_argument("--attachment-stiffness-nm-rad", type=float)
    run.add_argument("--attachment-damping-nm-s-rad", type=float)
    run.add_argument("--attachment-limit-rad", type=float)
    run.add_argument("--live-lockstep", action="store_true",
                     help="run the Renode firmware while Gazebo advances in synchronized paused steps; requires RIOSE_ZEPHYR_ELF")
    run.add_argument("--quality", choices=sorted(_QUALITY), default="medium",
                     help="OGRE2 camera preset; does not change physics")
    run.add_argument("--overlay", dest="overlay", action="store_true", default=True,
                     help="request compact telemetry overlay in presentation outputs")
    run.add_argument("--no-overlay", dest="overlay", action="store_false",
                     help="disable the presentation telemetry overlay")
    run.add_argument("--firmware-trace", type=Path,
                     help="optional SIMULATED firmware trace JSONL for MVP2 RF/power adapters")
    run.add_argument("--sample-period", type=float, default=0.05,
                     help="joint trajectory knot period in simulated seconds")
    test = commands.add_parser("test", help="run MVP3 schema and Gazebo headless integration checks")
    test.add_argument("--quick", action="store_true", help="skip the live Gazebo check")
    report = commands.add_parser("report", help="summarize an experiment directory")
    report.add_argument("experiment", type=Path)
    replay = commands.add_parser("replay", help="replay a saved IMU experiment through Renode")
    replay.add_argument("experiment", type=Path)
    replay.add_argument("--presentation", action="store_true",
                        help="also render the experiment's recorded camera/event presentation")
    replay.add_argument("--slow-motion", choices=(1.0,0.5,0.25), type=float, default=1.0)
    visualize = commands.add_parser("visualize", help="create an offline IMU viewer for an experiment")
    visualize.add_argument("experiment", type=Path)
    visualize.add_argument("--output", type=Path)
    visual = commands.add_parser("visual", help="Gazebo visual camera presets and clean screenshots")
    visual.add_argument("visual_args", nargs=argparse.REMAINDER)
    camera = commands.add_parser("camera", help="capture a real OGRE2 frame from a named MVP3 camera")
    camera.add_argument("name", choices=("overview", "follow", "head", "ear-tag", "anchor", "ground-low"))
    camera.add_argument("--quality", choices=sorted(_QUALITY), default="high")
    camera.add_argument("--output", type=Path)
    cinematic = commands.add_parser("cinematic", help="run a live firmware-linked camera sequence and encode a presentation")
    cinematic.add_argument("--quality", choices=sorted(_QUALITY), default="presentation")
    cinematic.add_argument("--slow-motion", choices=(1.0,0.5,0.25), type=float, default=0.5)
    cinematic.add_argument("--output", type=Path)
    cinematic.add_argument("--no-overlay", dest="overlay", action="store_false", default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "setup":
            env = _ros_environment()
            executable = _gz(env)
            print(f"{executable}: {subprocess.run([executable, 'sim', '--version'], env=env, text=True, capture_output=True, check=True).stdout.splitlines()[0]}")
            print(f"world: {WORLD}")
        elif args.command == "list":
            for scenario in SCENARIOS.values():
                print(f"{scenario.name}\t{scenario.duration_s:g}s\t{scenario.description}")
        elif args.command == "run":
            options = {"visual": args.visual, "seed": args.seed,
                       "sample_period_s": args.sample_period,
                       "attachment_position_m": tuple(args.attachment_position_m) if args.attachment_position_m else None,
                       "attachment_stiffness_nm_rad": args.attachment_stiffness_nm_rad,
                       "attachment_damping_nm_s_rad": args.attachment_damping_nm_s_rad,
                       "attachment_limit_rad": args.attachment_limit_rad,
                       "duration_s": args.duration_s,
                       "firmware_trace": args.firmware_trace,
                       "fast_headless": args.fast_headless,
                       "live_lockstep": args.live_lockstep,
                       "quality": args.quality, "overlay": args.overlay}
            if args.scenario == "mass-sweep":
                parent = args.output or (REPO_ROOT / "results" / "mvp3" /
                                         f"mass-sweep-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
                runs = [run_experiment("walking", output=parent / f"mass_{mass:02d}g",
                                       tag_mass_g=mass, **options)
                        for mass in (20, 25, 30, 35, 40)]
                parent.mkdir(parents=True, exist_ok=True)
                with (parent / "mass_sweep_summary.csv").open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream, lineterminator="\n")
                    writer.writerow(("provenance", "tag_mass_g_assumed", "peak_abs_x_g",
                                     "peak_abs_y_g", "peak_abs_z_g", "attachment_pivot_drift_max_m"))
                    for mass, experiment in zip((20, 25, 30, 35, 40), runs):
                        summary = json.loads((experiment / "summary.json").read_text(encoding="utf-8"))
                        peaks = summary["peak_abs_acceleration_g_by_axis"]
                        writer.writerow(("SIMULATED", mass, peaks["x"], peaks["y"], peaks["z"],
                                         summary["ear_attachment_metrics"].get("relative_pivot_position_drift_max_m")))
                print("\n".join(str(path) for path in runs))
            else:
                output = run_experiment(args.scenario, output=args.output,
                                        tag_mass_g=args.tag_mass_g, **options)
                print(output)
        elif args.command == "report":
            from .reporting import report_experiment
            print(json.dumps(report_experiment(args.experiment), indent=2, sort_keys=True))
        elif args.command == "replay":
            if args.presentation:
                from .presentation import replay_presentation
                print(json.dumps(replay_presentation(args.experiment, slow_motion=args.slow_motion), indent=2, sort_keys=True))
            else:
                from .bridge import replay_in_renode
                print(json.dumps(replay_in_renode(args.experiment), indent=2, sort_keys=True))
        elif args.command == "visualize":
            from .visualization import create_viewer
            print(create_viewer(args.experiment, output=args.output))
        elif args.command == "visual":
            from .visualization.cinematic import main as visual_main
            return visual_main(args.visual_args)
        elif args.command == "camera":
            print(capture_camera(args.name, output=args.output, quality=args.quality))
        elif args.command == "cinematic":
            from .presentation import run_cinematic
            print(json.dumps(run_cinematic(quality=args.quality, slow_motion=args.slow_motion,
                                           output=args.output, overlay=args.overlay), indent=2, sort_keys=True))
        elif args.command == "test":
            from .validation import run_validation
            print(json.dumps(run_validation(live=not args.quick), indent=2, sort_keys=True))
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"MVP3 error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
