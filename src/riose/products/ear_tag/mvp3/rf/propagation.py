"""Optional CPU propagation estimate, kept separate from logical RF events."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Protocol, Sequence


class RFPropagationBackend(Protocol):
    """Interface for a future simple or ray-tracing propagation backend."""

    name: str

    def estimate(self, tx_power_dbm: float, frequency_hz: float,
                 transmitter_xyz_m: Sequence[float], receiver_xyz_m: Sequence[float]) -> "PropagationEstimate": ...


@dataclass(frozen=True)
class PropagationEstimate:
    status: str
    backend: str
    distance_m: float
    frequency_hz: float
    tx_power_dbm: float
    path_loss_db: float
    rssi_dbm: float
    provenance: str
    assumptions: tuple[str, ...]
    antenna_model_status: str = "ANTENNA_MODEL_UNVALIDATED"
    antenna_gain_used_dbi: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class SimpleBackend:
    """Free-space Friis estimate with an explicitly isotropic antenna proxy.

    This is a coarse simulated link-budget estimate. It omits polarization,
    antenna orientation/pattern, ground reflection, occlusion, and multipath.
    The estimate never changes whether a `LOGICAL_RF_EVENT` was transmitted or
    whether the anchor recorded that event.
    """

    name = "SIMULATED_SIMPLE_RF"
    SPEED_OF_LIGHT_M_S = 299_792_458.0

    def estimate(self, tx_power_dbm: float, frequency_hz: float,
                 transmitter_xyz_m: Sequence[float], receiver_xyz_m: Sequence[float]) -> PropagationEstimate:
        values = (tx_power_dbm, frequency_hz, *transmitter_xyz_m, *receiver_xyz_m)
        if len(transmitter_xyz_m) != 3 or len(receiver_xyz_m) != 3:
            raise ValueError("RF endpoint positions must each have three coordinates")
        if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in values):
            raise ValueError("RF inputs must be finite numbers")
        if frequency_hz <= 0:
            raise ValueError("frequency_hz must be positive")
        distance_m = math.dist(tuple(map(float, transmitter_xyz_m)), tuple(map(float, receiver_xyz_m)))
        if distance_m <= 0:
            raise ValueError("RF endpoints must have nonzero separation")
        path_loss_db = 20 * math.log10(4 * math.pi * distance_m * frequency_hz / self.SPEED_OF_LIGHT_M_S)
        # Zero-dBi isotropic gain is a placeholder assumption, not an antenna result.
        return PropagationEstimate(
            status=self.name, backend=self.name, distance_m=distance_m,
            frequency_hz=float(frequency_hz), tx_power_dbm=float(tx_power_dbm),
            path_loss_db=path_loss_db, rssi_dbm=float(tx_power_dbm) - path_loss_db,
            provenance="SIMULATED", assumptions=(
                "Free-space Friis path loss.",
                "Both antenna gains set to assumed 0 dBi isotropic placeholders.",
                "No orientation, obstruction, polarization, ground reflection, or multipath model.",
            ),
        )


__all__ = ["RFPropagationBackend", "PropagationEstimate", "SimpleBackend"]
