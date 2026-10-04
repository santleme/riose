"""Explicitly experimental/future sensor interfaces."""

from __future__ import annotations

from dataclasses import dataclass
import math
import random

from ..domain.contracts import EvidenceStatus


@dataclass(frozen=True, slots=True)
class CSIObservation:
    timestamp_s: float
    tag_id: str
    anchor_id: str
    amplitude: tuple[float, ...]
    phase_rad: tuple[float, ...]
    status: EvidenceStatus = EvidenceStatus.EXPERIMENTAL


def simulate_wifi_csi(timestamp_s: float, tag_id: str, anchor_id: str,
                      movement_intensity: float, seed: int = 0,
                      subcarriers: int = 32) -> CSIObservation:
    """Toy channel fluctuation model; not a Wi-Fi PHY or CSI measurement."""
    rng = random.Random(seed)
    amplitude, phase = [], []
    for i in range(subcarriers):
        multipath = sum(rng.random() * math.cos(i * (0.13 * path + 0.03) + rng.random())
                        for path in range(1, 4))
        movement = max(0.0, movement_intensity) * rng.gauss(0, 0.05)
        amplitude.append(max(0.0, 1.0 + 0.15 * multipath + movement))
        phase.append(math.atan2(math.sin(0.2 * multipath + rng.gauss(0, 0.05)),
                                math.cos(0.2 * multipath + rng.gauss(0, 0.05))))
    return CSIObservation(timestamp_s, tag_id, anchor_id, tuple(amplitude), tuple(phase))


@dataclass(frozen=True, slots=True)
class CellularObservation:
    timestamp_s: float
    network_type: str | None = None
    cell_id: str | None = None
    rssi_dbm: float | None = None
    status: EvidenceStatus = EvidenceStatus.FUTURE
