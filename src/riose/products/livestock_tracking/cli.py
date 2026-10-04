"""Command line entry point for local demo and reproducible experiments."""

from __future__ import annotations

import argparse
from array import array
from bisect import bisect_left
import csv
from dataclasses import replace
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any, Iterator

from .domain.contracts import Estimate, FarmConfig
from .adapters.datasets import write_episode_dataset
from .localization import METHODS, estimate, evaluate, train_fingerprint_model_from_matrices
from .application.pipeline import domain_randomized_training_matrices, run_episode
from .simulation import simulate_episode
from .simulation.rf import RFConfig


MAX_OFFLINE_SAMPLE_UNITS = 1_000_000
MAX_BENCHMARK_CDF_POINTS = 1_000_000
MAX_BENCHMARK_SCENARIOS = 10_000
MAX_DURATION_S = 604_800.0


def _validate_positive_finite(name: str, value: float, maximum: float) -> None:
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be finite, greater than zero, and at most {maximum:g}")


def _validate_counts(animals: int, anchors: int) -> None:
    if not 1 <= animals <= 1000:
        raise ValueError("animals must be between 1 and 1000")
    if not 1 <= anchors <= 40:
        raise ValueError("anchors must be between 1 and 40")


def _workload_units(animals: int, anchors: int, duration: float,
                    sample_period: float, *, training_animals: int | None = None) -> int:
    steps = max(1, math.ceil(duration / sample_period))
    units = animals * steps * (anchors + 3)
    if training_animals is not None:
        training_steps = max(1, math.ceil(max(600.0, duration) / min(30.0, sample_period)))
        units += 3 * training_animals * training_steps * (anchors + 3)
    return units


def _validate_demo_workload(animals: int, anchors: int) -> None:
    _validate_counts(animals, anchors)
    units = _workload_units(animals, anchors, 1800.0, 30.0)
    if units > MAX_OFFLINE_SAMPLE_UNITS:
        raise ValueError(f"demo exceeds in-memory sample budget ({units:,} > {MAX_OFFLINE_SAMPLE_UNITS:,})")


def _validate_dataset_workload(args: argparse.Namespace) -> None:
    _validate_counts(args.animals, args.anchors)
    _validate_positive_finite("duration", args.duration, MAX_DURATION_S)
    _validate_positive_finite("period", args.period, MAX_DURATION_S)
    units = args.animals * max(1, math.ceil(args.duration / args.period)) * (args.anchors + 2)
    if units > MAX_OFFLINE_SAMPLE_UNITS:
        raise ValueError(f"dataset export exceeds in-memory sample budget ({units:,} > {MAX_OFFLINE_SAMPLE_UNITS:,})")


