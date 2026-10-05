#!/usr/bin/env python3
"""Assemble a seekable presentation from timestamped Gazebo camera clips."""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import cv2


def load_frames(media: Path, camera: str):
    path = media / f"{camera}.mp4"
    timeline = media / f"{camera}_timestamps.csv"
    if not path.is_file() or not timeline.is_file():
        return []
    times = [float(row["sim_time_s"]) for row in csv.DictReader(timeline.open(encoding="utf-8"))]
    capture = cv2.VideoCapture(str(path))
    frames = []
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if index < len(times):
            frames.append((times[index], frame))
        index += 1
    capture.release()
    return frames


def stamp(frame, lines, *, rf=False):
    h, w = frame.shape[:2]
    overlay = frame.copy()
    cv2.rectangle(overlay, (20, 18), (min(w-18, 760), 30 + 29*len(lines)), (14, 23, 27), -1)
    cv2.addWeighted(overlay, 0.76, frame, 0.24, 0, frame)
    for i, line in enumerate(lines):
        cv2.putText(frame, line, (34, 48+i*29), cv2.FONT_HERSHEY_SIMPLEX, 0.66,
                    (231, 241, 239), 1, cv2.LINE_AA)
    if rf:
        # UI event marker only; this line does not model RF propagation.
        cv2.line(frame, (int(w*.51), int(h*.66)), (int(w*.82), int(h*.34)),
                 (74, 202, 245), 3, cv2.LINE_AA)
        cv2.circle(frame, (int(w*.51), int(h*.66)), 7, (74,202,245), -1)
        cv2.circle(frame, (int(w*.82), int(h*.34)), 7, (74,202,245), -1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment", type=Path)
    parser.add_argument("--slow-motion", type=float, choices=(1.0, 0.5, 0.25), default=1.0)
    args = parser.parse_args()
    target = args.experiment.resolve()
    media = target / "media"
    output = media / "cinematic.mp4"
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    show_overlay = bool(manifest.get("visual_presentation", {}).get("telemetry_overlay_requested", True))
    camera_frames = {name: load_frames(media, name) for name in
                     ("overview", "follow", "head", "ear_tag_macro", "anchor", "ground_low")}
    if any(not frames for frames in camera_frames.values()):
        missing = [name for name, frames in camera_frames.items() if not frames]
        raise SystemExit(f"camera video evidence missing for: {', '.join(missing)}")
    width, height = camera_frames["overview"][0][1].shape[1], camera_frames["overview"][0][1].shape[0]
    fps = float(manifest.get("visual_presentation", {}).get("fps", 8))
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width,height))
    if not writer.isOpened():
        raise SystemExit(f"could not encode {output}")
    live = json.loads((target / "firmware_live_evidence.json").read_text(encoding="utf-8"))
    live_status_path = target / "live_status.json"
    live_status = json.loads(live_status_path.read_text(encoding="utf-8")) if live_status_path.is_file() else {}
    tx = json.loads((target / "sx1262_tx_trace.json").read_text(encoding="utf-8"))
    power_path = target / "power" / "power_summary.json"
    power = json.loads(power_path.read_text(encoding="utf-8")) if power_path.is_file() else {}
    tx_times = [float(record["tx_start_ns"])*1e-9 for record in tx.get("records", [])]
    end = float(manifest["duration_s"])
    shake_start = min(11.0, end*.65)
    shake_end = min(14.0, end*.9)
    event_time = tx_times[-1] if tx_times else min(end, 1.0)
    shots = [
        ("overview", 0.0, min(2.5,end), 1.0, "SHOT 1 · RIOSE RURAL DIGITAL TWIN"),
        ("follow", min(3.0,end), min(8.4,end), 1.0, "SHOT 2 · WALKING · PHYSICAL IMU TRACE"),
        ("head", min(8.4,end), min(10.8,end), 1.0, "SHOT 3 · HEAD / EAR RESPONSE"),
        ("ear_tag_macro", min(10.8,end), shake_start, 1.0, "SHOT 4 · RIOSE EAR TAG · MVP2 CAD"),
        ("head", shake_start, shake_end, args.slow_motion, f"SHOT 5 · HEAD SHAKE · {args.slow_motion:g}x"),
        ("anchor", max(0,event_time-.45), min(end,event_time+.65), 1.0, "SHOT 6 · TX EVENT · LOGICAL BACKEND"),
        ("ear_tag_macro", max(0,end-2), end, 1.0, "SHOT 7 · PRODUCT / LIVE FIRMWARE TELEMETRY"),
    ]
    timeline = []
    playback_time = 0.0
    for camera, begin, finish, rate, title in shots:
        frames = [(sim, image) for sim,image in camera_frames[camera] if begin <= sim <= finish]
        if not frames:
            continue
        for sim_time, original in frames:
            frame = cv2.resize(original, (width,height), interpolation=cv2.INTER_AREA)
            labels = [title] if show_overlay else []
            rf_event = "SHOT 6" in title
            if rf_event and not show_overlay:
                labels.append("EVENT VISUALIZATION · NOT RF PROPAGATION")
            if rf_event and show_overlay:
                labels.extend(("EVENT VISUALIZATION · NOT RF PROPAGATION",
                               f"TX {len(tx_times)} · RSSI: UNAVAILABLE · SNR: UNAVAILABLE"))
            elif "SHOT 7" in title and show_overlay:
                labels.extend((f"FIRMWARE {live.get('firmware_final_state','UNAVAILABLE')} · RENODE",
                               f"IMU {live.get('sample_injection_count',0)} samples · TX_DONE {live.get('radio_tx_done_count',0)}"))
                battery_v = live_status.get("battery_mv")
                battery_text = f"{float(battery_v) / 1000.0:.2f} V SIMULATED" if battery_v is not None else "UNAVAILABLE"
                labels.append(f"ACTIVITY {live_status.get('behavior_estimate','UNAVAILABLE')} · RADIO {live_status.get('radio_state','UNAVAILABLE')} · RAIL {battery_text}")
                last = tx.get("records", [])[-1] if tx.get("records") else None
                payload = bytes.fromhex(last["payload_hex"]) if last else b""
                sequence = int.from_bytes(payload[6:10], "little") if len(payload) >= 10 else None
                tag_id = int.from_bytes(payload[2:6], "little") if len(payload) >= 6 else None
                labels.append(f"LAST PACKET tag {tag_id} · seq {sequence} · RENODE logical")
                energy = power.get("energy_by_event_mj", {}).get("TX")
                battery = power.get("ngspice", {}).get("battery_min_v")
                if energy is not None:
                    rail_label = f"{battery:.3f} V SIMULATED" if battery is not None else "UNAVAILABLE"
                    labels.append(f"TX energy {energy:.2f} mJ ASSUMED · min rail {rail_label}")
            stamp(frame, labels, rf=rf_event)
            writer.write(frame)
            timeline.append({"frame": len(timeline), "sim_time_s": sim_time,
                             "playback_time_s": playback_time, "camera": camera,
                             "shot": title, "playback_rate": rate})
            playback_time += 1.0 / fps
            if rate < 1:
                duplicate_count = int(round(1.0/rate)) - 1
                for _ in range(duplicate_count):
                    writer.write(frame)
                    timeline.append({"frame": len(timeline), "sim_time_s": sim_time,
                                     "playback_time_s": playback_time, "camera": camera,
                                     "shot": title, "playback_rate": rate})
                    playback_time += 1.0 / fps
    writer.release()
    (media / "cinematic_timeline.json").write_text(
        json.dumps({"schema_version":"riose.mvp3.cinematic/v1",
                    "video":str(output), "frames":len(timeline), "fps":fps,
                    "slow_motion":args.slow_motion,
                    "source_clock":"Gazebo simulation timestamps retained in per-camera CSV and this timeline",
                    "rf_visualization":"EVENT VISUALIZATION — NOT RF PROPAGATION",
                    "rssi_dbm":None,"snr_db":None,"backend":"Renode SX1262 logical model",
                    "shots":[{"camera":camera,"start_s":begin,"end_s":finish,
                              "playback_rate":rate,"title":title}
                             for camera,begin,finish,rate,title in shots],
                    "timeline":timeline}, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    if not output.is_file() or output.stat().st_size < 1024:
        raise SystemExit("cinematic output is unexpectedly small")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
