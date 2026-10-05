# MVP 3 digital animal and ear-tag twin report

**MVP3 digital-integration milestone: `MVP3_DIGITAL_INTEGRATION_PASSED` (11/11), including a live one-hour Gazebo/Renode run.** Product and physical validation remain partial. All evidence is `SIMULATED`; mechanical, sensor, radio, and energy inputs retain their `ASSUMED` or model provenance. No physical animal, ear tag, radio, battery, or lab measurement was used. MVP 1 and MVP 2 remain in place.

The first integrated GUI result is [`live_lockstep_walking_visual_complete/`](../results/mvp3/evidence/live_lockstep_walking_visual_complete/). A corrected headless repeat is [`live_lockstep_causal_dashboard_walk_03/`](../results/mvp3/evidence/live_lockstep_causal_dashboard_walk_03/), with an attached loopback companion panel. Both runs used Gazebo Harmonic as simulation-clock master and the existing Zephyr firmware in Renode. The corrected 16 s run injected all **801 timestamped Gazebo IMU samples** only after Renode reached each sample timestamp. At all 80 paused-step barriers, Renode `currentTime` was queried directly; maximum observed clock error was **0 s**. The LIS2DW12 wake comparator evaluated **200 samples at 12.5 Hz**, matching the modeled ODR rather than evaluating all 50 Hz Gazebo samples. The corrected short scenario matrix and five-mass sweep also have live records. The completed one-hour live run is summarized below.

## Answers to the milestone questions

| # | Question | Result |
|---|---|---|
| 1 | Was the cow physically simulated? | Yes. Gazebo Harmonic simulated gravity, collisions, articulated body/neck/head/ears, tag joint dynamics, an anchor, and deterministic motion. The cow is a procedural recognizable placeholder, not a licensed asset or validated biomechanical model. |
| 2 | Did the tag stay attached? | In the live walking run, the measured relative-pivot drift was **0.000000432 m** (0.43 μm), below the 5 mm software tolerance. This checks simulated poses, not physical retention. |
| 3 | What happened during walking? | In the live 16 s scenario, the tag followed the articulated ear. The cow translated **1.789 m** during 12 s of commanded walking, versus the **6.60 m** target. Peak absolute sensor-frame acceleration was **3.23 g X, 2.35 g Y, 0.21 g Z**. The trajectory materially undershot its target. |
| 4 | What happened during running? | The separate 10 s Gazebo run completed. Peak absolute acceleration was **2.79/2.65/0.26 g X/Y/Z**; the cow moved **1.66 m** versus a **12.8 m** target. This is a procedural motion profile, not validated bovine running. Firmware was not live-coupled for this run. |
| 5 | What happened during head shake? | The separate 8 s run recorded **2.14/1.00/0.19 g X/Y/Z** and **0.326 rad (18.7°)** maximum tag orientation change. Firmware response was not live-coupled for this run. |
| 6 | Which accelerations were observed? | The live walking peaks are above. Separate standing capture was about **0.77/0.64/0.001 g X/Y/Z**; its nonzero X/Y baseline reflects simulated gravity and sensor orientation. All values are `SIMULATED`, not animal measurements. |
| 7 | How did mass affect motion? | The corrected live `ASSUMED` 20, 25, 30, 35, and 40 g sweep produced X/Y/Z peak absolute acceleration of **1.93/1.93/0.17, 2.52/2.15/0.17, 1.51/1.91/0.34, 1.82/2.22/0.26, and 3.67/2.87/0.25 g**, respectively. Pivot drift stayed below 1 μm in every run. The response is non-monotonic; the model does not validate acceptable mass or attachment force. |
| 8 | How did attachment position affect motion? | The offset `(0.008, 0.035, -0.020) m` run stayed within **0.82 μm** relative-pivot drift and recorded **3.22/2.21/0.31 g X/Y/Z** peaks. A paired run with the same seed and timing was not preserved, so no causal position comparison is claimed. |
| 9 | Did Gazebo feed the virtual IMU correctly? | Yes in the corrected live run: all 801 timestamped acceleration samples in m/s² were converted to g and injected into the Renode LIS2DW12 model at their Gazebo timestamps. Zephyr ran from the existing C/Zephyr ELF and read 49 output samples. The firmware received physical quantities only; no scenario label was sent. |
| 10 | Did firmware wake from those events? | In the corrected live walk, the model recorded **46 WU events generated**, **46 event reads**, and **50 source reads**; the IRQ was clear at the end. The comparator evaluated 200 held physical samples at an observed 12.5 Hz. Zephyr generated two post-boot `ALERT` packets during WALKING and ended in `SLEEP` (state code 2 read from its state-trace GPIO pins). The wake comparator remains a first-order high-pass approximation, not validated LIS2DW12 silicon behavior. |
| 11 | How many transmissions occurred? | Renode captured **3 completed logical TX/TX_DONE** operations: one startup packet and two movement-phase `ALERT` packets at **2.960 s** and **12.961 s**. The logical anchor accepted all **3** events. The companion panel animates a short logical tag-to-anchor indicator after acceptance; this is not a physical RF transmission or reception. |
| 12 | What was the energy impact? | The one-hour trace produced **9.335 μAh** modeled charge over exactly **3,600 s**, including the terminal sleep period; ngspice and all 16 modeled sensitivity cases passed. Currents and circuit inputs remain `ASSUMED`/`DATASHEET`; this is not a battery measurement or autonomy estimate. |
| 13 | Did Renode remain synchronized? | Yes. In the one-hour live run Gazebo was clock master, physics stepped at 1 ms in batches of 200, and Renode received all **180,001** timestamped IMU samples. Direct clock checks at **18,000** barriers showed a maximum error of **0 s**. |
| 14 | Did the anchor receive events? | Yes as a `LOGICAL_RF_EVENT`: **3/3** TX records were accepted by the virtual anchor. No path loss, physical RF result, or RSSI is asserted. |
| 15 | Which parts remain approximations? | Procedural cow gait and root drive; assumed ear/tag geometry, mass and joint stiffness/damping; first-order LIS2DW12 wake-filter model; Renode's STM32L071 surrogate for the STM32L031; logical SX1262 transactions; assumed current/regulator values; unvalidated antenna model. The loopback companion panel shows synchronized firmware state, IMU/orientation, telemetry, TX, logical anchor acceptance, and a provisional assumed MCU/TX charge estimate with an animated logical event indicator. It runs beside Gazebo rather than inside the Gazebo viewport; the live charge omits final IMU-event accounting, which remains in the post-run MVP2 power analysis. |
| 16 | What did we learn about tag design? | The constrained attachment remained stable in the simulated runs, while acceleration depended strongly on the assumed trajectory, orientation, mass, and damping. Root travel missed its target substantially. These results identify model inputs to calibrate; they do not support a physical fit or animal-welfare decision. |
| 17 | Should an MVP 2 decision change? | No hardware or RF decision should change from these simulations alone. Keep the firmware/digital-twin architecture and compare new physical measurements against these explicit assumptions before revising the design. |
| 18 | Are we ready for a physical prototype? | The digital integration gate passes for the focused walking run, so a controlled bench prototype can be planned. The simulation does not establish that the tag is ready to fit to an animal: mechanical retention, mass, sensor threshold/filter behavior, power, antenna, and RF performance still need physical validation. |

