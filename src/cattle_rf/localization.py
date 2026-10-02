"""Inference-safe localization estimators for receiver-visible RF data.

Ground truth is accepted only by :func:`train_fingerprint_model` (as a
separate training target) and :func:`evaluate`. The real-time ``estimate``
entry point has no ground-truth parameter by design.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from math import hypot, isfinite, log10
from typing import Mapping, Sequence

import numpy as np

from .contracts import Anchor, Estimate, GroundTruth, RFObservation


METHODS = (
    "strongest_anchor",
    "weighted_centroid",
    "path_loss",
    "extra_trees",
    "gradient_boosting",
    "temporal_fusion",
)

EPOCH_TOLERANCE_S = 1.0

# Match the default simulated radio setup: 14 dBm transmit power, 915 MHz,
# and free-space loss at the 1 m reference distance.
_DEFAULT_PATH_LOSS_EXPONENT = 2.7
_DEFAULT_RSSI_AT_1M_DBM = 14.0 - (
    32.44 + 20.0 * log10(915.0) + 20.0 * log10(1.0 / 1000.0)
)


@dataclass(slots=True)
class FingerprintModel:
    """Fitted RF fingerprint model; truth is not retained after fitting."""

    estimator: object
    anchor_ids: tuple[str, ...]
    algorithm: str

    def predict(self, observations: Sequence[RFObservation], anchors: Sequence[Anchor]) -> list[Estimate]:
        """Predict positions from observations only."""
        grouped = _group_observations(observations)
        anchor_map = {a.anchor_id: a for a in anchors}
        rows: list[np.ndarray] = []
        keys: list[tuple[str, float]] = []
        for key, group in sorted(grouped.items()):
            rows.append(_fingerprint_features(group, self.anchor_ids))
            keys.append(key)
        if not rows:
            return []
        predictions = np.asarray(self.estimator.predict(np.vstack(rows)), dtype=float)
        result: list[Estimate] = []
        for (tag_id, timestamp), xy in zip(keys, predictions, strict=True):
            quality = _observation_quality(grouped[(tag_id, timestamp)], anchor_map)
            result.append(Estimate(timestamp, tag_id, float(xy[0]), float(xy[1]), self.algorithm, quality))
        return result


def train_fingerprint_model(
    observations: Sequence[RFObservation],
    ground_truth: Sequence[GroundTruth],
    anchors: Sequence[Anchor],
    algorithm: str = "extra_trees",
    *,
    random_state: int = 7,
) -> FingerprintModel:
    """Fit an optional supervised fingerprint model.

    This is the only training entry point that consumes ground truth. Inference
    is performed by ``FingerprintModel.predict`` / ``estimate`` and has no
    truth input.
    """
    if algorithm not in {"extra_trees", "gradient_boosting"}:
        raise ValueError("algorithm must be 'extra_trees' or 'gradient_boosting'")
    grouped = _group_observations(observations)
    truth_by_tag: dict[str, list[GroundTruth]] = defaultdict(list)
    for point in ground_truth:
        truth_by_tag[point.tag_id].append(point)
    for points in truth_by_tag.values():
        points.sort(key=lambda point: point.timestamp_s)
    matched = []
    for key, group in sorted(grouped.items()):
        point = _nearest_truth(key[0], key[1], truth_by_tag)
        if point is not None:
            matched.append((key, group, (float(point.x), float(point.y))))
    if len(matched) < 2:
        raise ValueError("at least two matching observation/ground-truth samples are required")
    try:
        from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor
        from sklearn.multioutput import MultiOutputRegressor
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("scikit-learn is required for fingerprint models") from exc
    anchor_ids = tuple(sorted(a.anchor_id for a in anchors))
    if algorithm == "extra_trees":
        model = ExtraTreesRegressor(n_estimators=100, min_samples_leaf=1, random_state=random_state, n_jobs=1)
    else:
        # sklearn's GradientBoostingRegressor is single-output; wrap it so the
        # same model predicts the farm's two spatial coordinates.
        model = MultiOutputRegressor(
            GradientBoostingRegressor(random_state=random_state, n_estimators=100, max_depth=2)
        )
    X = np.vstack([_fingerprint_features(group, anchor_ids) for _, group, _ in matched])
    y = np.asarray([xy for _, _, xy in matched], dtype=float)
    model.fit(X, y)
    return FingerprintModel(model, anchor_ids, algorithm)


def estimate(
    observations: Sequence[RFObservation],
    anchors: Sequence[Anchor],
    method: str,
    *,
    prior_positions: Mapping[str, tuple[float, float]] | None = None,
    fingerprint_models: Mapping[str, FingerprintModel] | None = None,
) -> list[Estimate]:
    """Estimate position by tag and timestamp from anchor-side inputs only.

    ``prior_positions`` maps a tag ID to a previously estimated ``(x, y)``;
    callers must not place ground-truth positions there. Fingerprint models are
    fitted separately. A missing/unfitted fingerprint model yields an explicit
    unavailable estimate instead of falling back silently.
    """
    if method not in METHODS:
        raise ValueError(f"unknown localization method {method!r}; choose one of {list(METHODS)}")
    enabled = {a.anchor_id: a for a in anchors if a.enabled}
    grouped = _group_observations(observations)
    if method in {"extra_trees", "gradient_boosting"}:
        algorithm = method
        model = (fingerprint_models or {}).get(algorithm)
        if model is None:
            return [Estimate(t, tag, None, None, method, 0.0) for tag, t in sorted(grouped)]
        return model.predict(observations, anchors)

    results: list[Estimate] = []
    previous: dict[str, tuple[float, float]] = dict(prior_positions or {})
    previous_time: dict[str, float] = {}
    velocity: dict[str, tuple[float, float]] = {}
    for (tag_id, timestamp), group in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        usable = [(o, enabled.get(o.anchor_id)) for o in group if o.packet_received and o.rssi_dbm is not None]
        usable = [(o, a) for o, a in usable if a is not None and isfinite(float(o.rssi_dbm))]
        quality = _observation_quality(group, enabled)
        xy: tuple[float, float] | None
        if not usable:
            xy = None
        elif method == "strongest_anchor":
            obs, anchor = max(usable, key=lambda pair: float(pair[0].rssi_dbm))
            xy = (anchor.x, anchor.y)
        elif method in {"weighted_centroid", "temporal_fusion"}:
            xy = _weighted_centroid(usable)
        else:
            xy = _multilateration(usable)

        if method == "temporal_fusion":
            imu = [float(o.imu_accel_norm_g) for o in group if o.imu_accel_norm_g is not None and isfinite(float(o.imu_accel_norm_g))]
            activity = max((abs(v - 1.0) for v in imu), default=0.0)
            prior = previous.get(tag_id)
            dt = max(0.0, timestamp - previous_time.get(tag_id, timestamp))
            v = velocity.get(tag_id, (0.0, 0.0))
            predicted = (prior[0] + v[0] * dt, prior[1] + v[1] * dt) if prior is not None else None
            if xy is not None and predicted is not None:
                # Sparse RF makes the prior more influential; observed motion
                # raises the measurement weight without inventing direction.
                rf_weight = min(0.85, max(0.25, 0.30 + quality * 0.45 + min(activity, 1.0) * 0.10))
                fused = (predicted[0] * (1 - rf_weight) + xy[0] * rf_weight,
                         predicted[1] * (1 - rf_weight) + xy[1] * rf_weight)
                if dt > 0:
                    velocity[tag_id] = ((fused[0] - prior[0]) / dt, (fused[1] - prior[1]) / dt)
                xy = fused
            elif xy is None and predicted is not None:
                # Dead reckoning is explicitly bounded to a short gap.
                xy = predicted if dt <= 180.0 else None
            if xy is not None:
                previous[tag_id] = xy
                previous_time[tag_id] = timestamp

        results.append(Estimate(timestamp, tag_id, *(xy if xy is not None else (None, None)), method, quality))
    return results


def evaluate(estimates: Sequence[Estimate], ground_truth: Sequence[GroundTruth]) -> dict[str, float | int | None]:
    """Compare estimates with truth after inference; returns honest error stats."""
    truth_by_tag: dict[str, list[GroundTruth]] = defaultdict(list)
    for point in ground_truth:
        truth_by_tag[point.tag_id].append(point)
    for points in truth_by_tag.values():
        points.sort(key=lambda point: point.timestamp_s)
    matched = [(e, _nearest_truth(e.tag_id, float(e.timestamp_s), truth_by_tag))
               for e in estimates if e.x is not None and e.y is not None]
    errors = [hypot(float(e.x) - point.x, float(e.y) - point.y)
              for e, point in matched if point is not None]
    total = len(ground_truth)
    if not errors:
        return {"samples": 0, "coverage_pct": 0.0 if total else None,
                "mean_error_m": None, "median_error_m": None, "p90_error_m": None,
                "p95_error_m": None, "p99_error_m": None, "max_error_m": None}
    values = np.asarray(errors, dtype=float)
    return {
        "samples": len(values), "coverage_pct": len(values) / total * 100 if total else None,
        "mean_error_m": float(np.mean(values)), "median_error_m": float(np.median(values)),
        "p90_error_m": float(np.percentile(values, 90)), "p95_error_m": float(np.percentile(values, 95)),
        "p99_error_m": float(np.percentile(values, 99)), "max_error_m": float(np.max(values)),
    }


def _group_observations(observations: Sequence[RFObservation]) -> dict[tuple[str, float], list[RFObservation]]:
    # Small anchor clock offsets are expected. Cluster observations from one
    # beacon epoch within a bounded tolerance, anchored to the epoch's first
    # timestamp to avoid an unbounded chaining window.
    by_tag: dict[str, list[RFObservation]] = defaultdict(list)
    for obs in observations:
        by_tag[obs.tag_id].append(obs)
    grouped: dict[tuple[str, float], list[RFObservation]] = {}
    for tag_id, tag_rows in by_tag.items():
        tag_rows.sort(key=lambda row: row.timestamp_s)
        current: list[RFObservation] = []
        epoch_start = 0.0
        for observation in tag_rows:
            timestamp = float(observation.timestamp_s)
            if current and timestamp - epoch_start > EPOCH_TOLERANCE_S:
                epoch_time = float(np.median([row.timestamp_s for row in current]))
                grouped[(tag_id, epoch_time)] = current
                current = []
            if not current:
                epoch_start = timestamp
            current.append(observation)
        if current:
            epoch_time = float(np.median([row.timestamp_s for row in current]))
            grouped[(tag_id, epoch_time)] = current
    return grouped


def _nearest_truth(tag_id: str, timestamp: float,
                   truth_by_tag: Mapping[str, Sequence[GroundTruth]]) -> GroundTruth | None:
    points = truth_by_tag.get(tag_id, ())
    if not points:
        return None
    times = [point.timestamp_s for point in points]
    index = bisect_left(times, timestamp)
    candidates = points[max(0, index - 1):min(len(points), index + 1)]
    nearest = min(candidates, key=lambda point: abs(point.timestamp_s - timestamp))
    return nearest if abs(nearest.timestamp_s - timestamp) <= EPOCH_TOLERANCE_S else None


def _weighted_centroid(usable: Sequence[tuple[RFObservation, Anchor]]) -> tuple[float, float]:
    strongest = max(float(o.rssi_dbm) for o, _ in usable)
    # Relative received power avoids needing a guessed absolute transmit power.
    weights = np.asarray([10 ** ((float(o.rssi_dbm) - strongest) / 20.0) for o, _ in usable])
    coords = np.asarray([(a.x, a.y) for _, a in usable], dtype=float)
    point = np.average(coords, axis=0, weights=weights)
    return float(point[0]), float(point[1])


def _multilateration(usable: Sequence[tuple[RFObservation, Anchor]]) -> tuple[float, float] | None:
    if len(usable) < 3:
        return None
    anchors = [a for _, a in usable]
    coords = np.asarray([(a.x, a.y) for a in anchors], dtype=float)
    if np.linalg.matrix_rank(coords[1:] - coords[0]) < 2:
        return None
    try:
        from scipy.optimize import least_squares
    except ImportError:  # pragma: no cover - declared core dependency
        return None
    # This is a rough log-distance inversion, not a ranging capability claim.
    # Calibrate to the simulator's default 14 dBm / 915 MHz link budget;
    # the previous -44 dBm intercept made default simulated ranges about ten
    # times too short before multilateration even began.
    rssi = np.asarray([float(o.rssi_dbm) for o, _ in usable])
    distances = np.clip(10 ** ((_DEFAULT_RSSI_AT_1M_DBM - rssi) /
                              (10.0 * _DEFAULT_PATH_LOSS_EXPONENT)), 1.0, 10000.0)
    initial = _weighted_centroid(usable)
    result = least_squares(lambda p: (np.linalg.norm(coords - p, axis=1) - distances),
                           np.asarray(initial), loss="soft_l1", f_scale=10.0, max_nfev=100)
    if not result.success or not np.all(np.isfinite(result.x)):
        return None
    return float(result.x[0]), float(result.x[1])


def _observation_quality(group: Sequence[RFObservation], anchors: Mapping[str, Anchor]) -> float:
    configured = sum(1 for a in anchors.values() if a.enabled)
    if not configured:
        return 0.0
    received = sum(1 for o in group if o.packet_received and o.rssi_dbm is not None and o.anchor_id in anchors)
    return min(1.0, received / configured)


def _fingerprint_features(group: Sequence[RFObservation], anchor_ids: Sequence[str]) -> np.ndarray:
    by_anchor = {o.anchor_id: o for o in group}
    feature: list[float] = []
    for anchor_id in anchor_ids:
        obs = by_anchor.get(anchor_id)
        valid = obs is not None and obs.packet_received and obs.rssi_dbm is not None
        feature.extend([float(obs.rssi_dbm) if valid else -150.0,
                        float(obs.snr_db) if valid and obs.snr_db is not None else -50.0,
                        float(valid)])
    imu = [float(o.imu_accel_norm_g) for o in group if o.imu_accel_norm_g is not None and isfinite(float(o.imu_accel_norm_g))]
    feature.append(float(np.mean(imu)) if imu else 1.0)
    return np.asarray(feature, dtype=float)
