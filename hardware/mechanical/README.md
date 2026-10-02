# RIOSE mechanical digital model

Headless, parameter-driven first-pass envelope and fit analysis. Required
dimensions come from `hardware/spec.yaml`; only explicitly labeled assumed
coordinates/clearances have fixture-friendly fallbacks. The current product
spec records a candidate layout; it is not a reviewed mechanical drawing.

Run from the repository root:

```sh
python3 -m hardware.mechanical.model \
  --spec hardware/spec.yaml \
  --output results/mvp2/mechanical/geometry.json
```

Optional STEP export uses CadQuery when installed:

```sh
python3 -m hardware.mechanical.model --step results/mvp2/mechanical/ear_tag.step
```

The report contains component bounding boxes, cavity fit checks, estimated
volume/mass and center of mass, along with each source/status. Body and parts
are simplified boxes; the battery is represented by a cylindrical mass estimate
and a rectangular fit envelope. Material density and component masses are
assumptions. The mounting-hole location and package layout are provisional.
The current assumed layout uses a 38 × 68 × 18.5 mm enclosure, a 30 × 48 mm PCB
in the lower Y bay, and a TLL-5902 body envelope in the separate upper Y bay.
The model checks the cell/PCB clearance and the mounting-hole clearance. A
passing bounding-box result does not validate the cell terminals, tolerances,
sealing, retention, antenna performance, or physical fit. The gate cannot become
`READY_FOR_PHYSICAL_PROTOTYPE` here.

Required spec records are:

* `mechanical.enclosure.width_mm`, `height_mm`, `thickness_mm`,
  `wall_thickness_mm`, `mounting_hole_diameter_mm`, and the assumed
  `mounting_hole_center_x_mm`;
* `mechanical.pcb.width_mm`, `height_mm`, `thickness_mm`;
* `mechanical.battery.diameter_mm`, `length_mm`;
* `mechanical.layout_candidate.pcb_center_x_mm`, `pcb_center_y_mm`,
  `battery_center_x_mm`, `battery_center_y_mm`;
* `mechanical.minimum_clearance_mm`;
* `mechanical.antenna.width_mm`, `height_mm`, `thickness_mm`, `keepout_mm`;
* `mechanical.components.{mcu,radio,imu}.{width_mm,height_mm,thickness_mm}`;
* optional `materials.{enclosure,pcb,battery,component}_density_g_cm3`.

Each value must use the spec record `{value, unit, source, status}`. Lengths
accept `mm`, `cm`, `m`, or `in`. Missing material densities use explicit
`ASSUMED` generic fallbacks with warnings; missing geometry is an error.

The default project environment may not include CadQuery. JSON fit and mass
analysis remains available without it; STEP export requires the optional
CadQuery runtime exposed by `hardware/activate-mvp2-toolchain.sh`. When
available, `--step` and `--stl` export the same
assembly geometry in both formats. The enclosure is modeled as a closed hollow
shell with a through mounting hole. A completed analysis exits 0 even when it
finds fit blockers; callers should inspect `fit.fits` and `gate`. Invalid spec
or missing optional CAD runtime exits 2. YAML input requires PyYAML.
