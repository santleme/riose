# `riose_cow` Gazebo Harmonic model

This is an explicit, primitive-only bovine approximation for the RIOSE MVP3
Gazebo scene. It uses an elongated barrel, coat patches, sloped neck, muzzle,
nostrils, eyes, horn stubs, two ears, four legs, and hoof blocks. Coordinates
use +X forward, +Y cow-left, +Z up. The model name is `riose_cow`; preserve it
when including/spawning the model if the documented absolute topics are used.

## Evidence boundary

The animal anatomy, dimensions, link inertias, collision extents, body mass,
limb masses, damping, and controller gains are **ASSUMED / SIMULATED**. The
450 kg torso mass plus neck, head, ears, and four 7.5 kg leg links are plausible
simulation inputs, not measured cattle properties or a validated biomechanical
model. One hip/shoulder revolute joint per leg is a deliberately limited
kinematic approximation; there are no knee/hoof articulation joints.

The shapes are procedural SDF ellipsoids, capsules, cylinders, spheres, and
boxes; no external mesh or image assets are required. The controller is the
Gazebo Sim Harmonic built-in `JointTrajectoryController`. This model explicitly
subscribes to `/model/riose_cow/joint_trajectory` and accepts
`gz.msgs.JointTrajectory`.
Its controlled joints, in configured order, are:

1. `body_translation_joint`: world-to-body prismatic X translation, limited to
   -50 to +50 m for long deterministic trajectories.
2. `head_pitch_joint`: neck/head pitch, limited to -0.30 to +0.30 rad.
3. `ear_left_joint`: left-ear flap, limited to -0.25 to +0.25 rad.
4. `front_left_leg_joint`, `front_right_leg_joint`, `rear_left_leg_joint`,
   `rear_right_leg_joint`: each hip/shoulder pitch, limited to -0.35 to
   +0.35 rad.

A seeded trajectory can therefore drive translation and articulated motion
through one topic. Trajectory interpolation, seed choice, simulation step, and
world physics remain the caller's responsibility. This model does not claim
that a particular trajectory is a validated walking gait.

## Ear-tag include and scoped-joint contract

Include the model with the canonical name in the world:

```xml
<include>
  <uri>model://riose_cow</uri>
  <name>riose_cow</name>
  <pose>0 0 0 0 0 0</pose>
</include>
```

The left tag anchor is the origin of the cow link `ear_left` (the cow-left
side, +Y). The CLI copies this model into each experiment, nests the tag, and
creates a model-scoped physical joint so Gazebo resolves the endpoints:

```xml
<include><uri>model://riose_ear_tag</uri><name>riose_ear_tag</name>
  <pose relative_to="ear_left">0 0.025 -0.015 1.570796 0 3.141593</pose>
</include>
<joint name="riose_ear_tag_attachment" type="revolute">
  <pose relative_to="ear_left">0 0.025 -0.015 0 0 0</pose>
  <parent>ear_left</parent><child>riose_ear_tag::tag_link</child>
  <axis><xyz expressed_in="ear_left">1 0 0</xyz>
    <limit><lower>-0.523599</lower><upper>0.523599</upper></limit>
    <dynamics><damping>0.02</damping><spring_stiffness>0.1</spring_stiffness></dynamics>
  </axis>
</joint>
```

The tag-link origin is the assumed hole pivot. The copied experiment model
applies any requested attachment offset. Pose and fit remain provisional, not
validated. IMU data belongs to the tag model at
`/riose/mvp3/imu/data`; this cow model publishes no IMU.

## Runtime integration notes

Gazebo Harmonic must load the built-in `JointTrajectoryController` system and
support the SDF 1.9 geometry used here. The world must load its physics, scene,
and sensor systems as needed. Runtime loading, contact stability, controller
response, ear/tag alignment, and sensor publication have not been verified in
this checkout. If the simulator rejects world-parent prismatic joints or the
SDF plugin parameters, treat that as an integration issue and update the model
and world together without changing the anatomy evidence labels.
