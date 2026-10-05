# MVP3 experiment report: 02_walking

- Gate: `MVP3_DIGITAL_INTEGRATION_PASSED` (11/11 checks)
- Evidence: `SIMULATED`; no physical measurements or animal data.
- Physical RF result: `ANTENNA_MODEL_UNVALIDATED`; RSSI is not estimated.

## Gate checks

| Requirement | Status | Evidence |
|---|---|---|
| gazebo_operational | PASS | Gazebo process exited successfully |
| animal_model_operational | PASS | Gazebo model-load evidence |
| ear_attachment_operational | PASS | max relative pivot drift 4.320681274291866e-07 m |
| physical_movement_generated | PASS | cow root moved 1.789 m; requested forward displacement 6.600 m |
| imu_integration_operational | PASS | 208 Gazebo IMU samples |
| renode_bridge_operational | PASS | Live Gazebo IMU samples reached the Renode LIS2DW12; Zephyr and SX1262 ran in the same lockstep session |
| firmware_wake_tx_operational | PASS | movement-derived IRQ, firmware wake and follow-up TX evidence |
| rf_anchor_operational | PASS | logical radio receiver evidence |
| power_integration_operational | PASS | validated SIMULATED power schedule and native analysis output |
| recording_operational | PASS | validated IMU/pose sample count, monotonic simulation timestamps, finite sensor values, and nearest-pose time alignment |
| scenario_completed | PASS | successful Gazebo exit with IMU and validated recording artifacts |

## Recorded acceleration

Gazebo IMU samples: 208.
Firmware receives acceleration samples only; scenario labels remain on the orchestration side.

## Animal root displacement

Recorded cow root displacement: 1.789 m; requested forward displacement: 6.600 m.
