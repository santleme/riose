"""Premium, evidence-preserving camera replay for MVP3 experiments."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .cli import REPO_ROOT, run_experiment


def _compose(experiment: Path, *, slow_motion: float) -> Path:
    command = ["/usr/bin/python3", str(REPO_ROOT / "scripts" / "compose_mvp3_cinematic.py"),
               str(experiment), "--slow-motion", str(slow_motion)]
    result = subprocess.run(command, text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"cinematic composition failed: {result.stderr.strip() or result.stdout.strip()}")
    path = Path(result.stdout.strip().splitlines()[-1]).resolve()
    if not path.is_file():
        raise RuntimeError(f"cinematic renderer reported a missing output: {path}")
    return path


def replay_presentation(experiment: Path, *, slow_motion: float = 1.0) -> dict:
    experiment = experiment.resolve()
    manifest_path = experiment / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"experiment manifest not found: {manifest_path}")
    path = _compose(experiment, slow_motion=slow_motion)
    timeline = experiment / "media" / "cinematic_timeline.json"
    return {"status": "SIMULATED", "video": str(path), "timeline": str(timeline),
            "source_experiment": str(experiment), "slow_motion": slow_motion,
            "clock_source": "original Gazebo camera image timestamps"}


def run_cinematic(*, quality: str = "presentation", slow_motion: float = 0.5,
                  output: Path | None = None, overlay: bool = True) -> dict:
    elf = os.environ.get("RIOSE_ZEPHYR_ELF")
    if not elf or not Path(elf).is_file():
        build = subprocess.run([str(REPO_ROOT / "scripts" / "build_mvp3_renode_firmware.sh")],
                               text=True, capture_output=True, timeout=900)
        if build.returncode:
            raise RuntimeError(
                "could not build the Renode-profile Zephyr ELF for cinematic; "
                f"inspect the toolchain output: {build.stderr.strip() or build.stdout.strip()}")
        elf = build.stdout.strip().splitlines()[-1]
        if not Path(elf).is_file():
            raise RuntimeError(f"firmware build reported a missing Renode ELF: {elf}")
        os.environ["RIOSE_ZEPHYR_ELF"] = elf
    target = output or (REPO_ROOT / "results" / "mvp3" /
                        f"cinematic-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    experiment = run_experiment("mixed", visual=False, output=target, live_lockstep=True,
                                quality=quality, overlay=overlay, capture_video=True)
    firmware = json.loads((experiment / "firmware_live_evidence.json").read_text(encoding="utf-8"))
    if not firmware.get("firmware_wake_tx_validated"):
        raise RuntimeError(
            "cinematic capture did not pass its live firmware/IMU/TX gate; "
            f"state={firmware.get('firmware_final_state')}, "
            f"samples={firmware.get('sample_injection_count')}, "
            f"TX_DONE={firmware.get('radio_tx_done_count')}; "
            f"inspect {experiment / 'firmware_live_evidence.json'}")
    video = _compose(experiment, slow_motion=slow_motion)
    media = experiment / "media"
    public_media = REPO_ROOT / "results" / "mvp3" / "media"
    public_media.mkdir(parents=True, exist_ok=True)
    for source_name, output_name in (("overview.png", "overview.png"),
                                     ("ear_tag_macro.png", "ear_tag_closeup.png"),
                                     ("follow.mp4", "walking.mp4"),
                                     ("head.mp4", "head_shake.mp4"),
                                     ("anchor.mp4", "rf_event.mp4"),
                                     ("cinematic.mp4", "cinematic.mp4")):
        source = media / source_name
        if source.is_file():
            shutil.copy2(source, public_media / output_name)
    return {"status": "SIMULATED", "experiment": str(experiment),
            "video": str(video), "media_directory": str(public_media),
            "firmware_evidence": str(experiment / "firmware_live_evidence.json"),
            "capture_evidence": str(media / "capture_manifest.json"),
            "timeline": str(media / "cinematic_timeline.json"),
            "radio_metrics": {"rssi_dbm": None, "snr_db": None,
                              "backend": "Renode SX1262 logical model"},
            "slow_motion": slow_motion, "telemetry_overlay": overlay}
