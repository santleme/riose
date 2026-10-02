# RIOSE MVP 2 antenna experiments

Run headlessly from the repository root:

```sh
python -m hardware.antenna.run --spec hardware/spec.yaml --output results/mvp2/antenna
```

The command writes `antenna_experiments.json` and `antenna.csv` with the five
scenario IDs: free space, PCB, battery, enclosure, and experimental animal
proximity. If openEMS/CSXCAD are missing, each case is `NOT_AVAILABLE` and RF
metrics are null. If bindings exist without a configured adapter, status is
`ADAPTER_NOT_CONFIGURED`; no analytical estimate is represented as a simulation.

The expected spec records are under `antenna` and each includes `value`,
`unit`, `source`, and `status`. A `MEASURED` status is rejected for MVP 2.
Initial defaults (915 MHz, 82 mm element length, feed coordinate, 2 mm
clearance and meandered monopole topology) are all `ASSUMED`, not findings.

## Explicit simulation candidate

`hardware/antenna/candidate_model.json` contains
`planar_four_run_monopole_v1`; its values carry their own `source` and
`ASSUMED` status. Keeping this candidate outside `hardware/spec.yaml` preserves
the spec hash used by the existing MVP2 reports. The model's serialized
centerline defines an 82 mm four-run path, with a 1 mm PEC
trace, a lumped feed at its first point, and a 30 by 48 mm finite PEC ground
plane. Non-free-space cases add a generic FR-4-like dielectric substrate. The
other cases add one isolated surrogate: the assumed cell envelope as a PEC
cuboid, the assumed enclosure dimensions as a six-wall generic dielectric
shell, or the existing homogeneous dielectric animal sensitivity slab.

These are reproducible numerical assumptions, not a released antenna design.
Conductor loss, cell internals, selected PCB stackup, enclosure details and
animal anatomy are not modeled. The simplified mechanical bounding-box
candidate now passes the model's cavity, PCB, and mounting-hole checks with an
assumed 0.5 mm clearance. The current assumed enclosure candidate is 38 by 68
by 18.5 mm, with its lower wall still touching the simulation ground plane at
z=-1.2 mm. Neither that geometric result nor the RF simulations validate
terminals, tolerances, retention, materials, or physical assembly. The animal
case is not tissue validation.
Thus a solver run can describe only this assumed candidate and cannot change
the physical prototype gate to ready. A `COMPLETED` antenna row means the
openEMS run completed and the declared two-mesh S11 comparison passed; it does
not mean the geometry, materials, RF performance or product are validated.

To select the adapter in an environment with the installed openEMS bindings:

```sh
source hardware/activate-mvp2-toolchain.sh
export RIOSE_OPENEMS_ADAPTER=hardware.antenna.openems_adapter
python -m hardware.antenna.run --spec hardware/spec.yaml --output results/mvp2/antenna
```

The adapter stores the assumed geometry description, both mesh runs, native
solver logs, raw openEMS/NF2FF files, and derived CSVs per scenario. It reports
`FAILED` when the field-energy stop criterion is not reached before the
timestep limit, the frequency sweep does not bracket an S11 minimum, solver
artifacts are missing, derived metrics are invalid, or coarse/fine S11
comparison misses its declared tolerance. It does not substitute analytical
estimates. A separate battery-surrogate check reached the 160,000-step limit
before the energy criterion, so the adapter stopped before starting the fine
mesh and returned no RF metrics. Metric and raw-file paths are relative to the
scenario directory and are verified there before a row can be marked
`COMPLETED`.

Before `fdtd.Run()` allocates native solver fields, the adapter counts the
final smoothed mesh and applies a fail-closed memory preflight. Its planning
estimate is 128 bytes per cell, with a default cap of 128 MiB. This leaves the
largest recorded 2 mm candidate mesh (505,760 cells, about 61.7 MiB estimated)
under the cap, while rejecting the 1 mm free-space pilot (2,322,540 cells,
about 283.5 MiB estimated). A rejected mesh returns `FAILED` with no RF metrics.
The cap can be changed in MiB with
`RIOSE_OPENEMS_MAX_ESTIMATED_MEMORY_MIB`; invalid values also fail closed.
This estimate is a conservative planning guard, not a measured or guaranteed
process-RSS limit: actual native memory depends on solver internals and the
environment. It does not change mesh spacing, domain, PML, timestep, or solver
criteria.

### Free-space convergence pilot

The simulation-only candidate was also run outside the five-scenario CLI at
1 mm to inspect refinement after the 4/2 mm pair failed. The three
free-space runs returned these solver-derived summaries:

| Maximum mesh step | Cells | Resonance | Minimum S11 | Input impedance | VSWR | Efficiency | Gain |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 mm | 75,348 | 1.993 GHz | -0.89 dB | 60.74 + j231.00 Ω | 19.58 | 0.00226 | -24.61 dBi |
| 2 mm | 367,500 | 2.245 GHz | -11.98 dB | 30.73 - j6.63 Ω | 1.67 | 0.00233 | -25.28 dBi |
| 1 mm | 2,322,540 | 2.035 GHz | -1.68 dB | 284.46 + j255.65 Ω | 10.36 | 0.00032 | -29.58 dBi |

Both adjacent comparisons fail the adapter limits (at most 1 dB S11 delta
and 2% resonance delta). The 4/2 mm resonance changes by about 12.7% and S11
by 11.1 dB; the 2/1 mm pair changes by about 9.4% and 10.3 dB. These are
failed exploratory runs of an assumed model, not accepted antenna performance.
The 1 mm mesh used about 2.32 million cells; this cost is why the five-case
adapter remains opt-in rather than part of the quick integration run. All five
scenario paths were exercised separately: free-space, PCB, enclosure, and
animal-proximity runs reached the solver energy criterion but failed mesh
convergence; the battery surrogate hit the timestep limit before that
criterion, so the fine mesh did not start. No scenario produced accepted RF
metrics. Detailed solver output was kept in temporary run directories, not
treated as release artifacts. The summarized evidence and candidate/spec
hashes are in `results/mvp2/antenna/openems_candidate_pilot.json`. Treat the
entire pilot as historical: it predates both the corrected TLL-5902 package
envelope and the updated assumed enclosure depth in `candidate_model.json`.
Its solver results are not evidence for the current candidate. The integrated
run did simulate the updated assumptions, but it did not establish converged
RF performance for four of the five scenarios. Rerun after the physical layout
and material assumptions are reviewed.

Set `RIOSE_OPENEMS_ADAPTER=module.name` to load a Python module exposing
`simulate(spec=..., scenario=..., output_dir=...)`. It may report `COMPLETED`
only after a real openEMS run and must return solver-derived `metrics`. Preserve
raw S-parameter/far-field outputs and provenance. The animal case is an
experimental material approximation, not tissue validation. GPU/Sionna RT is
separate and optional; inspect its machine-readable report with
`python -m hardware.antenna.capabilities` or call `detect_capabilities()` from
`hardware.antenna.capabilities`.

The Sionna RT capability/scenario manifest is generated separately:

```sh
python -m hardware.antenna.sionna_experiment \
  --spec hardware/spec.yaml --output results/mvp2/antenna/sionna
```

It records the 10 m, obstacle, and orientation-variant scenarios. CPU-only or
missing-Sionna environments mark them `SKIPPED_OPTIONAL` with null metrics. If
CUDA and Sionna are present, a deployment may provide `RIOSE_SIONNA_ADAPTER`
implementing `simulate(scenario, spec_path, output_dir)`; adapter failures are
recorded as blocked optional experiments and never alter the core gate.
