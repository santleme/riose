# RIOSE Renode virtual hardware

Status: **SIMULATED / IMPLEMENTATION SCAFFOLD**. These files add a headless
Renode platform and custom bus responders for the protocol subset used by the
current firmware. No analog, RF propagation, energy, or silicon behavior is
represented here.

## Platform support finding

The inherited Renode L071 platform normally runs an `ApplySVD` command against
an upstream STM32L0 SVD URL during initialization. That SVD supplies debugger
register names and peripheral metadata; it does not drive emulated behavior.
This platform overrides the inherited `sysbus` init hook and keeps its address
range tags, so headless smoke tests do not download external SVD data at
runtime. The CPU and peripheral models still come from the installed Renode
release.

Renode upstream currently provides a `platforms/cpus/stm32l071.repl` Cortex-M0+
SoC model, but no STM32L031 platform was found. This scaffold reuses that L071
peripheral set to exercise an L0-family firmware path. The L071 platform has
192 KiB flash and 20 KiB SRAM in its memory map, while the candidate
STM32L031K6 has 32 KiB flash and 8 KiB SRAM. It must not be used to claim the
L031 memory limits, exact clock tree, or exact peripheral revision. Its GPIO,
SPI, I2C, timers, EXTI, watchdog, RTC and power-controller models are still
software abstractions. STOP/deep-sleep current and real wake latency are not
modeled. The existing Zephyr `native_sim` path remains the fast software-only
test path.

The SX1262 responder implements the SPI command subset used by the current
firmware: status, standby/sleep, LoRa configuration, buffer bases, FIFO,
IRQ-mask routing, TX completion, and bounded RX timeout. It rejects unsupported
commands and fixed-length command frames with a fault counter/status. `HoldBusy`,
`SuppressIRQ`, and `DropSPI` are fault hooks. While `HoldBusy` is active, TX/RX
starts fail with `CMD_FAILED` and schedule no operation. TX completion and RX timeout use
Renode's virtual clock (64 kHz, matching the SX126x 15.625-us timeout tick), so
firmware polling does not advance radio time by an arbitrary amount per byte.
TX completion is a deterministic logical event; no RF waveform or peer is
modeled, so RX ends in timeout and never synthesizes `RX_DONE`. When a malformed
streamed command is detected at chip-select release, its status is available to
a following `GET_STATUS`; the byte already shifted during that same SPI frame
cannot be changed retroactively. The C callback can report the error in the
same returned transfer buffer because it receives the complete frame at once.

The SX1262 model can export `riose.renode.sx1262_tx_trace/v1` records for
completed or timed-out TX operations. The trace preserves FIFO payload bytes
and Renode virtual-clock start/done times, along with the captured PLL
frequency word and signed `SetTxParams` power setting; its clock uses
nanosecond ticks.
`riose mvp3 replay EXPERIMENT` writes `sx1262_tx_trace.json` in the experiment
directory; `RIOSE_RENODE_TX_TRACE` can override that destination. These are
model-observed logical TX operations, not RF reception,
RSSI, or a Gazebo/Renode lockstep timestamp mapping.

Renode upstream provides the `Sensors.LIS2DW12` data/RESD model (available
since Renode 1.13.3), so RIOSE does not duplicate its register/sample path.
Synthetic or Gazebo-derived samples are fed through
`FeedAccelerationSamplesFromRESD`. RIOSE's adapter adds `INT1` routing because
upstream has no INT1 GPIO property. Dataset manifests identify the input as
`SIMULATED`; these traces are not measured bovine behavior.

`dataset_to_resd.py` validates the dataset contract and emits a RESD file plus a
`.resd.json` identity sidecar with profile ID, seed, CSV SHA-256, and RESD
SHA-256. Each conversion removes its previous target first, so a failed
conversion cannot leave an old RESD appearing to be current.

## SX1262 comparison with the C model

