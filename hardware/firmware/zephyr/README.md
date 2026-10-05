# Zephyr firmware adapter

The app adapts the portable C firmware core to Zephyr. On `native_sim`, the HAL
passes firmware-generated SX1262 SPI command frames to the real C peripheral
model and reads the LIS2DW12 register model; Zephyr supplies kernel timing and
sleep. On a physical board target, `src/main.c` binds the HAL to SPI, I2C, and
GPIO devices. Its optional HAL event wait uses Zephyr GPIO callbacks and a
semaphore: INT1 wakes the MCU on LIS2DW12 activity and the timed wait ends at
the next scheduled beacon. Host and native_sim retain their clock/sleep
callback path. Neither path forks or replaces `../src/`.

Build/run was verified against Zephyr v4.2.1 with the host toolchain on
`native_sim/native/64`. This native path executes the C FSM and the SX1262 and
LIS2DW12 C models. It is a software simulation, not analog bus emulation or a
physical-board build. The production-board `src/main.c` electrical wake,
current, and interrupt timing remain unverified on silicon.

## Structured firmware trace

The portable FSM exposes an optional non-blocking `tag_hal_t.trace_event`
callback. `CONFIG_TAG_STRUCTURED_TRACE` controls whether the Zephyr adapters
connect that callback; disabling it does not change FSM policy. When structured
tracing and Zephyr logging are enabled, adapters emit records to the console as comma-separated
`SIMULATED_TRACE,v1` lines with fields:

`sequence,timestamp_us,state_id,event,event_id,source_id,result,value0,value1,value2,packet_hex`

