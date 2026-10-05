"""Deterministic activity plans for the virtual animal.

The activity labels drive only Gazebo-side motion controllers. The firmware
input contract contains physical IMU samples only.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MotionPhase:
    duration_s: float
    behavior: str
    forward_speed_m_s: float = 0.0
    leg_frequency_hz: float = 0.0
    head_amplitude_rad: float = 0.0
    ear_amplitude_rad: float = 0.0
    head_offset_rad: float = 0.0


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    phases: tuple[MotionPhase, ...]
    tag_mass_g: float = 27.1588325228
    attachment_position_m: tuple[float, float, float] = (0.0, 0.025, -0.015)
    attachment_stiffness_nm_rad: float = 0.1
    attachment_damping_nm_s_rad: float = 0.02
    attachment_limit_rad: float = 0.523599
    seed: int = 20261003

    @property
    def duration_s(self) -> float:
        return sum(phase.duration_s for phase in self.phases)


SCENARIOS: dict[str, Scenario] = {
    "01_standing": Scenario("01_standing", "Animal and tag remain at rest.", (
        MotionPhase(10.0, "STANDING"),)),
    "02_walking": Scenario("02_walking", "Steady procedural walk cycle.", (
        MotionPhase(2.0, "STANDING"), MotionPhase(12.0, "WALKING", 0.55, 1.8, 0.08, 0.18),
        MotionPhase(2.0, "STANDING"))),
    "03_running": Scenario("03_running", "Short deterministic procedural run cycle.", (
        MotionPhase(2.0, "STANDING"), MotionPhase(8.0, "RUNNING", 1.6, 3.2, 0.16, 0.32))),
    "04_head_shake": Scenario("04_head_shake", "Head oscillation with a passive ear/tag response.", (
        MotionPhase(2.0, "STANDING"), MotionPhase(4.0, "HEAD_SHAKE", 0.0, 0.0, 0.25, 0.25),
        MotionPhase(2.0, "STANDING"))),
    "10_ear_flick": Scenario("10_ear_flick", "Short isolated ear flick followed by recovery.", (
        MotionPhase(2.0, "STANDING"), MotionPhase(0.8, "EAR_FLICK", 0.0, 0.0, 0.0, 0.42),
        MotionPhase(2.0, "STANDING"))),
    "11_lower_head": Scenario("11_lower_head", "Smooth head lowering and hold.", (
        MotionPhase(2.0, "STANDING"), MotionPhase(2.0, "LOWER_HEAD", head_offset_rad=-0.22),
        MotionPhase(2.0, "STANDING"))),
    "12_raise_head": Scenario("12_raise_head", "Smooth head raising and hold.", (
        MotionPhase(2.0, "STANDING"), MotionPhase(2.0, "RAISE_HEAD", head_offset_rad=0.18),
        MotionPhase(2.0, "STANDING"))),
    "05_mixed_activity": Scenario("05_mixed_activity", "Standing, walking, head shake, then rest.", (
        MotionPhase(3.0, "STANDING"), MotionPhase(8.0, "WALKING", 0.5, 1.7, 0.08, 0.16),
        MotionPhase(3.0, "HEAD_SHAKE", 0.0, 0.0, 0.24, 0.22), MotionPhase(3.0, "STANDING"))),
    "06_heavy_tag": Scenario("06_heavy_tag", "Walking with a 40 g assumed tag mass.", (
        MotionPhase(2.0, "STANDING"), MotionPhase(10.0, "WALKING", 0.5, 1.8, 0.08, 0.18),
        MotionPhase(2.0, "STANDING")),
        tag_mass_g=40.0),
    "07_attachment_variation": Scenario("07_attachment_variation", "Walking with an offset ear attachment.", (
        MotionPhase(2.0, "STANDING"), MotionPhase(10.0, "WALKING", 0.5, 1.8, 0.08, 0.18)),
        attachment_position_m=(0.008, 0.035, -0.02)),
    "08_radio_event": Scenario("08_radio_event", "Motion trace for a firmware radio-event replay.", (
        MotionPhase(2.0, "STANDING"), MotionPhase(8.0, "WALKING", 0.5, 1.8, 0.08, 0.18))),
    "09_long_simulation": Scenario("09_long_simulation", "One virtual hour of low-rate walking/rest cycles.", (
        MotionPhase(1800.0, "WALKING", 0.0, 1.6, 0.06, 0.14),
        MotionPhase(1800.0, "STANDING"))),
    "scenario_random_activity": Scenario("scenario_random_activity", "Seeded activity schedule represented as fixed phases.", (
        MotionPhase(3.0, "STANDING"), MotionPhase(6.0, "WALKING", 0.45, 1.6, 0.08, 0.14),
        MotionPhase(3.0, "HEAD_SHAKE", 0.0, 0.0, 0.4, 0.2), MotionPhase(5.0, "STANDING"))),
}


def get_scenario(name: str) -> Scenario:
    """Return a known scenario or accept its short CLI alias."""
    aliases = {"standing": "01_standing", "walking": "02_walking", "running": "03_running",
               "head-shake": "04_head_shake", "mixed": "05_mixed_activity",
               "ear-flick": "10_ear_flick", "lower-head": "11_lower_head",
               "raise-head": "12_raise_head",
               "heavy-tag": "06_heavy_tag", "attachment-variation": "07_attachment_variation",
               "radio-event": "08_radio_event", "long-simulation": "09_long_simulation",
               "random-activity": "scenario_random_activity"}
    key = aliases.get(name, name)
    try:
        return SCENARIOS[key]
    except KeyError as exc:
        raise ValueError(f"unknown MVP3 scenario {name!r}; choose from {', '.join(SCENARIOS)}") from exc
