# Gazebo worlds

`riose_mvp3.sdf` is the SDFormat 1.9 world for Gazebo Harmonic. It provides
gravity, a 1 ms physics step, the ground plane, a warm directional daylight
setup, a bounded paddock fence, a water trough, a distant field shed, and the
standard Physics / UserCommands / SceneBroadcaster systems. The static receiver
mast remains beside the simulated animal path.

The world includes `model://riose_cow`; the cow includes `model://riose_ear_tag`
as a nested model. Its revolute attachment joint is inside the cow model so
Gazebo Sim can resolve both endpoints. A launch environment must add the
experiment's generated `sim_assets/models` directory (or the source
`gazebo/models` directory) to `GZ_SIM_RESOURCE_PATH`.

The CLI copies both models into each experiment's `sim_assets/models` and
applies the selected tag mass, attachment point, spring, damping, and hinge
limits there. These values remain assumed inputs. The saved copies make each
run reproducible without modifying the source assets.

The field dressing is deliberately lightweight SDF geometry. The broad ground
collision is still flat and the scene contains no vegetation, weather, terrain
height field, or photographic assets. This improves scale and composition cues
without implying a validated farm environment or changing the animal's tested
ground contact. Fence rails and end rails have matching collision geometry;
the receiver mast and trough are physical static props, while the distant shed
is visual context only.

The anchor is a fixed mast with a colored receiver marker. It does not emulate
radio propagation or receive packets by itself; those behaviors belong to the
MVP3 RF/telemetry integration. The current sun and scene settings are a single
daylight presentation preset. Golden-hour, overcast, capture cameras, and
automated shot sequences are not implemented yet.

The world has been parsed and launched with Gazebo Harmonic 8.15.0. Runtime
captures include the cow/tag poses and IMU stream. The simplified geometry
still emits a DART warning for unsupported ellipsoid collision geometry; DART
uses generated meshes for those collisions.
