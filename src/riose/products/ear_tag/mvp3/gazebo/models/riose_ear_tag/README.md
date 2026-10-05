# RIOSE MVP3 ear-tag Gazebo model

This is a provisional simulation proxy. It is not a released, reviewed, or
physically validated design, and its simulated contacts or sensor output must
not be presented as physical measurements.

## Provenance and frame

The envelope, mounting-hole candidate, assumed mass and approximate center of
mass come from `hardware/spec.yaml` and
`results/mvp2/mechanical/geometry.json`. The current values are:

| Input | Default | Evidence/status |
| --- | ---: | --- |
| Envelope (X × Y × Z) | 38 × 68 × 18.5 mm | ASSUMED; commercial reference informs scale only |
| Mass | 27.1588 g | ASSUMED; geometry estimate |
| Approximate center of mass from envelope center/bottom datum | (-2.1476, 8.0090, 7.7498) mm | ASSUMED; geometry estimate |
| Hole diameter and source center | 4 mm; (+15, +30.8) mm in envelope XY | ASSUMED; fit review required |

The canonical link is `riose_ear_tag::tag_link`. Its origin is at the candidate
mounting-hole center, and `riose_ear_tag::attachment_pivot` is the named frame
for that origin. Local +Y points toward the hole end; the enclosure extends
toward local -Y. Shape geometry is a set of boxes with a square 4 mm relief
around the assumed round hole. The inertial values are a uniform-envelope box
approximation using the estimated mass; they do not represent measured
material distribution. If mass, envelope, or pivot changes, update the
`<inertial>` values and geometry poses together in `model.sdf`.

## Cow attachment contract

The runtime nested revolute attachment is named `riose_ear_tag_attachment`,
with parent `ear_left` and child `riose_ear_tag::tag_link` inside the cow model.
The cow link frame origin represents the candidate pin point. The CLI copies
the model into each experiment and applies the requested attachment offset to
both the nested include and joint frame so the tag pivot remains constrained.

Suggested initial orientation in the cow frame is RPY
`(+pi/2, 0, pi)` (approximately quaternion x/y/z/w = 0.5/0.5/0.5/0.5): it
maps tag local -Y down cow -Z and local +Z outward cow +Y. This is only a
coordinate convention for integration, not an anatomical fit assertion. The
cow model defines the attachment at the ear-link pivot:

```xml
<include>
  <uri>model://riose_ear_tag</uri><name>riose_ear_tag</name>
  <pose relative_to="ear_left">0 0.025 -0.015 1.570796 0 3.141593</pose>
</include>
<joint name="riose_ear_tag_attachment" type="revolute">
  <pose relative_to="ear_left">0 0.025 -0.015 0 0 0</pose>
  <parent>ear_left</parent>
  <child>riose_ear_tag::tag_link</child>
  <axis>
    <xyz>0 1 0</xyz>
    <limit>
      <lower>-0.523599</lower>
      <upper>0.523599</upper>
      <effort>1</effort>
      <velocity>6.28</velocity>
    </limit>
    <dynamics>
      <damping>0.02</damping>
      <spring_reference>0</spring_reference>
      <spring_stiffness>0.1</spring_stiffness>
    </dynamics>
  </axis>
</joint>
```

The shown angle bounds (±30 degrees), damping (0.02 N·m·s/rad), spring
reference (0 rad), stiffness (0.1 N·m/rad), effort (1 N·m), and velocity
(6.28 rad/s) are `ASSUMED` tuning defaults only. The CLI exposes position,
damping, stiffness, and angular limits as run inputs; they are not validated
retention or tissue properties. For a different pivot, move
`attachment_pivot` and update all visual, collision, sensor, and inertial poses
consistently.

## IMU

The model exposes a Gazebo IMU sensor named `imu` on the canonical link, with
topic `/riose/mvp3/imu/data` and a 50 Hz assumed update rate. The configured
zero-noise entries are neutral simulator defaults; they do not characterize a
LIS2DW12 or imply sensor performance. Gazebo transport/ROS bridging is the
responsibility of the consuming simulation setup.
