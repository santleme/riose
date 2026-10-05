from __future__ import annotations

import math

import pytest

from riose.products.ear_tag.mvp3.rf import SimpleBackend


def test_simple_backend_emits_separately_labeled_friis_estimate() -> None:
    result = SimpleBackend().estimate(14, 915_000_000, (0, 0, 1), (3, 0, 2))
    assert result.status == "SIMULATED_SIMPLE_RF"
    assert result.distance_m == pytest.approx(math.sqrt(10))
    assert result.rssi_dbm == pytest.approx(result.tx_power_dbm - result.path_loss_db)
    assert result.antenna_model_status == "ANTENNA_MODEL_UNVALIDATED"
    assert "orientation" in " ".join(result.assumptions)


@pytest.mark.parametrize("positions", [((0, 0), (1, 1, 1)), ((0, 0, 0), (0, 0, 0))])
def test_simple_backend_rejects_invalid_or_coincident_positions(positions) -> None:
    with pytest.raises(ValueError):
        SimpleBackend().estimate(14, 915_000_000, *positions)
