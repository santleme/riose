# Cattle RF MVP

Local-first prototype for evaluating whether shared sub-GHz anchors can track
cattle without GPS on each animal. The core is a reproducible CPU simulation;
all outputs are labeled by evidence status. Simulated results are not field
validation.

The product boundaries, proposed package layout, migration sequence, and
current memory policy are documented in [`docs/architecture.md`](docs/architecture.md).

## Status

The CPU-first MVP is implemented and runs locally. It includes a seeded farm
and RF simulator, virtual tag energy/FSM model, six estimators, SQLite animal
identity/event history, editable local dashboard, dataset export, and a
multi-scenario benchmark. All measured errors in this repository are software
simulation results, not farm trials.

## Start

```sh
./setup.sh
./run_demo.sh
```

The dashboard is served locally at `http://127.0.0.1:8000`. It starts with 100
animals and eight anchors; controls change herd size, anchor count/placement,
estimator, and packet loss, then rerun the simulation. Run tests with
`uv run pytest`; run the benchmark with `make benchmark`. `cattle-rf dataset`
creates train/validation/holdout files with ground truth separated from RF
features.

## Local API

The API is served from the same local process. Main routes include
`/api/animals`, `/api/animals/{id}`, `/api/anchors`, `/api/telemetry`,
`/api/positions`, `/api/positions/history`, `/api/events`,
`/api/simulation/run`, `/api/experiments`, and `/api/metrics`. Normal position
responses exclude truth fields; `debug=true` is required to request them.
Animal event hashes are locally verifiable; signatures and public blockchain
publication are future interfaces.

## Results

The checked-in `results/` directory contains the generated full benchmark
tables, error CDF, cost placeholders, report, and a separate holdout dataset.
The detailed run summary and interpretation are in
[`docs/results-2026-10-01.md`](docs/results-2026-10-01.md). A price is left blank
and marked `PRICE_RESEARCH_REQUIRED` until a dated supplier source is entered.

## Evidence labels

- `SIMULATED`: produced by the software model, not measured on a farm.
- `ASSUMED`: configured parameter without verified component measurement.
- `EXPERIMENTAL`: exploratory model (for example simulated Wi-Fi CSI).
- `VALIDATED`: reserved for evidence verified against physical measurements.
- `FUTURE`: interface or capability not implemented in this MVP.

Ground truth is maintained separately from receiver-visible observations and
is available only to evaluation and the explicitly enabled dashboard debug
view. Hardware cost fields remain `PRICE_RESEARCH_REQUIRED` until supported by
dated sources.

## Virtual hardware tag

The new C hardware MVP is documented in [`hardware/reports/mvp-hardware-report.md`](hardware/reports/mvp-hardware-report.md)
and does not replace the RF localization simulator above. It contains a
portable firmware FSM, SX1262 SPI command model, LIS2DW12 register/IRQ model,
24-byte telemetry with CRC-16, an integration harness, and a configurable
energy profile. Run `make hardware-test` for the CTest suite or
`make hardware-demo` for the accelerated tag scenarios.

The normal firmware beacon interval is 15 minutes (96 transmissions/day);
ACTIVE and ALERT cadence increases are bounded to two-minute bursts. The energy
budget is below the 0.5 mAh/day target under its configured assumptions. A
Tadiran TLL-5902 (1/2 AA, 1.1 Ah) and TI TPS62840 buck are engineering
candidates, not finalized physical parts. The firmware also builds and runs
with Zephyr `native_sim/native/64` against the same C peripheral models
(`make hardware-native-sim` from a configured Zephyr workspace). ngspice 42
ran 150 voltage/ESR/capacitor sensitivity scenarios; this averaged circuit
model is not a vendor regulator model or physical brownout validation. The
nominal-cell rail minimum was 3.2909 V under the +14 dBm TX stress current; low
voltage points crossed an assumed 2.7 V design threshold. Autonomy is only an
unmeasured quantity; no runtime is calculated and no battery-life claim is made. The physical test protocol
and capture analyzer are in [`hardware/physical/`](hardware/physical/README.md).
Full details,
test counts, energy scenarios, and blockers are in
[`hardware/reports/mvp-hardware-report.md`](hardware/reports/mvp-hardware-report.md).

