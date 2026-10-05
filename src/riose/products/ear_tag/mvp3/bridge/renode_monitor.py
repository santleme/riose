"""Small Telnet client for controlled Renode virtual-time/sample commands."""
from __future__ import annotations

import math
import re
import socket
import time
from typing import SupportsFloat


class RenodeMonitorError(RuntimeError):
    """Renode monitor connection, protocol, or command failure."""


class RenodeMonitorClient:
    """Issue serialized commands through Renode's loopback Telnet monitor.

    Start Renode with ``--plain --port PORT``. This client does not start or
    pause emulation implicitly; callers choose the clock policy explicitly.
    """

    _PROMPT = re.compile(rb"(?:^|[\r\n])\((?:machine-\d+|monitor)\)\s*$")
    _ANSI = re.compile(rb"\x1b\[[0-9;]*m")
    _IAC, _DONT, _DO, _WONT, _WILL, _SB, _SE = 255, 254, 253, 252, 251, 250, 240

    def __init__(self, host: str = "127.0.0.1", port: int = 12355, *, timeout_s: float = 30.0):
        if not 1 <= port <= 65535:
            raise ValueError("Renode monitor port must be in [1, 65535]")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        self._socket = socket.create_connection((host, port), timeout=timeout_s)
        self._timeout_s = timeout_s
        self._iac_state = 0
        self._iac_command: int | None = None
        self._subnegotiation = False
        self._pending_output = bytearray()
        self._settle_telnet_negotiation()
        self._drain_initial_prompt()

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> "RenodeMonitorClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def command(self, command: str) -> str:
        """Run one monitor command and return its prompt-delimited response."""
        if not isinstance(command, str) or not command.strip() or "\n" in command or "\r" in command:
            raise ValueError("command must be one nonempty line")
        try:
            self._socket.sendall(command.encode("utf-8") + b"\n")
            response = self._read_prompt()
        except (OSError, TimeoutError) as exc:
            raise RenodeMonitorError(
                f"Renode monitor command failed: {command} "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        text = self._ANSI.sub(b"", response).decode("utf-8", errors="replace")
        if "Error executing command" in text or "Error: " in text:
            raise RenodeMonitorError(text.strip())
        return text

    def run_for(self, seconds: SupportsFloat) -> str:
        """Advance Renode by an explicit virtual-time interval."""
        value = float(seconds)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("virtual-time interval must be finite and positive")
        return self.command(f'emulation RunFor "{value:.12g}"')

    def inject_acceleration_sample_from_gazebo(
        self, x_g: SupportsFloat, y_g: SupportsFloat, z_g: SupportsFloat,
    ) -> str:
        """Inject one Gazebo sensor-frame acceleration sample, in g."""
        values = tuple(float(value) for value in (x_g, y_g, z_g))
        if not all(math.isfinite(value) for value in values):
            raise ValueError("acceleration sample must contain finite values")
        axes = " ".join(f"{value:.9g}" for value in values)
        return self.command(f"sysbus.i2c1.imu InjectAccelerationSampleFromGazebo {axes}")

    def inject_timed_acceleration_sample_from_gazebo(
        self, x_g: SupportsFloat, y_g: SupportsFloat, z_g: SupportsFloat,
        timestamp_s: SupportsFloat,
    ) -> str:
        """Inject a sample with the Renode virtual timestamp at which it arrives."""
        values = tuple(float(value) for value in (x_g, y_g, z_g, timestamp_s))
        if not all(math.isfinite(value) for value in values) or values[3] < 0:
            raise ValueError("sample values and timestamp must be finite; timestamp must be nonnegative")
        args = " ".join(f"{value:.12g}" for value in values)
        return self.command(f"sysbus.i2c1.imu InjectAccelerationSampleFromGazeboAtTime {args}")

    def current_time(self) -> str:
        """Return Renode's human-readable virtual and host time report."""
        return self.command("currentTime")

    def observed_virtual_time_s(self) -> float:
        """Read and parse the virtual clock reported by the running Renode."""
        response = self.current_time()
        match = re.search(
            r"Current virtual time:\s*(\d+):(\d{2}):(\d{2})(?:\.(\d+))?",
            response,
        )
        if match is None:
            raise RenodeMonitorError(f"cannot parse Renode virtual clock: {response!r}")
        hours, minutes, seconds = (int(match.group(i)) for i in (1, 2, 3))
        fraction = match.group(4) or ""
        fractional_seconds = float("0." + fraction) if fraction else 0.0
        return hours * 3600 + minutes * 60 + seconds + fractional_seconds

    def _read_prompt(self) -> bytes:
        output = self._pending_output
        self._pending_output = bytearray()
        deadline = time.monotonic() + self._timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for the Renode monitor prompt")
            self._socket.settimeout(remaining)
            chunk = self._socket.recv(4096)
            if not chunk:
                raise ConnectionError("Renode monitor closed the connection")
            for byte in chunk:
                self._consume_byte(byte, output)
            plain = self._ANSI.sub(b"", bytes(output))
            if self._PROMPT.search(plain):
                return bytes(output)

    def _settle_telnet_negotiation(self) -> None:
        """Answer the server's initial option negotiation before commands."""
        deadline = time.monotonic() + min(self._timeout_s, 1.0)
        self._socket.settimeout(0.2)
        while time.monotonic() < deadline:
            try:
                chunk = self._socket.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                raise RenodeMonitorError("Renode monitor closed during Telnet negotiation")
            for byte in chunk:
                self._consume_byte(byte, self._pending_output)

    def _drain_initial_prompt(self) -> None:
        """Consume Renode's delayed welcome banner and initial prompt fully."""
        output = self._pending_output
        self._pending_output = bytearray()
        deadline = time.monotonic() + self._timeout_s
        while not self._PROMPT.search(self._ANSI.sub(b"", bytes(output))):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RenodeMonitorError("timed out waiting for the initial Renode monitor prompt")
            self._socket.settimeout(min(remaining, 0.2))
            try:
                chunk = self._socket.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise RenodeMonitorError("Renode monitor closed before its initial prompt")
            for byte in chunk:
                self._consume_byte(byte, output)

        # A short quiet drain catches the second startup prompt when the
        # --execute include command finishes just after the first prompt.
        quiet_deadline = time.monotonic() + 0.1
        while time.monotonic() < quiet_deadline:
            self._socket.settimeout(max(0.01, quiet_deadline - time.monotonic()))
            try:
                chunk = self._socket.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                raise RenodeMonitorError("Renode monitor closed during startup prompt drain")
            for byte in chunk:
                self._consume_byte(byte, output)

    def _consume_byte(self, byte: int, output: bytearray) -> None:
        if self._subnegotiation:
            if self._iac_state == 1 and byte == self._SE:
                self._subnegotiation = False
                self._iac_state = 0
            else:
                self._iac_state = 1 if byte == self._IAC else 0
            return
        if self._iac_state == 2:
            command = self._iac_command
            if command in (self._DO, self._DONT):
                self._socket.sendall(bytes((self._IAC, self._WONT, byte)))
            elif command in (self._WILL, self._WONT):
                self._socket.sendall(bytes((self._IAC, self._DONT, byte)))
            self._iac_state = 0
            return
        if self._iac_state == 1:
            self._iac_state = 0
            if byte in (self._DO, self._DONT, self._WILL, self._WONT):
                self._iac_command = byte
                self._iac_state = 2
            elif byte == self._SB:
                self._subnegotiation = True
            elif byte == self._IAC:
                output.append(byte)
            return
        if byte == self._IAC:
            self._iac_state = 1
        else:
            output.append(byte)


__all__ = ["RenodeMonitorClient", "RenodeMonitorError"]