| Behavior | C model (MVP1 host model) | Renode model | Match or difference |
| --- | --- | --- | --- |
| SPI commands and fixed lengths | Whole-frame transfer callback; validates exact fixed lengths | Byte-stream peripheral; validates at chip-select release | Same supported opcodes/lengths; error status is observable on the next `GET_STATUS` because shifted bytes cannot be changed retroactively |
| FIFO | 256-byte array; 8-bit offset wraps | 256-byte array; 8-bit offset wraps | Match; read/write and `0xFF` wrap are tested |
| State/reset | Standby/sleep/TX/RX state; reset clears configuration, FIFO, IRQ and pending operations | Standby/sleep/TX/RX state; active-low GPIO reset clears configuration, FIFO, IRQ and timer | Match for logical state; Renode supplies the reset pin and virtual timer |
| BUSY | No BUSY pin or timing | BUSY is high during TX/RX and can be held high by a fault hook | Intentional Renode-only pin behavior |
| IRQ | Global and DIO1 masks gate logical IRQ result | Global and DIO1 masks drive DIO1 GPIO; suppression hook can hide the pin | Match for DIO1; fault hook is Renode-only |
| TX | Completion uses configured latency; equal/earlier radio timeout wins | One-shot `LimitTimer` uses Renode virtual time; equal/earlier timeout wins | Same deadline decision; C advances in integer milliseconds while Renode uses 15.625-us ticks |
| RX | Logical receive window always ends with timeout; continuous RX capped at one second | Logical receive window always ends with timeout; continuous RX capped at one virtual second | Match; neither invents an over-the-air packet or `RX_DONE` |
| Sleep/standby | Cancels active logical TX/RX and clears pending timeout | Cancels and resets the active Renode timer | Match; time granularity differs |
| SPI transport failure | Synchronous C callback has no timed controller transfer | `DropSPI` returns `0xFF`; optional `SPITransferFault` stalls controller register accesses until a virtual deadline | Dropped byte and timed controller fault are tested separately; the watchdog is simulation instrumentation |

`hardware/models/sx1262` remains the firmware-host reference for command bytes,
configuration state, FIFO contents, IRQ status, and logical TX/RX outcomes.
The C model rounds radio timeouts up to an integer millisecond; Renode uses
15.625-us virtual ticks. The C continuous-RX case is bounded to one second,
matching Renode. Both complete TX against the captured latency deadline and
apply the same timeout tie policy. Renode is the source of evidence for actual
virtual-time scheduling and BUSY pin behavior; the C smoke is not used to claim
those Renode properties.

Neither model generates received RF payloads, RSSI, CRC outcomes, RF power,
analog behavior, or propagation. The models perform no RF-power or analog
inference. The Renode Robot suite drives the radio peripheral byte by byte and
tests config, FIFO including address wrap, virtual TX_DONE and timeout, IRQ
read/clear/routing, malformed frame sizes, reset, BUSY, and fault hooks. It does
not verify STM32 SPI chip-select waveforms or a complete firmware recovery
cycle. This was an earlier run before a Zephyr ELF was supplied; see the
firmware wake-up scenario below for the current end-to-end result.

```sh
python hardware/models/lis2dw12/generate_datasets.py --seed 20261002
uv pip install --python "$(command -v python)" -r hardware/renode/requirements.txt
python hardware/renode/scripts/dataset_to_resd.py STATIC --output /tmp/static.resd
python hardware/renode/scripts/dataset_to_resd.py WALK --output /tmp/walk.resd
RIOSE_LIS2DW12_STATIC_RESD=/tmp/static.resd \
RIOSE_LIS2DW12_WALK_RESD=/tmp/walk.resd \
renode-test hardware/renode/tests/platform-smoke.robot
```

The converter tests validate the RESD payloads and metadata, and the Robot tests
load STATIC and WALK streams before emulation starts. With Renode 1.17.0 and a
fresh Zephyr 4.2.1 Renode-profile ELF, the tests compare all three raw axis words
returned to firmware against the first sample in each RESD payload. The observed
Z words are `0x4028` (STATIC, 1.002 g) and `0x4A48` (WALK, 1.160 g); a zero or
fallback sample now fails the comparison. These are simulated register values.