## Gate, synchronization, and artifacts

The saved corrected short matrix and mass sweep all pass the 11/11 gate. Their summary is [`matrix-resolution.md`](../results/mvp3/evidence/live_causal_lockstep_matrix_20261004/matrix-resolution.md); per-run artifacts preserve the per-sample injection, per-batch clock barriers, Renode SX1262 trace, canonical firmware trace, logical RF/anchor events, and power schedule/output. The original 40 g scenario omitted a terminal rest interval and ended with a wake IRQ asserted (10/11); the scenario now includes a 2 s standing clearance phase, and the corrected rerun passes 11/11. Recovery work also removed the Gazebo iteration cap that expired during paused stepping, corrected Renode monitor greeting/command framing and JSON command echo, made the Gazebo clock authoritative after late service replies, cleaned up Gazebo on Ctrl-C, and kept live recorders open for the full simulated run instead of a fixed wall-clock duration.

### Corrected live scenario matrix and mass sweep

The corrected live matrix ran standing (10 s), walking (16 s), running (10 s), head shake (8 s), mixed activity (17 s), heavy tag (14 s), offset attachment (12 s), radio event (10 s), and seeded random activity (17 s). All nine final records passed 11/11. Every run observed **0 s** maximum Renode/Gazebo clock error, the 12.5 Hz modeled comparator rate (finite-sample averages were 12.4706 Hz for the two 17 s scenarios), and matching TX/logical-anchor counts. The heavy-tag correction adds 2 s of standing after movement so pending wake IRQs are serviced before the session ends.

The refreshed five-mass sweep at 20/25/30/35/40 g completed at 16 s per run with 11/11 for every mass. Each observed 0 s maximum clock error, 12.5 Hz comparator evaluation, three completed TX and three logical anchor accepts, and a deasserted wake IRQ at end. Results and CSV are under [`live_causal_lockstep_mass_sweep_20261004/`](../results/mvp3/evidence/live_causal_lockstep_mass_sweep_20261004/). Acceleration response remains non-monotonic and entirely simulated.

### Historical live scenario coverage before causal/ODR correction

The rows below completed with 11/11 in earlier live artifacts, before fixing causal sample injection and ODR-rate comparator evaluation. They remain useful simulated motion evidence, but their firmware wake/TX counts are historical and require rerunning on the corrected bridge before being treated as current end-to-end coverage. Standing is evaluated for quiescence; motion scenarios are evaluated for movement-derived wake/TX.

