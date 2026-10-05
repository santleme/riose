# MVP3 experiment report: 09_long_simulation

- Gate: `MVP3_DIGITAL_INTEGRATION_PASSED` (11/11 checks)
- Evidence: `SIMULATED`; no physical measurements or animal data.
- Physical RF result: `ANTENNA_MODEL_UNVALIDATED`; RSSI is not estimated.

## Gate checks

| Requirement | Status | Evidence |
|---|---|---|
| gazebo_operational | PASS | Gazebo process exited successfully |
| animal_model_operational | PASS | Gazebo model-load evidence |
| ear_attachment_operational | PASS | max relative pivot drift 1.6792714037563227e-07 m |
| physical_movement_generated | PASS | peak ear/tag angular speed 0.860 rad/s; linear speed 0.242 m/s |
| imu_integration_operational | PASS | 46800 Gazebo IMU samples |
| renode_bridge_operational | PASS | Live Gazebo IMU samples reached the Renode LIS2DW12; Zephyr and SX1262 ran in the same lockstep session |
| firmware_wake_tx_operational | PASS | movement-derived IRQ, firmware wake and follow-up TX evidence |
| rf_anchor_operational | PASS | logical radio receiver evidence |
| power_integration_operational | PASS | validated SIMULATED power schedule and native analysis output |
| recording_operational | PASS | validated IMU/pose sample count, monotonic simulation timestamps, finite sensor values, and nearest-pose time alignment |
| scenario_completed | PASS | successful Gazebo exit with IMU and validated recording artifacts |

## Recorded acceleration

Gazebo IMU samples: 46800.
Firmware receives acceleration samples only; scenario labels remain on the orchestration side.

## Animal root displacement

Recorded cow root displacement: 0.005 m; requested forward displacement: 0.000 m.

## Power analysis

Modeled window: 3600.0 s; simulated charge: 9.335000000004557 µAh; charge status: `SIMULATED_AT_ASSUMED_REGULATOR_OUTPUT_VOLTAGE`; ngspice: `PASS`.
Load currents retain their ASSUMED/DATASHEET provenance; these are not battery measurements.