The timestamp uses the 64-bit Zephyr uptime clock and is quantized to 1 ms;
the `_us` suffix provides a consistent unit, not microsecond timing precision.
Event and state identifiers are the stable C
enum values in `tag_firmware.h`. `PACKET_CREATED.packet_hex` contains the exact
transmitted bytes in lowercase hexadecimal; `value0` gives their byte length.
Other event values carry stage-specific context: IMU axes (signed values
represented in 32-bit two's-complement), sequence, behavior, TX configuration,
IRQ flags, sleep duration, or failure count. On physical targets with Zephyr
HWINFO reset-cause support, startup emits `WATCHDOG` or `REBOOT` only when the
hardware reports a prior watchdog or other non-power-on reset; `value0` contains
the raw reset-cause flags and `value1` the classified cause. The native_sim
backend does not emulate reset causes and does not fabricate these events.
`TRACE_END` marks the end of a simulator capture and is not an operational FSM
event.
All trace lines are labeled `SIMULATED_TRACE`; they are software/model
events, not electrical measurements. Existing GPIO state-trace output remains
available and unchanged. A null callback keeps MVP 1 integrations silent. The
`trace_export.py` helper normalizes console captures to the shared
`riose.firmware.trace/v1` JSONL schema and checks required fields,
sequence/timestamp integrity, state transitions, event source/state context,
telemetry bytes and CRC, and complete packet/TX/RX intervals. The `native_sim`
adapter uses the same versioned record layout as the board adapter. `make
hardware-native-sim` captures a normal run and an injected IMU-failure recovery
run, exporting them to `/tmp/riose-native-sim-trace.jsonl` and
`/tmp/riose-native-sim-failure-trace.jsonl`; it repeats the normal run to compare
semantic event determinism and builds/runs a trace-disabled configuration to
check that FSM policy still passes without trace records. The target fails if
producer output cannot be parsed or the disabled run emits trace records. The
NUCLEO profile sets
`CONFIG_LOG=n`, so its console trace records remain disabled unless logging is
enabled for that board. GPIO state markers remain independent.

`boards/native_sim_bus_emul.overlay` is an optional bus-emulator wiring template.
The validated native_sim path uses the same standalone C peripheral models as
the host integration harness, so it does not claim electrical bus emulation.
The NUCLEO-L031K6 profile below supplies one concrete physical overlay and
Zephyr SPI/I2C/GPIO drivers; other boards still need their own pin map.

## Build when Zephyr is installed

From a Zephyr workspace with `ZEPHYR_BASE` exported and dependencies installed:

```sh
west build -b native_sim/native/64 -d build/tag-native \
  /path/to/riose/hardware/firmware/zephyr
west build -d build/tag-native -t run
```
The native_sim C models provide deterministic responses. The run exits after
one firmware TX/RX/sleep cycle with a `SIMULATED native_sim cycle PASS` log.
A hardware-target run requires an actual LIS2DW12 responder/device returning
`WHO_AM_I=0x44`.

For a physical board, supply a board-specific DTS overlay defining
`tag-radio`, `tag-imu`, `tag-radio-reset`, `tag-radio-busy`, `tag-radio-dio1`,
and `tag-imu-int`. `tag-radio-busy` must be an active-high GPIO connected to
the SX1262 BUSY output. The physical SPI adapter waits for BUSY to deassert
before every command, polling at 1 ms intervals for at most 100 ms; a stuck
line returns `-ETIMEDOUT` without clocking SPI. No physical GPIO number or
board pin mapping is prescribed here: choose pins for the selected board and
module in its overlay. Also configure GPIO polarity, SPI timing, I2C address,
oscillator/antenna, radio regional parameters, and interrupt wiring for that
board. The 915 MHz default is a prototype setting, not a regulatory approval
or validated RF design.

## NUCLEO-L031K6 wiring profile

`boards/nucleo_l031k6.overlay` provides a concrete MCU-side pin assignment for
the ST NUCLEO-L031K6 and a 3.3 V SX1262 plus LIS2DW12 module. It uses the
Arduino Nano connector assignments in Zephyr's checked-in
`nucleo_l031k6.dts` / `arduino_nano_connector.dtsi` and ST [UM1956 Table 14 and
section 7.10](https://www.st.com/resource/en/user_manual/dm00231744.pdf).
It does not assert any breakout-board header order. Wire by the signal names
printed on the actual modules and confirm each board's logic voltage first.

| Function | MCU pin | NUCLEO header | Connect to module signal |
|---|---|---|---|
| SX1262 SCK | PB3 | D13 | SCK |
| SX1262 MISO | PB4 | D12 | MISO |
| SX1262 MOSI | PB5 | D11 | MOSI |
| SX1262 NSS / CS | PA11 | D10 | NSS / CS |
| SX1262 NRESET | PA12 | D2 | NRESET |
| SX1262 BUSY | PB0 | D3 | BUSY |
| SX1262 DIO1 | PB1 | D6 | DIO1 |
| SX1262 antenna switch | PC15 | D8 | ANT SW held high; SX1262 DIO2 selects TX/RX |
| LIS2DW12 SCL | PB6 | A5 (CN4-7) | SCL |
| LIS2DW12 SDA | PB7 | A4 (CN4-8) | SDA |
| LIS2DW12 INT1 | PA8 | D9 | INT1 |
| State trace bit 0 | PA0 | A0 | logic analyzer channel 0 |
| State trace bit 1 | PA1 | A1 | logic analyzer channel 1 |
| State trace bit 2 | PA3 | A2 | logic analyzer channel 2 |
| Shared supply | — | +3V3 (CN4-14) | module VCC, only if its datasheet allows 3.3 V |
| Shared return | — | GND (CN3-4 or CN4-2) | module GND |

Board solder bridge requirements from UM1956: set **SB16 and SB18 ON** to
route PB6/PB7 to A5/A4; this consumes D5/D4. Open **SB15** to disconnect the
on-board user LED from PB3 before using D13 as SPI SCK. The overlay keeps PA5
and PA6 as input GPIOs as required by the SB16/SB18 configuration and disables
the board's PB3 LED device. Do not connect anything to D4, D5, A4, or A5 except
the stated I2C signals in this wiring profile. The unused D0/D1 UART pins are
left alone. Verify the physical board's solder-bridge defaults before wiring.
The A0/PA0 trace output assumes the board's standard LSE clock arrangement;
check the ST-LINK clock solder bridges before using PA0 in a modified board
configuration.

The D8 antenna-switch mapping is for the Semtech SX1262MB2xAS shield: TX is
selected before `SetTx`, RX is restored after `TX_DONE`, and failures return
the switch to RX. Other modules may use DIO2 or different switch logic and
need a matching overlay/HAL implementation. The default I2C address in the
overlay is `0x18` (LIS2DW12 SA0 low); set it to
`0x19` if the specific module straps SA0 high. The radio SPI rate is initially
limited to 1 MHz. BUSY must connect directly to the radio's active-high BUSY
signal. Reset is active-low. Pull-ups, interrupt output configuration, module
regulator/current behavior, antenna and RF matching remain module-specific and
must follow their datasheets. The profile config in
`boards/nucleo_l031k6.conf` enables Zephyr PM and builds the physical test at
`+14 dBm`; it is board-specific, so `native_sim` keeps its independent
settings.

On the NUCLEO-L031K6 target, `boards/nucleo_l031k6.overlay` selects the STM32
RTC as the SysTick low-power companion timer. STOP halts SysTick; the RTC is
clocked from the board's internal LSI because this Nucleo target has no LSE
crystal configured. This lets Zephyr account for elapsed STOP time and schedule
the next kernel timeout, but LSI accuracy and real wake timing still need
bench measurement. `native_sim` does not use this physical timer path.

The three state-trace pins output the binary `tag_state_t` value, with A0 as
the least-significant bit. Capture them on a high-impedance logic analyzer to
measure transition times and dwell intervals without UART logging. Codes are
`000 BOOT`, `001 SELF_TEST`, `010 SLEEP`, `011 IMU_MONITORING`, `100 RF_TX`,
`101 RF_RX`, `110 ALERT`, and `111 ERROR_RECOVERY`. Connect analyzer ground to
board ground; do not drive these outputs. Probe capacitance and any attached
load become part of the measured board configuration.

The physical profile was compiled successfully with Zephyr v4.2.1 and Zephyr
SDK 0.16.8 using:

```sh
west build -b nucleo_l031k6 -d build/tag-nucleo-l031k6 \
  /home/lucas_coimbra/projects/riose/hardware/firmware/zephyr
```

The latest build produced `zephyr.bin` at 29,104 bytes (88.82% of the MCU's 32 KiB
flash) and uses 2,976 bytes of its 8 KiB RAM. The RTC companion timer accounts
for additional code and leaves about 3 KiB of flash headroom. This verifies compilation and
linking for the target only. The image was not flashed; radio behavior, IRQ
wake, rail stability and current still require the actual board, modules,
power path, and measurement setup.

The LIS2DW12 setup follows ST [AN5038 section 5.4](https://www.st.com/resource/en/application_note/dm00401877-lis2dw12-alwayson-3d-accelerometer-stmicroelectronics.pdf): `CTRL1=0x14` (12.5-Hz high-performance, 14-bit output), `WAKE_UP_DUR=0x00`, `WAKE_UP_THS=0x02` (62.5 mg at ±2 g), `CTRL4.INT1_WU=0x20`, and `CTRL7.INTERRUPTS_ENABLE=0x20`. Startup reads `WAKE_UP_SRC` and `ALL_INT_SRC` to clear stale sources; after INT1 wakes the MCU, firmware reads `WAKE_UP_SRC` to report and clear WU_IA. These register-level changes and the simulated IRQ tests do not measure physical INT1 polarity, wake latency, or sleep current.

Zephyr native_sim has documented SPI, I2C and GPIO emulation support; the bus
emulators still require peripheral-specific responder implementations:
[native_sim board documentation](https://docs.zephyrproject.org/latest/boards/native/native_sim/doc/index.html),
[bus-connected device emulator documentation](https://docs.zephyrproject.org/latest/hardware/emulator/bus_emulators.html).
# Zephyr firmware trace export

The firmware emits compact `SIMULATED_TRACE,v1` records to the Zephyr log
backend. Capture the console output, then normalize it to the shared versioned
JSONL schema with:

```sh
python3 hardware/firmware/zephyr/trace_export.py capture.log \
  --output results/mvp2/firmware/zephyr-trace.jsonl
```

The normalizer rejects missing records, malformed rows, sequence gaps, and
non-monotonic virtual timestamps. A normalized trace is protocol/test evidence
only; the timestamp is virtual time and does not measure current or RF behavior.