Wokwi and KiCad remain unverified: their executables are unavailable here, so
the virtual peripherals use local C models and the circuit topology is
documented rather than claiming a compiled KiCad schematic or PCB. No radio
propagation, 134.2 kHz RFID reader, physical battery/brownout, or field animal
behavior is claimed as validated.

## MVP 2 digital twin (ear tag only)

The MVP 2 work adds a provenance-controlled hardware specification and a
headless analysis entry point. It preserves this MVP 1 simulator and its C
firmware models. The commercial Allflex dimensions are only an `ASSUMED` scale
reference, not approved RIOSE geometry. Run:

```sh
uv run python -m riose.products.ear_tag.digital_twin validate-spec
uv run python -m riose.products.ear_tag.digital_twin preflight
uv run python -m riose.products.ear_tag.digital_twin run
```

The previous `python -m riose.digital_twin` command remains available as a
compatibility entry point.

The run writes machine-readable outputs under `results/mvp2/` and
`docs/mvp2-digital-twin-report.md`. Missing Renode, ngspice, CadQuery, openEMS,
or a configured openEMS solver adapter is reported as unavailable; missing
solvers never produce invented electrical or RF metrics. GPU/Sionna is only an
`OPTIONAL_GPU_EXPERIMENT`. The current assumed mechanical candidate passes its
bounding-box fit checks with separate PCB and battery bays, 0.5 mm clearance,
and an 18.5 mm enclosure depth. This is only a simulated layout estimate; it
does not validate terminals, tolerances, retention, or physical assembly. No parameter may be marked
`MEASURED`; dimensions, antenna and fit still require review before a READY
gate can be reached. See [`hardware/spec.yaml`](hardware/spec.yaml) for the
candidate architecture and provenance.

## MVP 3 digital animal / ear-tag twin

MVP 3 adds a procedural, physically simulated bovine scene in Gazebo Harmonic
around the existing MVP 2 ear-tag design. The model is an `ASSUMED` mechanical
placeholder; Gazebo output is `SIMULATED`, and no physical animal, attachment,
RF, or battery validation is claimed. Read the current gate and experiment
results in [`docs/mvp3-animal-digital-twin-report.md`](docs/mvp3-animal-digital-twin-report.md).

```sh
./scripts/setup_mvp3.sh
./scripts/run_mvp3.sh walking --visual
./scripts/run_mvp3_headless.sh head-shake
uv run riose mvp3 run mass-sweep --fast-headless
uv run riose mvp3 run long-simulation --headless --live-lockstep --duration-s 60
RIOSE_ZEPHYR_ELF=/tmp/riose-mvp3-renode-build/zephyr/zephyr.elf \
  uv run riose mvp3 run walking --visual --live-lockstep
uv run riose mvp3 replay results/mvp3/EXPERIMENT_DIR
uv run riose mvp3 report results/mvp3/EXPERIMENT_DIR
uv run riose mvp3 visualize results/mvp3/EXPERIMENT_DIR
./scripts/test_mvp3.sh --quick
```

`standing`, `walking`, `running`, `head-shake`, `mixed`, `heavy-tag`,
`attachment-variation`, `radio-event`, `long-simulation` (one virtual hour),
and `mass-sweep` are deterministic scenario entry points. Mass, attachment
position, stiffness, damping, and angular limits can be set from the CLI. The
animal behavior labels drive only the Gazebo-side trajectory; only acceleration
samples enter the LIS2DW12 bridge. Offline replay uses Gazebo simulation time
against the captured trace; `--live-lockstep` instead pauses/steps Gazebo as the
clock master while the existing Zephyr firmware runs in Renode and receives
each timestamped IMU sample. Set `RIOSE_ZEPHYR_ELF` to the Renode-profile ELF
for that mode. It prints a loopback-only live companion-panel URL while the run
is active; the panel labels logical anchor events and provisional power as
simulated/assumed. `--duration-s` truncates a longer scenario for staged
runtime and resource checks. The wake comparator is an explicitly experimental
approximation, not a silicon-accurate LIS2DW12 model. See the report for gates
and limits.

