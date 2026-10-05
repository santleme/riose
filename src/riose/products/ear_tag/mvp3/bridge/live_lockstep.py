"""Deterministic Gazebo-master stepping with live Renode IMU injection.

This adapter deliberately advances both virtual clocks under host control.
Gazebo advances in fixed physics batches; timestamped IMU messages produced by
those batches are injected into the running Renode peripheral before Renode is
advanced to the same Gazebo timestamp. Wall-clock speed is consequently not
used as a synchronization signal.
"""
from __future__ import annotations

import json
import math
import queue
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .gazebo_to_resd import G0_M_S2
from .renode_monitor import RenodeMonitorClient


@dataclass(frozen=True)
class GazeboIMUSample:
    timestamp_s: Decimal
    acceleration_g: tuple[Decimal, Decimal, Decimal]
    orientation_xyzw: tuple[Decimal, Decimal, Decimal, Decimal] = (
        Decimal(0), Decimal(0), Decimal(0), Decimal(1))


class GazeboIMUSubscriber:
    """Keep one Gazebo Transport node for IMU subscription and world stepping."""

    def __init__(self, env: dict[str, str], topic: str,
                 control_topic: str = "/world/riose_mvp3/control",
                 clock_topic: str = "/world/riose_mvp3/clock"):
        worker = Path(__file__).with_name("gazebo_lockstep_worker.py")
        self.process = subprocess.Popen(
            ["/usr/bin/python3", str(worker), topic, control_topic, clock_topic],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        self.samples: queue.Queue[GazeboIMUSample] = queue.Queue()
        self.errors: queue.Queue[str] = queue.Queue()
        self.responses: queue.Queue[dict] = queue.Queue()
        self._clock_condition = threading.Condition()
        self._latest_clock_s: Decimal | None = None
        self._write_lock = threading.Lock()
        self._command_id = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._read, name="riose-gazebo-imu", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            self.close()
            raise RuntimeError("Gazebo Transport lockstep worker did not become ready")

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line_number, line in enumerate(self.process.stdout, 1):
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                if message.get("kind") == "ready":
                    self._ready.set()
                elif message.get("kind") == "response":
                    self.responses.put(message)
                elif message.get("kind") == "imu":
                    timestamp = Decimal(str(message["timestamp_s"]))
                    acceleration = tuple(Decimal(str(message["linear_acceleration"][axis]))
                                         for axis in "xyz")
                    orientation = tuple(Decimal(str(message.get("orientation", {}).get(axis, default)))
                                        for axis, default in zip("xyzw", (0, 0, 0, 1)))
                    self.samples.put(GazeboIMUSample(
                        timestamp, tuple(value / G0_M_S2 for value in acceleration), orientation))
                elif message.get("kind") == "clock":
                    with self._clock_condition:
                        self._latest_clock_s = Decimal(str(message["timestamp_s"]))
                        self._clock_condition.notify_all()
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                self.errors.put(f"Gazebo IMU subscriber line {line_number}: {exc}")
        code = self.process.poll()
        if code not in (None, 0):
            self.errors.put(f"Gazebo Transport worker exited ({code})")

    def _send_step(self, steps: int) -> dict:
        if self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeError("Gazebo Transport worker is not running")
        self._command_id += 1
        command_id = self._command_id
        with self._write_lock:
            self.process.stdin.write(json.dumps({"id": command_id, "op": "step", "steps": steps}) + "\n")
            self.process.stdin.flush()
        deadline = time.monotonic() + 8.0
        deferred = []
        while time.monotonic() < deadline:
            try:
                response = self.responses.get(timeout=0.1)
            except queue.Empty:
                if not self.errors.empty():
                    raise RuntimeError(self.errors.get())
                if self.process.poll() is not None:
                    raise RuntimeError("Gazebo Transport worker exited during a world step")
                continue
            if response.get("id") == command_id:
                for item in deferred:
                    self.responses.put(item)
                return response
            deferred.append(response)
        for item in deferred:
            self.responses.put(item)
        raise RuntimeError(f"Gazebo Transport lockstep step {command_id} timed out")

    def _wait_clock(self, target_s: Decimal, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._clock_condition:
            while time.monotonic() < deadline:
                current = self._latest_clock_s
                if current is not None:
                    if abs(current - target_s) <= Decimal("0.000000001"):
                        return True
                    if current > target_s + Decimal("0.000000001"):
                        raise RuntimeError(
                            f"Gazebo clock advanced beyond requested barrier: {current}s > {target_s}s")
                self._clock_condition.wait(timeout=max(0.001, deadline - time.monotonic()))
        return False

    def step(self, steps: int, target_s: Decimal) -> None:
        response = self._send_step(steps)
        if not response.get("success"):
            # Transport can time out after Gazebo already applied multi_step.
            # Never resend an ambiguous request: that could advance twice.
            if self._wait_clock(target_s, 2.0):
                return
            latest = self._latest_clock_s
            if latest is not None and latest > target_s + Decimal("0.000000001"):
                raise RuntimeError(f"Gazebo returned a rejected step and overshot target {target_s}s")
            raise RuntimeError("Gazebo Transport lockstep step failed: " +
                               str(response.get("error", "service rejected request")) +
                               f" (sent={response.get('sent')}, data={response.get('data')}); "
                               f"Gazebo /clock remained at {self._latest_clock_s}s, "
                               f"requested {target_s}s")
        if not self._wait_clock(target_s, 1.0):
            raise RuntimeError(f"Gazebo acknowledged a step but /clock did not reach {target_s}s "
                               f"(last={self._latest_clock_s}s)")

    def receive_through(self, target_s: Decimal, *, timeout_s: float = 2.0) -> list[GazeboIMUSample]:
        """Collect ordered sensor output through a paused Gazebo batch boundary."""
        deadline = time.monotonic() + timeout_s
        result: list[GazeboIMUSample] = []
        while not result or result[-1].timestamp_s < target_s - Decimal("0.001"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                latest = result[-1].timestamp_s if result else None
                raise RuntimeError(f"Gazebo IMU stream did not reach {target_s}s (last sample={latest})")
            try:
                result.append(self.samples.get(timeout=remaining))
            except queue.Empty as exc:
                if not self.errors.empty():
                    raise RuntimeError(self.errors.get()) from exc
                if self.process.poll() is not None:
                    raise RuntimeError("Gazebo IMU subscriber exited before reaching batch boundary") from exc
                raise RuntimeError(f"Gazebo IMU stream did not reach {target_s}s") from exc
        while True:
            try:
                result.append(self.samples.get_nowait())
            except queue.Empty:
                return result

    def close(self) -> None:
        if self.process.poll() is None and self.process.stdin is not None:
            try:
                with self._write_lock:
                    self.process.stdin.write('{"op":"close"}\n')
                    self.process.stdin.flush()
            except OSError:
                pass
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stdin is not None:
            self.process.stdin.close()


def reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RenodeLiveSession:
    """Own a Renode process and its monitor for a fixed-time co-simulation."""

    def __init__(self, repo_root: Path, elf: Path, log_path: Path, *, timeout_s: float = 30.0):
        if not elf.is_file():
            raise RuntimeError(f"Zephyr Renode ELF does not exist: {elf}")
        renode = __import__("shutil").which("renode")
        if not renode:
            raise RuntimeError("Renode executable is not available on PATH")
        self.port = reserve_loopback_port()
        resc = repo_root / "hardware/renode/riose_stm32l0.resc"
        command = [renode, "--plain", "--port", str(self.port), "--disable-gui",
                   "--keep-temporary-files", "--execute", f"include @{resc}"]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(command, cwd=repo_root, stdout=self._log,
                                        stderr=subprocess.STDOUT, text=True,
                                        start_new_session=True)
        self.monitor: RenodeMonitorClient | None = None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Renode exited during startup ({self.process.returncode}); inspect {log_path}")
            try:
                self.monitor = RenodeMonitorClient(port=self.port, timeout_s=timeout_s)
                break
            except OSError:
                time.sleep(0.1)
        if self.monitor is None:
            self.close()
            raise RuntimeError(f"Renode monitor did not open on 127.0.0.1:{self.port}")
        self.monitor.command(f"sysbus LoadELF @{elf.resolve()}")
        self.virtual_time_s = Decimal(0)

    def advance_to(self, target_s: Decimal) -> None:
        delta = target_s - self.virtual_time_s
        if delta < Decimal("-0.000000001"):
            raise RuntimeError(f"Renode clock would move backward: {self.virtual_time_s}s -> {target_s}s")
        if delta > Decimal("0.000000001"):
            self.monitor.run_for(delta)
            self.virtual_time_s = target_s

    def inject(self, sample: GazeboIMUSample) -> None:
        if self.monitor is None:
            raise RuntimeError("Renode monitor is not connected")
        if abs(sample.timestamp_s - self.virtual_time_s) > Decimal("0.000001"):
            raise RuntimeError(
                f"Gazebo IMU sample at {sample.timestamp_s}s must be injected at the same "
                f"Renode time, currently {self.virtual_time_s}s")
        values = tuple(float(value) for value in sample.acceleration_g)
        if not all(math.isfinite(value) for value in values):
            raise RuntimeError("Gazebo IMU sample contains non-finite acceleration")
        self.monitor.inject_timed_acceleration_sample_from_gazebo(*values, float(sample.timestamp_s))

    def observed_time_s(self) -> Decimal:
        if self.monitor is None:
            raise RuntimeError("Renode monitor is not connected")
        return Decimal(str(self.monitor.observed_virtual_time_s()))

    def query(self, command: str) -> str:
        if self.monitor is None:
            raise RuntimeError("Renode monitor is not connected")
        return self.monitor.command(command)

    def close(self) -> None:
        if self.monitor is not None:
            self.monitor.close()
            self.monitor = None
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if not self._log.closed:
            self._log.close()

    def __enter__(self) -> "RenodeLiveSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def advance_gazebo_and_renode(
    *, executable: str, env: dict[str, str], duration_s: Decimal,
    monitor: RenodeLiveSession, subscriber: GazeboIMUSubscriber,
    control_topic: str = "/world/riose_mvp3/control",
    physics_step_s: Decimal = Decimal("0.001"),
    batch_steps: int = 200, evidence_path: Path,
    status_callback: Callable[[Decimal, GazeboIMUSample | None, Decimal], None] | None = None,
) -> dict[str, Any]:
    """Step Gazebo as clock master and feed every timestamped IMU sample live.

    Every raw Gazebo sensor message is injected, including values between the
    LIS2DW12 output reads. The peripheral samples its wake comparator at the
    configured ODR while retaining the latest raw physical value. Renode reaches
    each sample timestamp before that sample is injected.
    """
    total_steps = int(duration_s / physics_step_s)
    if Decimal(total_steps) * physics_step_s != duration_s:
        raise ValueError("duration must be an exact multiple of Gazebo physics_step_s")
    if batch_steps < 1:
        raise ValueError("batch_steps must be positive")
    if Decimal(batch_steps) * physics_step_s > Decimal("0.5"):
        raise ValueError("Gazebo lockstep batches may not exceed 0.5 virtual seconds")
    sim_time = Decimal(0)
    sample_count = 0
    raw_sample_count = 0
    batch_count = 0
    max_observed_clock_error = Decimal(0)
    last_sensor_timestamp = Decimal(-1)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with evidence_path.open("w", encoding="utf-8") as evidence:
        evidence.write(json.dumps({"event": "LOCKSTEP_START", "sim_time_s": "0",
                                  "renode_time_s": str(monitor.virtual_time_s),
                                  "clock_master": "GAZEBO_SIMULATION_TIME",
                                  "physics_step_s": str(physics_step_s),
                                  "batch_steps": batch_steps}, sort_keys=True) + "\n")
        while batch_count * batch_steps < total_steps:
            steps = min(batch_steps, total_steps - batch_count * batch_steps)
            next_sim_time = sim_time + Decimal(steps) * physics_step_s
            subscriber.step(steps, next_sim_time)
            sim_time = next_sim_time
            batch_count += 1
            samples = subscriber.receive_through(sim_time)
            raw_sample_count += len(samples)
            for sample in samples:
                if sample.timestamp_s <= last_sensor_timestamp:
                    raise RuntimeError("Gazebo IMU timestamps are not strictly increasing")
                if sample.timestamp_s > sim_time + physics_step_s:
                    raise RuntimeError(
                        f"Gazebo published IMU sample {sample.timestamp_s}s beyond current clock {sim_time}s")
                last_sensor_timestamp = sample.timestamp_s
                monitor.advance_to(sample.timestamp_s)
                monitor.inject(sample)
                sample_count += 1
                evidence.write(json.dumps({
                    "event": "IMU_INJECTED", "simulation_timestamp_s": str(sample.timestamp_s),
                    "renode_injection_target_time_s": str(monitor.virtual_time_s),
                    "acceleration_g": [str(value) for value in sample.acceleration_g],
                    "sample_index": sample_count,
                }, sort_keys=True) + "\n")
            monitor.advance_to(sim_time)
            observed_renode_time = monitor.observed_time_s()
            clock_error = sim_time - observed_renode_time
            max_observed_clock_error = max(max_observed_clock_error, abs(clock_error))
            if abs(clock_error) > Decimal("0.000001"):
                raise RuntimeError(
                    f"observed Renode clock missed Gazebo barrier by {clock_error}s "
                    f"(Gazebo={sim_time}s, Renode={observed_renode_time}s)")
            evidence.write(json.dumps({
                "event": "CLOCK_BARRIER", "simulation_timestamp_s": str(sim_time),
                "renode_timestamp_s": str(observed_renode_time),
                "clock_error_s": str(clock_error),
                "batch_index": batch_count, "gazebo_imu_samples": len(samples),
                "gazebo_imu_samples_injected": sample_count,
            }, sort_keys=True) + "\n")
            if status_callback is not None:
                status_callback(sim_time, samples[-1] if samples else None, observed_renode_time)
    if sample_count == 0:
        raise RuntimeError("Gazebo lockstep completed without receiving any IMU samples")
    return {"status": "SIMULATED", "mode": "GAZEBO_MASTER_PAUSED_STEPPING",
            "clock_master": "GAZEBO_SIMULATION_TIME", "live_lockstep": True,
            "simulation_duration_s": str(sim_time), "renode_duration_s": str(observed_renode_time),
            "clock_error_s": str(sim_time - observed_renode_time),
            "max_observed_clock_error_s": str(max_observed_clock_error),
            "renode_clock_observation": "MONITOR_CURRENT_TIME_AT_EACH_BATCH_BARRIER",
            "physics_step_s": str(physics_step_s), "steps_per_batch": batch_steps,
            "batch_count": batch_count, "gazebo_imu_raw_samples_recorded": raw_sample_count,
            "gazebo_imu_samples_injected": sample_count,
            "injection_policy": "EVERY_TIMESTAMPED_GAZEBO_IMU_SAMPLE",
            "last_gazebo_imu_timestamp_s": str(last_sensor_timestamp),
            "injected_acceleration_unit": "g", "source_acceleration_unit": "m/s^2",
            "clock_evidence": str(evidence_path)}


__all__ = ["GazeboIMUSample", "GazeboIMUSubscriber", "RenodeLiveSession",
           "advance_gazebo_and_renode"]
