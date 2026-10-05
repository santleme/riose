# MVP3 offline synchronized recording view

Generate a self-contained HTML replay from a recorded experiment:

```sh
uv run python -m riose.products.ear_tag.mvp3.visualization results/mvp3/<experiment>
```

The experiment needs `gazebo_imu.csv` (`timestamp_s,x_g,y_g,z_g`) and
`recording.jsonl` with explicit `simulation_timestamp_s`,
`pose_sample_timestamp_s`, and `tag_pose.position.{x,y,z}` fields. The command
writes `imu_viewer.html` plus an `imu_viewer.svg` IMU plot beside the records.
The HTML has timestamped IMU and tag-position plots, an offline 2D trajectory
projection with top/side/front presets, and event lanes when their evidence is
available.

Firmware events require `firmware_trace.jsonl` with the versioned SIMULATED
schema and integer `timestamp_us`. Logical RF and anchor lanes are read only
from their own `rf_events.jsonl` and `anchor_events.jsonl` records. Power
intervals are read from `power/schedule.jsonl` (or root `schedule.jsonl`) and
must carry SIMULATED status and firmware trace provenance. Event detail retains
its source file and recorded time; power schedule current remains ASSUMED.

Gazebo pose/IMU are displayed in Gazebo simulation time. By default firmware,
RF, anchor, and schedule lanes remain in source trace time. Offline lanes are
overlaid only when `firmware_replay.json` contains a valid
`riose.mvp3.clock_mapping/v1` mapping for `AFFINE_OFFLINE_RESD_REPLAY` with
`live_lockstep: false`. Live lanes can be overlaid from the experiment
`manifest.json` when it declares `LIVE_GAZEBO_MASTER_LOCKSTEP`,
`live_lockstep: true`, and `barrier_evidence` contains strictly increasing
Gazebo/Renode timestamps that agree at every recorded clock barrier. Invalid
or missing mappings leave the event lanes on their source trace-time axis and
show the validation reason.

An optional `alignment_check` is independently recomputed against the firmware
trace and normalized Gazebo IMU CSV. Its TX_START time must match a recorded
firmware event (within one microsecond, the trace timestamp resolution), and
its mapped time, nearest IMU timestamp, and absolute error must match those
source records within one microsecond. If the mapping or supplied check is
invalid, no overlay is applied; event lanes remain on their source trace-time
axis with the reason shown. Logical anchor acceptance is not physical RF
reception. The projection is a static recorded-data view, not a live Gazebo 3D
animation. The HTML uses inline CSS, SVG, and JavaScript only; it needs no
server or network connection.