def _validate_benchmark_workload(args: argparse.Namespace) -> None:
    if args.profile == "quick":
        animals = args.animals or [1, 10, 100]
        anchors = args.anchors or [4, 8, 20]
        duration, sample_period = (180.0 if args.duration is None else args.duration), 30.0
    else:
        animals = args.animals or [1, 10, 50, 100, 500, 1000]
        anchors = args.anchors or [4, 8, 12, 20]
        duration, sample_period = (300.0 if args.duration is None else args.duration), 60.0
    if args.seeds < 1 or args.seeds > 1000:
        raise ValueError("seeds must be between 1 and 1000")
    _validate_positive_finite("duration", duration, MAX_DURATION_S)
    training_animals = max(300, min(animals[0], 1000)) if animals else 300
    for animal_count in animals:
        for anchor_count in anchors:
            _validate_counts(animal_count, anchor_count)
            units = _workload_units(
                animal_count, anchor_count, duration, sample_period,
                training_animals=training_animals if animal_count == animals[0] else None,
            )
            if units > MAX_OFFLINE_SAMPLE_UNITS:
                raise ValueError(
                    f"benchmark scenario ({animal_count} animals, {anchor_count} anchors) "
                    f"exceeds in-memory sample budget ({units:,} > {MAX_OFFLINE_SAMPLE_UNITS:,})"
                )
    scenarios = len(animals) * len(anchors) * args.seeds
    if scenarios > MAX_BENCHMARK_SCENARIOS:
        raise ValueError(f"benchmark exceeds scenario budget ({scenarios:,} > {MAX_BENCHMARK_SCENARIOS:,})")
    cdf_points = sum(animal_count * max(1, math.ceil(duration / sample_period))
                     for animal_count in animals for _anchor_count in anchors)
    cdf_points *= args.seeds * len(METHODS)
    if cdf_points > MAX_BENCHMARK_CDF_POINTS:
        raise ValueError(
            f"benchmark exceeds retained CDF sample budget "
            f"({cdf_points:,} > {MAX_BENCHMARK_CDF_POINTS:,})"
        )
    stress_units = 100 * max(1, math.ceil(duration / sample_period)) * (20 + 3)
    if stress_units > MAX_OFFLINE_SAMPLE_UNITS:
        raise ValueError(
            f"benchmark stress scenarios exceed in-memory sample budget "
            f"({stress_units:,} > {MAX_OFFLINE_SAMPLE_UNITS:,})"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cattle-rf", description="Local cattle RF simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="start the local interactive demo")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8000)
    demo.add_argument("--animals", type=int, default=100)
    demo.add_argument("--anchors", type=int, default=8)
    demo.add_argument("--db", default="data/cattle_rf.sqlite3")
    benchmark = sub.add_parser("benchmark", help="run multi-method localization scenarios")
    benchmark.add_argument("--output", default="results")
    benchmark.add_argument("--profile", choices=("quick", "full"), default="full")
    benchmark.add_argument("--seeds", type=int, default=1)
    benchmark.add_argument("--duration", type=float, default=None)
    benchmark.add_argument("--animals", type=int, nargs="+", default=None)
    benchmark.add_argument("--anchors", type=int, nargs="+", default=None)
    dataset = sub.add_parser("dataset", help="generate separate train/validation/holdout files")
    dataset.add_argument("--output", default="datasets")
    dataset.add_argument("--seed", type=int, default=7)
    dataset.add_argument("--animals", type=int, default=20)
    dataset.add_argument("--anchors", type=int, default=8)
    dataset.add_argument("--duration", type=float, default=1800)
    dataset.add_argument("--period", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            _validate_demo_workload(args.animals, args.anchors)
        elif args.command == "benchmark":
            _validate_benchmark_workload(args)
        elif args.command == "dataset":
            _validate_dataset_workload(args)
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "demo":
        return run_demo(args)
    if args.command == "benchmark":
        run_benchmark(args)
        return 0
    if args.command == "dataset":
        run_dataset(args)
        return 0
    return 2


def run_demo(args: argparse.Namespace) -> int:
    import uvicorn
    from .adapters.api import create_app, ensure_animals
    from .adapters.persistence import Store

    db_path = Path(args.db)
    store = Store(db_path)
    app = create_app(db_path)
    config = FarmConfig(animal_count=args.animals, anchor_count=args.anchors,
                        duration_s=1800, sample_period_s=30, seed=7)
    episode, estimates, metrics = run_episode(config, "weighted_centroid")
    store.save_anchors(episode.anchors)
    store.save_episode(episode.observations, estimates, episode.ground_truth)
    store.set_metrics(metrics)
    ensure_animals(store, args.animals)
    store.close()
    app.state.anchors = list(episode.anchors)
    app.state.last_config = config
    app.state.last_ground_truth = episode.ground_truth
    print(f"Dashboard local: http://{args.host}:{args.port} (SIMULATED)", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def run_dataset(args: argparse.Namespace) -> None:
    root = Path(args.output)
    for split, offset in (("train", 0), ("validation", 100_003), ("holdout", 200_003)):
        config = FarmConfig(animal_count=args.animals, anchor_count=args.anchors,
                            duration_s=args.duration, sample_period_s=args.period,
                            seed=args.seed + offset)
        episode = simulate_episode(config)
        outputs = write_episode_dataset(episode.observations, episode.ground_truth, root, split,
                                        {"seed": config.seed, "animal_count": config.animal_count,
                                         "anchor_count": config.anchor_count,
                                         "width_m": config.width_m, "height_m": config.height_m,
                                         "duration_s": config.duration_s,
                                         "sample_period_s": config.sample_period_s})
        print(json.dumps({"split": split, **outputs}))


def run_benchmark(args: argparse.Namespace) -> None:
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    if args.profile == "quick":
        animal_counts = args.animals or [1, 10, 100]
        anchor_counts = args.anchors or [4, 8, 20]
        duration, sample_period = (180 if args.duration is None else args.duration), 30
    else:
        animal_counts = args.animals or [1, 10, 50, 100, 500, 1000]
        anchor_counts = args.anchors or [4, 8, 12, 20]
        duration, sample_period = (300 if args.duration is None else args.duration), 60
    seeds = max(1, args.seeds)
    rows: list[dict[str, Any]] = []
    errors = ErrorCsvWriter(root / "localization_errors.csv")
    energy_rows: list[dict[str, Any]] = []
    cdf_values: dict[str, array] = {method: array("d") for method in METHODS}
    gps_error_count = 0
    start = time.perf_counter()
    for anchor_count in anchor_counts:
        anchors = None
        model_cache: dict[str, Any] = {}
        for animal_count in animal_counts:
            for repeat in range(seeds):
                seed = 7001 + anchor_count * 1009 + animal_count * 17 + repeat
                rf = RFConfig(shadow_sigma_db=3.0 + (seed % 5),
                              measurement_sigma_db=1.0 + (seed % 4),
                              nlos_probability=(seed % 4) * 0.04,
                              interference_probability=0.01 + (seed % 3) * 0.01)
                config = FarmConfig(animal_count=animal_count, anchor_count=anchor_count,
                                    duration_s=duration, sample_period_s=sample_period,
                                    seed=seed, packet_loss_probability=0.05)
                episode = simulate_episode(config, anchors=anchors, rf_config=rf)
                anchors = episode.anchors
                missing_models = [name for name in ("extra_trees", "gradient_boosting")
                                  if name not in model_cache]
                if missing_models:
                    training_features = None
                    training_targets = None
                    try:
                        training_features, training_targets = domain_randomized_training_matrices(
                            config, anchors)
                    except RuntimeError as exc:
                        for model_name in missing_models:
                            model_cache[model_name] = exc
                    else:
                        for model_name in missing_models:
                            try:
                                model_cache[model_name] = train_fingerprint_model_from_matrices(
                                    training_features, training_targets, anchors,
                                    algorithm=model_name, random_state=17)
                            except RuntimeError as exc:
                                model_cache[model_name] = exc
                    finally:
                        del training_features, training_targets
                for method in METHODS:
                    models = None
                    if method in {"extra_trees", "gradient_boosting"}:
                        cached = model_cache[method]
                        if isinstance(cached, Exception):
                            rows.append({"animal_count": animal_count, "anchor_count": anchor_count,
                                         "seed": seed, "method": method, "status": "UNAVAILABLE",
                                         "reason": str(cached), "evidence": "SIMULATED"})
                            continue
                        models = {method: cached}
                    method_start = time.perf_counter()
                    estimates = estimate(episode.observations, episode.anchors, method,
                                         fingerprint_models=models)
                    metrics = evaluate(estimates, episode.ground_truth)
                    elapsed = time.perf_counter() - method_start
                    received = sum(o.packet_received for o in episode.observations)
                    total = len(episode.observations)
                    rows.append({"animal_count": animal_count, "anchor_count": anchor_count,
                                 "seed": seed, "method": method, **metrics,
                                 "packet_delivery_ratio": received / total if total else 0.0,
                                 "runtime_seconds": elapsed, "evidence": "SIMULATED"})
                    if metrics.get("mean_error_m") is not None:
                        import math
                        truth_map = {(g.tag_id, g.timestamp_s): g for g in episode.ground_truth}
                        for estimate_row in estimates:
                            target = truth_map.get((estimate_row.tag_id, estimate_row.timestamp_s))
                            if target and estimate_row.x is not None and estimate_row.y is not None:
                                error = math.hypot(estimate_row.x - target.x, estimate_row.y - target.y)
                                errors.append({"animal_count": animal_count, "anchor_count": anchor_count,
                                               "seed": seed, "method": method,
                                               "tag_id": estimate_row.tag_id,
                                               "timestamp_s": estimate_row.timestamp_s,
                                               "error_m": error, "evidence": "SIMULATED"})
                                cdf_values[method].append(error)
                # Perfect simulated GPS is an oracle/reference only. It is
                # constructed after inference from the isolated truth stream.
                for point in episode.ground_truth:
                    errors.append({"animal_count": animal_count, "anchor_count": anchor_count,
                                   "seed": seed, "method": "gps_oracle_reference",
                                   "tag_id": point.tag_id, "timestamp_s": point.timestamp_s,
                                   "error_m": 0.0, "evidence": "SIMULATED_REFERENCE"})
                    gps_error_count += 1
                rows.append({"animal_count": animal_count, "anchor_count": anchor_count,
                             "seed": seed, "method": "gps_oracle_reference",
                             **evaluate([Estimate(point.timestamp_s, point.tag_id, point.x,
                                                  point.y, "gps_oracle_reference")
                                         for point in episode.ground_truth], episode.ground_truth),
                             "packet_delivery_ratio": None, "runtime_seconds": 0.0,
                             "evidence": "SIMULATED_REFERENCE",
                             "note": "Ground-truth oracle; never an estimator input."})
                from .application.pipeline import energy_metrics
                energy = energy_metrics(episode, config)
                energy_rows.append({"animal_count": animal_count, "anchor_count": anchor_count,
                                    "seed": seed, **energy, "evidence": "SIMULATED"})
                if repeat == 0 and animal_count == animal_counts[0] and anchor_count == anchor_counts[0]:
                    write_episode_dataset(episode.observations, episode.ground_truth,
                                          root / "datasets", "holdout",
                                          {"seed": seed, "animal_count": animal_count,
                                           "anchor_count": anchor_count,
                                           "width_m": config.width_m, "height_m": config.height_m,
                                           "train_seed_profile": "domain_randomized training kept separate"})
    with CsvStreamWriter(root / "stress_metrics.csv", (
        "scenario", "anchor_count", "animal_count", "method", "samples", "coverage_pct",
        "mean_error_m", "median_error_m", "p90_error_m", "p95_error_m", "p99_error_m",
        "max_error_m", "packet_delivery_ratio", "movement_profile", "evidence",
    )) as stress_metrics, CsvStreamWriter(root / "stress_errors.csv", (
        "scenario", "anchor_count", "method", "tag_id", "timestamp_s", "error_m", "evidence",
    )) as stress_error_rows:
        stress_summary = StressSummary()
        for row_type, row in iter_stress_benchmark(duration, sample_period, seed=31_007):
            if row_type == "metric":
                stress_metrics.append(row)
                stress_summary.add(row)
            else:
                stress_error_rows.append(row)
    write_csv(root / "metrics.csv", rows)
    errors.close()
    write_csv(root / "energy.csv", energy_rows)
    write_csv(root / "cost_model.csv", create_cost_model())
    plots_dir = root / "plots"
    make_cdf_plot(cdf_values, plots_dir / "localization_error_cdf.png",
                  repeated_series={"gps_oracle_reference": (0.0, gps_error_count)})
    summary = {
        "status": "SIMULATED", "profile": args.profile,
        "animal_counts": animal_counts, "anchor_counts": anchor_counts,
        "seeds_per_scenario": seeds, "duration_s": duration,
        "sample_period_s": sample_period,
        "methods": ["gps_oracle_reference", *METHODS],
        "scenario_count": len(animal_counts) * len(anchor_counts) * seeds,
        "runtime_seconds": time.perf_counter() - start, "rows": len(rows),
        "error_rows": len(errors),
        "mean_error_by_method_m": aggregate_errors(cdf_values, gps_error_count),
        "stress_scenario_count": stress_summary.scenario_count,
        "stress_mean_error_m": stress_summary.means(),
        "energy_profile_note": "STM32WLE5 reference configuration; idle/BLE/Wi-Fi/alert currents are assumptions; no battery capacity is configured, so battery life is unavailable.",
        "limitations": ["Simulation only; no physical farm validation.",
                        "Path-loss calibration is assumed and RSSI ranging may be poor.",
                        "Advanced Sionna/ns-3/Zephyr/Wokwi integrations are optional and are not claimed as executed."],
    }
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_report(root / "REPORT.md", summary)
    print(f"Benchmark concluído: {root.resolve()} ({summary['scenario_count']} cenários, {summary['runtime_seconds']:.1f}s)")


def iter_stress_benchmark(duration_s: float, sample_period_s: float,
                          seed: int) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield stress metrics and errors as each scenario is evaluated."""
    methods = ("strongest_anchor", "weighted_centroid", "path_loss", "temporal_fusion")
    profiles = ("LOS", "NLOS_25pct", "PACKET_LOSS_50pct", "ANCHOR_DOWN",
                "CORRUPT_ANCHOR", "CLOCK_DRIFT_250ms")
    for anchor_count in (4, 8, 12, 20):
        for profile_name in profiles:
            config = FarmConfig(animal_count=100, anchor_count=anchor_count,
                                duration_s=duration_s, sample_period_s=sample_period_s,
                                seed=seed + anchor_count * 101,
                                packet_loss_probability=0.5 if profile_name == "PACKET_LOSS_50pct" else 0.05)
            rf = RFConfig(nlos_probability=0.25 if profile_name == "NLOS_25pct" else 0.0)
            anchors = None
            if profile_name == "ANCHOR_DOWN":
                from .simulation.episode import generate_anchors
                anchors = list(generate_anchors(config))
                anchors[0] = replace(anchors[0], enabled=False)
            episode = simulate_episode(config, anchors=anchors, rf_config=rf)
            anchor_set = list(episode.anchors)
            observations = list(episode.observations)
            if profile_name == "CORRUPT_ANCHOR" and anchor_set:
                bad_anchor = anchor_set[0].anchor_id
                observations = [replace(row,
                                        rssi_dbm=(float(row.rssi_dbm) + 40.0 if row.rssi_dbm is not None else None),
                                        snr_db=(float(row.snr_db) + 40.0 if row.snr_db is not None else None))
                                if row.anchor_id == bad_anchor and row.packet_received else row
                                for row in observations]
            elif profile_name == "CLOCK_DRIFT_250ms":
                offset_pattern = (-0.25, 0.25, -0.10, 0.10)
                offsets = {anchor.anchor_id: offset_pattern[index % len(offset_pattern)]
                           for index, anchor in enumerate(anchor_set)}
                observations = [replace(row, timestamp_s=row.timestamp_s + offsets[row.anchor_id])
                                for row in observations]
            truth_by_tag: dict[str, list[Any]] = {}
            for point in episode.ground_truth:
                truth_by_tag.setdefault(point.tag_id, []).append(point)
            truth_times_by_tag = {
                tag_id: [point.timestamp_s for point in points]
                for tag_id, points in truth_by_tag.items()
            }
            for method in methods:
                estimates = estimate(observations, anchor_set, method)
                method_metrics = evaluate(estimates, episode.ground_truth)
                received = sum(row.packet_received for row in observations)
                total = len(observations)
                yield "metric", {"scenario": profile_name, "anchor_count": anchor_count,
                                  "animal_count": config.animal_count, "method": method,
                                  **method_metrics,
                                  "packet_delivery_ratio": received / total if total else 0.0,
                                  "movement_profile": "behavior_driven_mixed_herd",
                                  "evidence": "SIMULATED"}
                import math
                for estimate_row in estimates:
                    candidates = truth_by_tag.get(estimate_row.tag_id, [])
                    point = nearest_truth_point(
                        candidates,
                        truth_times_by_tag.get(estimate_row.tag_id, []),
                        estimate_row.timestamp_s,
                    )
                    if (estimate_row.x is None or estimate_row.y is None or point is None
                            or abs(point.timestamp_s - estimate_row.timestamp_s) > 1.0):
                        continue
                    yield "error", {"scenario": profile_name, "anchor_count": anchor_count,
                                     "method": method, "tag_id": estimate_row.tag_id,
                                     "timestamp_s": estimate_row.timestamp_s,
                                     "error_m": math.hypot(estimate_row.x - point.x,
                                                           estimate_row.y - point.y),
                                     "evidence": "SIMULATED"}


def nearest_truth_point(candidates: list[Any], timestamps: list[float],
                        timestamp_s: float) -> Any | None:
    """Find the nearest time-ordered truth sample in logarithmic time.

    ``simulate_fence`` and stress evaluation accept timestamp offsets from
    clock-drift scenarios, so exact timestamp lookup is insufficient. Ties
    retain the legacy behavior of selecting the earlier truth sample.
    """
    if not candidates:
        return None
    index = bisect_left(timestamps, timestamp_s)
    nearby = candidates[max(0, index - 1):min(len(candidates), index + 1)]
    return min(nearby, key=lambda point: (abs(point.timestamp_s - timestamp_s),
                                          point.timestamp_s))


def run_stress_benchmark(duration_s: float, sample_period_s: float, seed: int):
    """Compatibility wrapper returning the historical pair of row lists."""
    metrics_rows: list[dict[str, Any]] = []
    errors_rows: list[dict[str, Any]] = []
    for row_type, row in iter_stress_benchmark(duration_s, sample_period_s, seed):
        (metrics_rows if row_type == "metric" else errors_rows).append(row)
    return metrics_rows, errors_rows


def aggregate_stress(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for scenario in sorted({row["scenario"] for row in rows}):
        for method in sorted({row["method"] for row in rows}):
            values = [float(row["mean_error_m"]) for row in rows
                      if row["scenario"] == scenario and row["method"] == method
                      and row.get("mean_error_m") is not None]
            result[f"{scenario}/{method}"] = statistics.mean(values) if values else None
    return result


class StressSummary:
    """Compact aggregate equivalent to ``aggregate_stress`` for streamed rows."""

    def __init__(self) -> None:
        self._scenarios: set[tuple[str, int]] = set()
        self._methods: set[str] = set()
        self._totals: dict[tuple[str, str], float] = {}
        self._counts: dict[tuple[str, str], int] = {}

    @property
    def scenario_count(self) -> int:
        return len(self._scenarios)

    def add(self, row: dict[str, Any]) -> None:
        scenario = str(row["scenario"])
        method = str(row["method"])
        self._scenarios.add((scenario, int(row["anchor_count"])))
        self._methods.add(method)
        value = row.get("mean_error_m")
        if value is not None:
            key = (scenario, method)
            self._totals[key] = self._totals.get(key, 0.0) + float(value)
            self._counts[key] = self._counts.get(key, 0) + 1

    def means(self) -> dict[str, float | None]:
        scenarios = sorted({scenario for scenario, _ in self._scenarios})
        return {
            f"{scenario}/{method}": (
                self._totals[(scenario, method)] / self._counts[(scenario, method)]
                if self._counts.get((scenario, method)) else None
            )
            for scenario in scenarios for method in sorted(self._methods)
        }


def create_cost_model() -> list[dict[str, Any]]:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    entries = [("component", "Tag: MCU + IMU + RFID + enclosure + battery", 100),
               ("component", "Anchor: ESP32-C6 + sub-GHz radio + power", 8),
               ("component", "Local gateway/computer", 1),
               ("component", "Shared infrastructure", 1),
               ("derived_metric", "Cost per animal (100-head reference)", 100),
               ("derived_metric", "Cost per hectare (100-ha reference)", 100)]
    return [{"record_type": kind, "component": name, "unit_cost": "", "currency": "BRL",
             "source_status": "PRICE_RESEARCH_REQUIRED", "quantity": qty,
             "date": today, "notes": "No verified supplier quote; blank cost is intentional."}
            for kind, name, qty in entries]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(rows)


class ErrorCsvWriter:
    """Stream benchmark errors while keeping only per-method aggregates."""

    fields = ("animal_count", "anchor_count", "seed", "method", "tag_id",
              "timestamp_s", "error_m", "evidence")

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("w", newline="", encoding="utf-8")
        self._writer = None
        self.row_count = 0

    def append(self, row: dict[str, Any]) -> None:
        if self._writer is None:
            self._writer = csv.DictWriter(self._stream, fieldnames=self.fields)
            self._writer.writeheader()
        self._writer.writerow(row)
        self.row_count += 1

    def close(self) -> None:
        if not self._stream.closed:
            self._stream.close()

    def __len__(self) -> int:
        return self.row_count


class CsvStreamWriter:
    """Write a known-schema CSV incrementally and publish it on success."""

    def __init__(self, path: Path, fields: tuple[str, ...]):
        self.path = path
        self.temporary_path = path.with_name(path.name + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.temporary_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._stream, fieldnames=fields)
        self._writer.writeheader()

    def append(self, row: dict[str, Any]) -> None:
        self._writer.writerow(row)

    def __enter__(self) -> "CsvStreamWriter":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stream.close()
        if exc_type is None:
            self.temporary_path.replace(self.path)
        else:
            self.temporary_path.unlink(missing_ok=True)


def make_cdf_plot(values: dict[str, Any], path: Path,
                  repeated_series: dict[str, tuple[float, int]] | None = None) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 5))
    for method, (value, count) in (repeated_series or {}).items():
        if count:
            # A repeated value forms a vertical CDF segment; plotting its two
            # endpoints avoids retaining a large array of identical values.
            ax.plot([value, value], [1.0 / count, 1.0], label=method)
    for method, data in values.items():
        if data:
            if isinstance(data, array):
                xs = np.sort(np.frombuffer(data, dtype=np.float64))
            else:
                xs = np.sort(np.asarray(data, dtype=float))
            ys = np.arange(1, len(xs) + 1, dtype=float) / len(xs)
            ax.plot(xs, ys, label=method)
    ax.set_xlabel("Localization error (m)")
    ax.set_ylabel("Cumulative fraction")
    ax.set_title("Simulated localization error CDF")
    ax.grid(True, alpha=0.25)
    if any(values.values()) or any(count for _, count in (repeated_series or {}).values()):
        ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    import matplotlib.pyplot as plt
    plt.close(fig)


def aggregate_errors(values: dict[str, array], gps_error_count: int) -> dict[str, float | None]:
    return {method: (0.0 if gps_error_count else None) if method == "gps_oracle_reference"
            else statistics.mean(values[method]) if values.get(method) else None
            for method in ("gps_oracle_reference", *METHODS)}


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = ["# Cattle RF benchmark report", "",
             "**Evidence: SIMULATED. Not validated on a physical farm.**", "",
             f"- Profile: {summary['profile']}", f"- Scenario count: {summary['scenario_count']}",
             f"- Runtime: {summary['runtime_seconds']:.1f} seconds", "",
             "## Mean localization error by method", "", "| Method | Mean error (m) |", "|---|---:|"]
    for method, value in summary["mean_error_by_method_m"].items():
        lines.append(f"| {method} | {value:.2f} |" if value is not None else f"| {method} | unavailable |")
    lines.extend(["", "## Interpretation and limits", "",
                  "All metrics come from a seeded software model and do not establish field performance. RSSI varies with assumed path loss, shadowing, obstacles, and packet loss. Fingerprint models train on distinct seeds and are evaluated on separate seeds; this still does not prove transfer to a farm.", "",
                  summary["energy_profile_note"], "",
                  "Cost entries are marked PRICE_RESEARCH_REQUIRED; blank values are not estimates.", "",
                  "GPS appears only as a zero-error oracle computed after inference from isolated truth; it is never passed to an estimator.",
                  "Robustness axes: stress_metrics.csv and stress_errors.csv.", "",
                  "Raw data: metrics.csv, localization_errors.csv, energy.csv. CDF: plots/localization_error_cdf.png."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
