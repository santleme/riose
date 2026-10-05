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

## Gazebo visual camera and screenshot pass

The visualization package also includes the production still-camera pass. Its
camera and sequence data live in `camera_presets.json` and `sequences.json`;
lighting variants live in `lighting_presets.json`. All capture runs launch the
same MVP3 cow/tag/receiver world and preserve its physics and sensors. Lighting
variants edit only the SDF scene colors and directional sun in a temporary copy.

```sh
source /opt/ros/jazzy/setup.bash
uv run riose mvp3 visual list
uv run riose mvp3 visual capture --all --lighting DAY
uv run riose mvp3 visual capture --sequence pitch_short --lighting GOLDEN_HOUR
uv run riose mvp3 visual capture --sequence engineering --lighting OVERCAST_TECH
```

Still images and `capture_manifest.json` go to `results/mvp3/visual/` by
default. Use `--output PATH` to choose another directory. The renderer must be
able to open a Gazebo GUI; capture waits for `/gui/screenshot` to write and
validate each PNG, and returns an error if it only acknowledges the queued
request. `--width` and `--height` set the GUI output dimensions; the default is
1920x1080. `--keep-open` leaves the GUI running after capture until Ctrl-C.

Named views cover front, side, rear, three-quarter front/rear, tag macro,
cattle/tag context, paddock, receiver, and provisional engineering angles.
Each still starts the same world at its initial state and applies that preset's
horizontal FOV, then records the rendered resolution and FOV in the manifest.
Sequence files define 22-second `pitch_short`, 51-second `product_demo`, and
`engineering` shot timing, but the command does not encode a video or synthesize
continuous camera movement. Those sequences are capture/storyboard foundations,
not finished footage.

`DAY`, `OVERCAST_TECH`, and `GOLDEN_HOUR` change only ambient/background/sun
visual settings. They do not alter world gravity, collisions, initial animal
pose, sensor rates, or telemetry semantics. The yellow housing currently uses
a COLLADA embedded diffuse albedo texture because Gazebo Harmonic rendered its
OBJ counterpart white even with SDF and OBJ material data. The bore pin uses a
separate SDF PBR polymer material. The housing roughness map and fine
micro-normal detail are not active in the validated render, so its surface
response remains a known material limitation. The bore pin is a visual-only
ASSUMED locking detail pending mechanical confirmation; it does not replace the
existing joint or collision model.

The GUI screenshot service can report that a request was queued before a PNG
exists. The command checks file existence, PNG signature, and size before it
reports `CAPTURED`. Actual image review should be done on these Gazebo frames;
mesh previews alone are not visual acceptance evidence. Ogre2 availability is
validated on this workstation, but GPU acceleration is not: `/dev/dri` was not
available during the current pass. Frame rate and capture latency therefore
remain unreported until measured on the intended graphics hardware.
