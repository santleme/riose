"""Portable virtual tag state machine with a replaceable hardware layer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .energy import EnergyLedger, EnergyProfile


class TagState(StrEnum):
    BOOT = "BOOT"
    SELF_TEST = "SELF_TEST"
    DEEP_SLEEP = "DEEP_SLEEP"
    IDLE = "IDLE"
    IMU_MONITORING = "IMU_MONITORING"
    RF_TX = "RF_TX"
    RF_RX = "RF_RX"
    BLE_ACTIVE = "BLE_ACTIVE"
    WIFI_ACTIVE = "WIFI_ACTIVE"
    ALERT_MODE = "ALERT_MODE"


class Activity(StrEnum):
    NORMAL = "NORMAL"
    RUNNING = "RUNNING"
    FENCE_WARNING = "FENCE_WARNING"
    STATIONARY_ANOMALY = "STATIONARY_ANOMALY"


class VirtualTagHAL(Protocol):
    """Methods implemented by native simulation or a physical driver adapter."""

    def self_test(self) -> bool: ...
    def read_imu_activity(self) -> Activity: ...
    def transmit_beacon(self, tag_id: str, high_rate: bool = False) -> None: ...
    def receive_window(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PassiveRFIDIdentity:
    """Architecture placeholder for the passive animal ID transponder."""

    identifier: str
    air_interface: str = "134.2 kHz animal RFID (ISO 11784/11785 class)"
    passive: bool = True


class PassiveRFIDReader(Protocol):
    """Replaceable reader interface; RFID is not simulated as an RF range link."""

    def read_identifier(self) -> PassiveRFIDIdentity | None: ...


@dataclass(slots=True)
class MemoryHAL:
    """Small deterministic test/demo HAL; it does not emulate RF propagation."""

    activity: Activity = Activity.NORMAL
    self_test_ok: bool = True
    transmissions: list[tuple[str, bool]] | None = None
    receive_windows: int = 0

    def __post_init__(self) -> None:
        if self.transmissions is None:
            self.transmissions = []

    def self_test(self) -> bool:
        return self.self_test_ok

    def read_imu_activity(self) -> Activity:
        return self.activity

    def transmit_beacon(self, tag_id: str, high_rate: bool = False) -> None:
        assert self.transmissions is not None
        self.transmissions.append((tag_id, high_rate))

    def receive_window(self) -> None:
        self.receive_windows += 1


@dataclass(frozen=True, slots=True)
class TagConfig:
    tag_id: str
    normal_beacon_period_s: float = 900.0
    active_beacon_period_s: float = 60.0
    alert_beacon_period_s: float = 15.0
    receive_window_s: float = 0.2
    stationary_alert_after_s: float = 4 * 3600.0

    def __post_init__(self) -> None:
        if not self.tag_id:
            raise ValueError("tag_id is required")
        if min(self.normal_beacon_period_s, self.active_beacon_period_s, self.alert_beacon_period_s) <= 0:
            raise ValueError("beacon periods must be positive")
        if self.receive_window_s < 0 or self.stationary_alert_after_s <= 0:
            raise ValueError("receive window must be non-negative and alert threshold positive")


class TagController:
    """Event-driven tag FSM; elapsed times are explicit and deterministic."""

    def __init__(self, config: TagConfig, hal: VirtualTagHAL, energy: EnergyProfile | None = None) -> None:
        self.config = config
        self.hal = hal
        self.energy_profile = energy or EnergyProfile.reference_stm32wle5()
        self.ledger = EnergyLedger(self.energy_profile)
        self.state = TagState.BOOT
        self.activity = Activity.NORMAL
        self.time_s = 0.0
        self.last_beacon_s: float | None = None
        self.stationary_s = 0.0
        self._normal_current_state = TagState.DEEP_SLEEP
        self._self_test_ok: bool | None = None

    @property
    def beacon_period_s(self) -> float:
        if self.state == TagState.ALERT_MODE:
            return self.config.alert_beacon_period_s
        if self.activity in (Activity.RUNNING, Activity.FENCE_WARNING):
            return self.config.active_beacon_period_s
        return self.config.normal_beacon_period_s

    def boot(self) -> TagState:
        if self.state != TagState.BOOT:
            return self.state
        self.state = TagState.SELF_TEST
        self._self_test_ok = self.hal.self_test()
        self.state = TagState.IDLE if self._self_test_ok else TagState.DEEP_SLEEP
        return self.state

    def set_radio_mode(self, state: TagState) -> None:
        if state not in (TagState.BLE_ACTIVE, TagState.WIFI_ACTIVE):
            raise ValueError("radio mode must be BLE_ACTIVE or WIFI_ACTIVE")
        self._normal_current_state = self.state
        self.state = state

    def end_radio_mode(self) -> None:
        if self.state in (TagState.BLE_ACTIVE, TagState.WIFI_ACTIVE):
            self.state = self._normal_current_state

    def step(self, elapsed_s: float, activity: Activity | None = None) -> TagState:
        """Advance firmware by an interval, sampling IMU and acting on cadence."""
        if elapsed_s < 0:
            raise ValueError("elapsed_s must be non-negative")
        if self.state == TagState.BOOT:
            self.boot()
        if self._self_test_ok is False:
            # A failed self-test is a latched startup fault. Keep the tag
            # asleep instead of allowing subsequent steps to transmit.
            return TagState.DEEP_SLEEP
        if self.state == TagState.SELF_TEST:
            return self.state

        self.time_s += elapsed_s
        measured = activity if activity is not None else self.hal.read_imu_activity()
        self.activity = measured
        if measured == Activity.STATIONARY_ANOMALY:
            self.stationary_s += elapsed_s
        else:
            self.stationary_s = 0.0

        if self.state == TagState.BLE_ACTIVE or self.state == TagState.WIFI_ACTIVE:
            self._record_state_energy(self.state, elapsed_s)
            return self.state

        if measured == Activity.FENCE_WARNING or self.stationary_s >= self.config.stationary_alert_after_s:
            self.state = TagState.ALERT_MODE
        elif measured == Activity.RUNNING:
            self.state = TagState.IMU_MONITORING
        else:
            self.state = TagState.DEEP_SLEEP

        base_state = self.state
        should_transmit = self.last_beacon_s is None or self.time_s - self.last_beacon_s >= self.beacon_period_s
        tx_duration = min(elapsed_s, 0.1) if should_transmit else 0.0
        rx_duration = (min(self.config.receive_window_s, max(0.0, elapsed_s - tx_duration))
                       if should_transmit else 0.0)
        # Partition each elapsed wall-clock interval across mutually exclusive
        # states. TX/RX are not added on top of the full sleep/monitor interval.
        self._record_state_energy(base_state, max(0.0, elapsed_s - tx_duration - rx_duration))
        if should_transmit:
            self.state = TagState.RF_TX
            self.hal.transmit_beacon(self.config.tag_id, high_rate=self.beacon_period_s != self.config.normal_beacon_period_s)
            self._record_state_energy(TagState.RF_TX, tx_duration)
            if rx_duration:
                self.state = TagState.RF_RX
                self.hal.receive_window()
                self._record_state_energy(TagState.RF_RX, rx_duration)
            self.last_beacon_s = self.time_s
            self.state = TagState.ALERT_MODE if self.activity == Activity.FENCE_WARNING or self.stationary_s >= self.config.stationary_alert_after_s else TagState.DEEP_SLEEP
        return self.state

    def _record_state_energy(self, state: TagState, duration_s: float) -> None:
        p = self.energy_profile
        currents = {
            TagState.DEEP_SLEEP: p.sleep_ma,
            TagState.IDLE: p.idle_ma,
            TagState.IMU_MONITORING: p.imu_monitoring_ma,
            TagState.RF_TX: p.rf_tx_ma,
            TagState.RF_RX: p.rf_rx_ma,
            TagState.BLE_ACTIVE: p.ble_active_ma,
            TagState.WIFI_ACTIVE: p.wifi_active_ma,
            TagState.ALERT_MODE: p.alert_ma,
        }
        self.ledger.record(currents.get(state, p.idle_ma), duration_s)


def detect_optional_capabilities() -> dict[str, dict[str, str | bool]]:
    """Report local tool availability without making it a runtime requirement."""
    import importlib.util
    import shutil

    west = shutil.which("west")
    wokwi = shutil.which("wokwi-cli")
    return {
        "zephyr_native_sim": {
            "available": bool(west),
            "status": "EXPERIMENTAL" if west else "FUTURE",
            "detail": f"west found at {west}" if west else "west CLI not installed; native_sim firmware build unavailable",
        },
        "wokwi": {
            "available": bool(wokwi),
            "status": "EXPERIMENTAL" if wokwi else "FUTURE",
            "detail": f"wokwi-cli found at {wokwi}" if wokwi else "Wokwi CLI not installed; virtual peripheral integration unavailable",
        },
        "sionna": {
            "available": importlib.util.find_spec("sionna") is not None,
            "status": "EXPERIMENTAL" if importlib.util.find_spec("sionna") is not None else "FUTURE",
            "detail": "Python package detection only; compatible GPU/runtime not verified",
        },
    }
