"""Reproducible observation datasets with truth in a separate file/namespace."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .contracts import GroundTruth, RFObservation

FEATURE_FIELDS = (
    "timestamp_s", "tag_id", "anchor_id", "rssi_dbm", "snr_db",
    "packet_received", "imu_accel_norm_g", "behavior_state", "tof_ns",
    "phase_rad", "status",
)
GROUND_TRUTH_FIELDS = ("timestamp_s", "tag_id", "x", "y")


def write_episode_dataset(observations: Iterable[RFObservation], truth: Iterable[GroundTruth],
                          output_dir: str | Path, split: str,
                          scenario: dict | None = None) -> dict[str, str]:
    """Write inference features and evaluation labels separately.

    CSV is the portable fallback. If PyArrow is installed, observations are
    additionally written as Parquet; ground truth remains in its own file.
    """
    if split not in {"train", "validation", "holdout"}:
        raise ValueError("split must be train, validation, or holdout")
    root = Path(output_dir)
    features_dir, truth_dir = root / split / "features", root / split / "ground_truth"
    features_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)
    obs_rows = [{
        "timestamp_s": o.timestamp_s, "tag_id": o.tag_id, "anchor_id": o.anchor_id,
        "rssi_dbm": o.rssi_dbm, "snr_db": o.snr_db,
        "packet_received": o.packet_received, "imu_accel_norm_g": o.imu_accel_norm_g,
        "behavior_state": o.behavior_state, "tof_ns": o.tof_ns,
        "phase_rad": o.phase_rad, "status": o.status.value,
    } for o in observations]
    truth_rows = [{"timestamp_s": t.timestamp_s, "tag_id": t.tag_id, "x": t.x, "y": t.y}
                  for t in truth]
    feature_csv = features_dir / "observations.csv"
    truth_csv = truth_dir / "labels.csv"
    write_csv(feature_csv, obs_rows, FEATURE_FIELDS)
    write_csv(truth_csv, truth_rows, GROUND_TRUTH_FIELDS)
    outputs = {"features_csv": str(feature_csv), "ground_truth_csv": str(truth_csv)}
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        outputs["parquet"] = "unavailable; CSV fallback written"
    else:
        feature_parquet = features_dir / "observations.parquet"
        parquet_schema = pa.schema([
            ("timestamp_s", pa.float64()), ("tag_id", pa.string()),
            ("anchor_id", pa.string()), ("rssi_dbm", pa.float64()),
            ("snr_db", pa.float64()), ("packet_received", pa.bool_()),
            ("imu_accel_norm_g", pa.float64()), ("behavior_state", pa.string()),
            ("tof_ns", pa.float64()), ("phase_rad", pa.float64()),
            ("status", pa.string()),
        ])
        pq.write_table(pa.Table.from_pylist(obs_rows, schema=parquet_schema), feature_parquet)
        outputs["features_parquet"] = str(feature_parquet)
    manifest = {"split": split, "feature_fields": list(FEATURE_FIELDS),
                "feature_file": str(feature_csv), "ground_truth_file": str(truth_csv),
                "scenario": scenario or {}, "status": "SIMULATED"}
    manifest_path = root / split / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    outputs["manifest"] = str(manifest_path)
    return outputs


def write_csv(path: Path, rows: list[dict], fields: Iterable[str] | None = None) -> None:
    fields = list(fields) if fields is not None else (list(rows[0]) if rows else [])
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(rows)

