"""Reproducible end-to-end farm/RF episode generator."""

from __future__ import annotations

import math
from typing import Mapping, Sequence

from cattle_rf.contracts import Anchor, FarmConfig, SimulationEpisode
from cattle_rf.sim.farm import DEFAULT_OBSTACLES, FarmSimulator, Obstacle
from cattle_rf.sim.rf import RFConfig, simulate_observations


def generate_anchors(config: FarmConfig,
                     obstacles: Sequence[Obstacle] | None = None) -> tuple[Anchor, ...]:
    """Place anchors in a deterministic perimeter-plus-interior grid, outside obstacles."""
    n = max(0, config.anchor_count)
    if not n:
        return ()
    blocked_areas = tuple(DEFAULT_OBSTACLES if obstacles is None else obstacles)
    points: list[tuple[float, float]] = []
    # Begin with corners, then edge midpoints, then a regular interior lattice.
    candidates = [
        (0.0, 0.0), (config.width_m, 0.0), (config.width_m, config.height_m),
        (0.0, config.height_m),
        (config.width_m / 2, 0.0), (config.width_m, config.height_m / 2),
        (config.width_m / 2, config.height_m), (0.0, config.height_m / 2),
    ]
    # Oversample the interior grid so filtering obstacle-covered points still
    # leaves enough locations for the requested anchor count.
    cols = max(2, math.ceil(math.sqrt(n * 4)))
    rows = max(2, math.ceil(n * 4 / cols))
    for r in range(rows):
        for c in range(cols):
            candidates.append((config.width_m * (c + 0.5) / cols,
                               config.height_m * (r + 0.5) / rows))
    for pt in candidates:
        if any(obstacle.contains(*pt) for obstacle in blocked_areas):
            continue
        if pt not in points:
            points.append(pt)
        if len(points) == n:
            break
    if len(points) < n:
        raise ValueError(f"could not place {n} anchors outside the configured obstacles")
    return tuple(Anchor(f"anchor-{i + 1:02d}", x, y) for i, (x, y) in enumerate(points))


def simulate_episode(config: FarmConfig, anchors: Sequence[Anchor] | None = None,
                     obstacles: Sequence[Obstacle] | None = None,
                     rf_config: RFConfig | None = None,
                     escape_targets: Mapping[str, tuple[float, float]] | None = None) -> SimulationEpisode:
    """Generate a reproducible episode with inference observations and separate truth."""
    world_obstacles = tuple(DEFAULT_OBSTACLES if obstacles is None else obstacles)
    anchor_set = tuple(generate_anchors(config, world_obstacles) if anchors is None else anchors)
    farm = FarmSimulator(config, world_obstacles, escape_targets)
    truth, motion = farm.generate()
    motion_map = {(float(m["timestamp_s"]), str(m["tag_id"])): m for m in motion}
    observations = simulate_observations(config, anchor_set, truth, motion_map,
                                         world_obstacles, rf_config)
    return SimulationEpisode(
        observations=observations,
        ground_truth=truth,
        anchors=anchor_set,
        metadata={
            "status": "SIMULATED",
            "seed": config.seed,
            "farm_width_m": config.width_m,
            "farm_height_m": config.height_m,
            "animal_count": config.animal_count,
            "sample_period_s": config.sample_period_s,
            "obstacle_count": len(world_obstacles),
            "escaped_tags": sorted((escape_targets or {}).keys()),
            "rf_model": "log-distance plus correlated shadow fading, obstacle attenuation, noise and simplified interference",
            "rf_frequency_mhz": (rf_config or RFConfig()).frequency_mhz,
        },
    )
