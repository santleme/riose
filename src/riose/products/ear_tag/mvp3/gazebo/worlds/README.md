# Gazebo worlds

`riose_mvp3.sdf` is the SDFormat 1.9 starter world for Gazebo Harmonic. It
provides gravity, a 1 ms physics step, the ground plane, directional light,
scene styling, the standard Physics / UserCommands / SceneBroadcaster systems,
and one static visualization fixture for the virtual receiver anchor.

The world includes `model://riose_cow`; the cow includes `model://riose_ear_tag`
as a nested model. Its revolute attachment joint is inside the cow model so
Gazebo Sim can resolve both endpoints. A launch environment must add the
experiment's generated `sim_assets/models` directory (or the source
`gazebo/models` directory) to `GZ_SIM_RESOURCE_PATH`.

The CLI copies both models into each experiment's `sim_assets/models` and
applies the selected tag mass, attachment point, spring, damping, and hinge
limits there. These values remain assumed inputs. The saved copies make each
run reproducible without modifying the source assets.

The anchor is a fixed mast with a colored receiver marker. It does not emulate
radio propagation or receive packets by itself; those behaviors belong to the
MVP3 RF/telemetry integration.

The world has been parsed and launched with Gazebo Harmonic 8.15.0. Runtime
captures include the cow/tag poses and IMU stream. The simplified geometry
still emits a DART warning for unsupported ellipsoid collision geometry; DART
uses generated meshes for those collisions.