The former zero samples came from RESD callback discovery: Renode searches the
concrete peripheral type, so the upstream LIS2DW12's private callback methods
are not inherited by `LIS2DW12WakeModel`. The adapter declares its own attributed
callbacks and forwards them to the upstream handlers. This retains native FIFO,
before-stream defaults, and end-of-stream behavior. The adapter exposes a
separate INT1 output that continuously ORs the native INT1 level with the
pending simulated wake source. Native data-ready callbacks therefore cannot
pulse PA8 low while wake is latched. A GPIO transition probe verifies exactly
one rising edge and no falling edges across before-, during- and after-stream
ticks, followed by one falling edge when the source is read. A separate case
checks native data-ready IRQ remains high after the wake source is cleared,
and that reset clears the exposed output. A separate case checks
the default before playback, the first and final samples, and the return to the
default on the next output read after the stream finishes. The bridge depends
on two private upstream method names and fails explicitly if a future Renode
release changes their signatures; rerun this suite when upgrading Renode.

Playback setup uses public I2C writes to CTRL1, avoiding private `SampleRate`
assignment and its integer rounding. The firmware currently writes `CTRL1=0x14`,
which Renode models as 2 Hz, high-performance 14-bit output (244 ug/LSB). Its
silicon ODR is 12.5 Hz. The firmware decodes its 14-bit output at 244 ug/LSB;
the assertions above prove transport of the dataset values to firmware-visible
register bytes, not that classifier thresholds are calibrated to an animal.

## Headless use

### Controller transfer timeout instrumentation

`spi-transfer-fault.repl` is an optional platform for polling SPI fault tests.
The wrapper forwards normal register reads and writes to Renode's native
`STM32SPI`. With `StallTransfers` enabled, an enabled-controller write to `DR`
starts a one-shot virtual watchdog (`TimeoutUs`, default 10,000 us). The write
does not reach the radio. While pending, `SR.BSY` is set and `SR.RXNE`/`SR.TXE`
are clear, and no received byte is available. At the deadline the transfer is
aborted, `TimeoutCount` increments once, `BSY` clears and `TXE` returns. Disabling
`SPE` or resetting the wrapper cancels the deadline. Clearing `StallTransfers`
allows the next transfer to use the native controller normally.

The watchdog and its counter are **SIMULATED test instrumentation**, not STM32
hardware registers or a physical timeout measurement. The overlay keeps the
native controller at debugger alias `0x40013400` and intercepts firmware's SPI1
address `0x40013000`; the standard platform is unchanged. This optional fault
path covers polling register accesses only, not DMA or interrupt-driven SPI.
The test halts the CPU and checks controller state at 9 ms and exactly 10 ms,
single expiry, register polling, cancellation, and a recovered `GetStatus`
response. A separate case proves that `DropSPI` returns an immediate `0xFF`
with `RXNE` set and no watchdog expiry.

```sh
renode-test hardware/renode/tests/spi-transfer-timeout.robot
```

This demonstrates controller-level timed fault injection and protocol recovery
by the test harness. Firmware timeout/error handling and a complete Zephyr
recovery cycle under this fault remain unverified; this evidence alone should
not close issue #5.

Renode and `renode-test` are optional local tools; this workspace does not
vendor them. Install a Renode release and ensure `renode` and `renode-test`
are on `PATH`. The RIOSE platform compiles the custom C# model in the STM32
CPU's `preinit` block so the types are available before Renode resolves the
radio and IMU entries. Keep the suite's default setup and teardown from
`renode-test`; they connect the Robot remote library. Build the existing
Zephyr firmware for the STM32L0 target and set `RIOSE_ZEPHYR_ELF` to its ELF.

```sh
python3 hardware/renode/scripts/check_tools.py
renode --console --disable-xwt -e 'include @hardware/renode/riose_stm32l0.resc'
renode-test hardware/renode/tests/platform-smoke.robot
```

The repository target `make hardware-renode-test` runs the tool probe and
headless smoke suite when both Renode commands are installed; otherwise it
prints an explicit `SKIPPED` line so this optional tool does not block the
core firmware gate. The CI invokes the same target. The Robot suite proves
platform/peripheral registration and optional dataset consumption; its
firmware-load case remains skipped unless `RIOSE_ZEPHYR_ELF` points to a
compatible STM32L0 ELF.

For no-GUI execution in CI, use the console/headless switches above. The
Robot smoke test can load the platform without firmware; its second case is
skipped unless an ELF path is provided. A valid firmware ELF must be compiled
for a compatible STM32L0 memory map and peripheral base addresses. The current
board profile and Renode surrogate mismatch must be reviewed before wiring this
into required CI.

## Firmware wake-up scenario status