| Live scenario | Virtual duration | Injected IMU samples | Completed TX | Movement-derived TX | Root displacement |
|---|---:|---:|---:|---:|---:|
| Standing | 10 s | 501 | 1 (boot only) | 0 | 0.000 m |
| Walking (GUI) | 16 s | 801 | 3 | 2 | 1.789 m |
| Running | 10 s | 501 | 2 | 1 | 1.539 m |
| Head shake | 8 s | 401 | 2 | 1 | 0.000 m |
| Mixed activity | 17 s | 851 | 3 | 2 | 0.849 m |
| 40 g tag | 12 s | 601 | 2 | 1 | 1.090 m |
| Offset attachment | 12 s | 601 | 2 | 1 | 1.133 m |
| Radio event | 10 s | 501 | 2 | 1 | 0.724 m |
| Seeded random activity | 17 s | 851 | 3 | 1 | 0.400 m |

The historical 20/25/30/35/40 g sweep also passed 11/11 in every run; X/Y/Z peak accelerations were respectively `1.93/1.93/0.17`, `2.52/2.15/0.17`, `1.51/1.91/0.34`, `1.82/2.22/0.26`, and `3.67/2.87/0.25 g`. Pivot drift stayed below 1 μm in all five runs. Those artifacts predate the causal/ODR correction; the refreshed sweep above supersedes them for current integration evidence.

The viewer at [`imu_viewer.html`](../results/mvp3/evidence/live_lockstep_walking_visual_complete/imu_viewer.html) maps firmware, RF, anchor, and power events onto Gazebo time only after validating the live barrier records. It is a recorded replay view, not a live telemetry overlay. The `--visual --live-lockstep` walking run completed with the Gazebo 3D scene open and the Zephyr ELF configured.

The earlier one-hour headless Gazebo-only case completed **3,600 simulated seconds** in **709.7 s wall time** (RTF 5.07); it did not execute Zephyr. It is distinct from the completed live run below. Peak Z acceleration reached **15.74 g**, which is not physically interpretable without model calibration. Neither run is a Ryzen 7 7700X benchmark.

The 60-second and 600-second live lockstep preflights completed in **166.9 s** and **2,941.7 s** wall time. The full one-hour live record is [`live_long_simulation_lockstep_1h_20261004_retry1/`](../results/mvp3/evidence/live_long_simulation_lockstep_1h_20261004_retry1/). It completed **3,600 simulated seconds** in **43,700 s wall time** including startup and final artifact generation (RTF **0.0824**), with exit code 0 and an **11/11** report gate. The bridge injected **180,001** raw IMU samples across **18,000** paused-step barriers; maximum observed Renode/Gazebo clock error was **0 s**. The comparator evaluated **45,000 samples at 12.5 Hz** against the configured 13 Hz ODR. Zephyr completed **21 TX** (**18 movement-derived**); the logical anchor accepted **21/21**, and firmware ended in `SLEEP`. The ear/tag pivot drift was **1.68×10⁻⁷ m** (software tolerance 5 mm). The trace-derived power window now spans all **3,600 s**, with **9.335 μAh** simulated charge, ngspice `PASS`, and **16/16** sensitivity analyses passed; the temperature axis remains explicitly unmodeled. Currents remain assumed/datasheet inputs. Resource profiling measured **1,150,992 KiB** peak process-tree RSS, up to **219.7%** CPU, and at least **832 GiB** free disk. The raw Gazebo pose stream is about **14.7 GB**. The long profile uses articulated walking motion in place: measured root travel was **0.0053 m** with no requested forward displacement, so it does not establish forward gait performance. This run demonstrates synchronized software stability for an hour; it is not a physical validation or Ryzen 7 7700X benchmark.

## Remaining MVP 3 work

- If a single-window experience is required, package the existing loopback companion fields into a Gazebo GUI plugin; the currently implemented small panel is separate from the 3D viewport.
- Improve root trajectory tracking and calibrate gait, ear motion, attachment parameters, and physical load ranges.
- Capture MCU state transitions over the complete live run; current final `SLEEP` is read from state-trace GPIO pins, while TX, IRQ, and IMU counters come from virtual peripherals.
- Keep RF marked `LOGICAL_RF_EVENT` and `ANTENNA_MODEL_UNVALIDATED` until an explicitly validated propagation model is used.

## Reproduction

```sh
source hardware/activate-mvp2-toolchain.sh
west build -b nucleo_l031k6 -d /tmp/riose-mvp3-renode-build \
  hardware/firmware/zephyr -- \
  -DCONF_FILE='prj.conf boards/nucleo_l031k6.conf boards/nucleo_l031k6_renode.conf'
RIOSE_ZEPHYR_ELF=/tmp/riose-mvp3-renode-build/zephyr/zephyr.elf \
  uv run riose mvp3 run 02_walking --headless --live-lockstep
uv run riose mvp3 visualize results/mvp3/EXPERIMENT_DIR
uv run riose mvp3 report results/mvp3/EXPERIMENT_DIR
```

The firmware remains the existing C/Zephyr program. The physical board build used the Renode configuration fragment; Renode remains an STM32L071 surrogate. Sionna/GPU propagation is optional and was not used.
