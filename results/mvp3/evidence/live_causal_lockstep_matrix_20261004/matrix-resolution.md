# Corrected live lockstep scenario matrix

All runs use live Gazebo→Renode lockstep after the causal timestamp and ODR fixes. The first `heavy-tag` run ended with its wake IRQ asserted and is retained as a 10/11 partial result; the scenario now includes a 2 s standing clearance phase. Its corrected replacement is `../live_causal_lockstep_heavy_tag_settling/` and passes 11/11. The wrapper's first random-activity invocation used an argparse-rejected alias; it was rerun using the canonical CLI value `scenario_random_activity` and passes 11/11.

| Scenario | Result | Duration | Clock error | ODR observed | TX / logical anchor | Evidence |
|---|---:|---:|---:|---:|---:|---|
| standing | 11/11 | 10 s | 0 s | 12.5 Hz | 1 / 1 | `standing/` |
| walking | 11/11 | 16 s | 0 s | 12.5 Hz | 3 / 3 | `walking/` |
| running | 11/11 | 10 s | 0 s | 12.5 Hz | 2 / 2 | `running/` |
| head-shake | 11/11 | 8 s | 0 s | 12.5 Hz | 2 / 2 | `head-shake/` |
| mixed | 11/11 | 17 s | 0 s | 12.4706 Hz (finite-sample average) | 3 / 3 | `mixed/` |
| heavy-tag, corrected | 11/11 | 14 s | 0 s | 12.5 Hz | 3 / 3 | `../live_causal_lockstep_heavy_tag_settling/` |
| attachment-variation | 11/11 | 12 s | 0 s | 12.5 Hz | 2 / 2 | `attachment-variation/` |
| radio-event | 11/11 | 10 s | 0 s | 12.5 Hz | 2 / 2 | `radio-event/` |
| random-activity | 11/11 | 17 s | 0 s | 12.4706 Hz (finite-sample average) | 3 / 3 | `random-activity/` |

Every live run reports `SIMULATED` evidence. RF represents logical TX→anchor acceptance only; no physical propagation, RSSI, or antenna result is claimed.