The Renode platform uses `Riose.LIS2DW12WakeModel`, a thin adapter over
Renode's upstream `Sensors.LIS2DW12`. Since upstream does not implement the
LIS2DW12 wake threshold comparator or its high-pass filter, the adapter adds an
explicit first-order behavioral approximation for the firmware's configured
WU path. RESD callbacks provide simulated x/y/z acceleration; threshold and
duration settings, filtered motion, source bits, and INT1 are handled by this
approximation. It is labeled `SIMULATED_APPROXIMATE_HIGH_PASS_MODEL`; it does
not validate the silicon transfer function, comparator latency, or electrical
behavior.

The same adapter exposes `InjectAccelerationSampleFromGazebo(xG, yG, zG)` for
one sample at a time. It feeds Renode's native LIS2DW12 register/FIFO path and
the approximate WU comparator; `GazeboSampleInjectionCount` makes accepted
samples observable. The Robot case injects individual vectors, reads
`WAKE_UP_SRC`, and verifies that reading the source releases INT1. A Python
`RenodeMonitorClient` can invoke this API over Renode's Telnet monitor and
advance virtual time with explicit `emulation RunFor` steps. This is the
control surface needed for a future streamed Gazebo bridge; the current
experiment runner still records Gazebo first and then replays its RESD data.
It does not claim live lockstep or a shared real-time clock.

The firmware writes `CTRL1=0x14`, which selects 12.5 Hz high-performance mode
and 14-bit output on the LIS2DW12. Renode v1.17.0 maps ODR code 1 to 2 Hz
without accounting for the MODE bits; the adapter corrects its modeled rate
for this mode. The production-path Robot case keeps the firmware's `CTRL1`
setting and verifies the mapped 13 Hz callback schedule. Gazebo timestamps do
not drive the firmware clock during replay.

The firmware's configured wake path is `CTRL4.INT1_WU` plus
`CTRL7.INTERRUPTS_ENABLE`; it leaves `HP_REF_MODE` and `USR_OFF_ON_WU` clear.
The `Firmware Wakes From Gazebo Motion Comparator And Transmits` Robot case
checks that Zephyr is in `SLEEP`, feeds the walking RESD at the modeled ODR,
and verifies the approximate comparator generates `WAKE_UP_SRC=0x0C`, resumes
the firmware, and yields sample reads, `ALERT`, and a completed CRC-valid TX.
After the configured 100 ms RX window, the test also verifies the firmware
returns to `SLEEP`. The case preserves `CTRL1=0x14`. Its result establishes
the digital firmware state sequence under the documented comparator
approximation, not the physical sensor behavior.

The [ST LIS2DW12 datasheet](https://www.st.com/resource/en/datasheet/lis2dw12.pdf)
defines the CTRL1 mode/ODR encoding and 14-bit output scale; [ST application
note AN5038](https://www.st.com/resource/en/application_note/dm00401877-lis2dw12-alwayson-3d-accelerometer-stmicroelectronics.pdf)
documents the wake-up high-pass path.

The optional Robot firmware case runs only when `RIOSE_ZEPHYR_ELF` is set. Use
`boards/nucleo_l031k6_renode.conf` in addition to the physical board profile:
it disables Zephyr PM so the STM32L071 surrogate can use its modeled periodic
timer instead of the physical board's RTC/STOP idle path. This remains an
STM32L071 proxy, not an exact STM32L031K6 model.

The Robot suite covers adapter-level wake plumbing, Gazebo RESD sample replay,
the approximate configured WU/classifier/TX path, and boot TX evidence when
`RIOSE_ZEPHYR_ELF` is set.
The SX1262 surrogate exposes TX count and the last logical TX payload so Robot
tests can inspect packet bytes; it does not represent a transmitted RF
waveform or receiver observation. Boot TX evidence is not movement-triggered
wake evidence.

The firmware integration initially failed because PA11, the board's active-low
SPI chip select, was not connected to the SX1262 model's chip-select GPIO. The
model consequently kept later SPI command bytes in the first `SetStandby`
frame. Mapping PA11 to model GPIO1 and finishing the transaction on CS
deassertion fixed the end-to-end path. Stack-size diagnostics at 2 KiB and
3 KiB did not fix the pre-CS-routing failure. `native_sim` remains a separate
software/model path and does not demonstrate electrical behavior.
