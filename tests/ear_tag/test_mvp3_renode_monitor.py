from __future__ import annotations

import pytest

from riose.products.ear_tag.mvp3.bridge.renode_monitor import RenodeMonitorClient


def _command_spy() -> tuple[RenodeMonitorClient, list[str]]:
    client = object.__new__(RenodeMonitorClient)
    commands: list[str] = []
    client.command = commands.append  # type: ignore[method-assign]
    return client, commands


def test_monitor_client_steps_virtual_time_explicitly() -> None:
    client, commands = _command_spy()

    client.run_for(0.076923076923)

    assert commands == ['emulation RunFor "0.076923076923"']


def test_monitor_client_injects_one_finite_gazebo_sample_in_g() -> None:
    client, commands = _command_spy()

    client.inject_acceleration_sample_from_gazebo(0.575174963, 0.793846411, 0.000129871)

    assert commands == [
        "sysbus.i2c1.imu InjectAccelerationSampleFromGazebo 0.575174963 0.793846411 0.000129871"
    ]


def test_monitor_client_injects_sample_at_its_timestamp() -> None:
    client, commands = _command_spy()

    client.inject_timed_acceleration_sample_from_gazebo(0.1, -0.2, 1.0, 0.08)

    assert commands == [
        "sysbus.i2c1.imu InjectAccelerationSampleFromGazeboAtTime 0.1 -0.2 1 0.08"
    ]


@pytest.mark.parametrize(("response", "expected"), [
    ("Current virtual time: 00:00:00.000000000", 0.0),
    ("Current virtual time: 00:00:01.250000000", 1.25),
    ("Current virtual time: 01:02:03.500000000", 3723.5),
])
def test_monitor_client_parses_observed_virtual_clock(response: str, expected: float) -> None:
    client = object.__new__(RenodeMonitorClient)
    client.current_time = lambda: response  # type: ignore[method-assign]

    assert client.observed_virtual_time_s() == expected


@pytest.mark.parametrize("duration", [0, -0.1, float("nan"), float("inf")])
def test_monitor_client_rejects_invalid_virtual_time(duration: float) -> None:
    client, commands = _command_spy()

    with pytest.raises(ValueError, match="finite and positive"):
        client.run_for(duration)

    assert commands == []


def test_monitor_client_rejects_nonfinite_acceleration() -> None:
    client, commands = _command_spy()

    with pytest.raises(ValueError, match="finite values"):
        client.inject_acceleration_sample_from_gazebo(0, float("nan"), 1)

    assert commands == []
