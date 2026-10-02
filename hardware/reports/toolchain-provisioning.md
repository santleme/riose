# MVP 2 toolchain provisioning log

Date: 2026-10-02 (America/Sao_Paulo)
Host: WSL2, all provisioning targets remain inside the Linux filesystem.

## Initial WSL audit

- Distribution: Ubuntu 24.04.4 LTS (Noble Numbat)
- Kernel: `6.6.87.2-microsoft-standard-WSL2`
- Architecture: `x86_64`
- System Python: `Python 3.12.3`; user-local `python3.12`: `Python 3.12.13`
- Existing `uv` on PATH: `0.11.7`
- Project `uv run` environment initially selected Python `3.13.13`, while the CI/toolchain target is Python 3.12; this needs reconciliation at project-environment level.
- CMake: `3.28.3`
- GCC/G++: `13.3.0`
- Ninja: not installed at audit time
- GPU: `/dev/dxg` exists, but `nvidia-smi`, CUDA tools/libraries, and PyTorch were not available to user space; no usable CUDA GPU detected by MVP2 preflight.
- Root filesystem: 1007 GB total, 886 GB available at audit time.
- Memory: 23 GiB total, about 22 GiB available; swap 8 GiB.
- Initial package/tool checks: Renode, `renode-test`, ngspice, openEMS, CadQuery, CSXCAD, Zephyr workspace and Zephyr SDK were not installed/configured.
- Ubuntu Noble package candidates observed before provisioning: Ninja 1.11.1 and ngspice 42; no openEMS package candidate. These are recorded as distro candidates, not as installed versions.

## Provisioning actions and validation

This section is updated as installations and tool smoke tests complete. A tool is called provisioned only after its actual executable/library version and a project-relevant headless check have been recorded. Optional GPU/Sionna remains non-blocking when no usable GPU is exposed to WSL.

### Provisioned WSL toolchain

All installations are user-local. No Windows-side package was installed, no system Python was replaced, and the project `.venv` is now explicitly pinned to Python 3.12 via `.python-version` (`uv sync --extra dev` creates it from the existing `uv.lock`). The system Python remains 3.12.3.

| Component | Provisioned version / path | Validation |
|---|---|---|
| Project Python | 3.12.13 in `.venv`; separate integration environment 3.12.14 at `~/.local/opt/riose-mvp2-venv` | `uv run python --version`; MVP2 preflight reports 3.12.14 when activated |
| uv / Ninja | uv 0.11.7; Ninja 1.13.2, under `~/.local` | Version probe matches `hardware/toolchain.json` |
| Zephyr / west | Zephyr 4.2.1 workspace at `~/zephyrproject/zephyr`; west 1.5.0 in `~/zephyrproject/.venv` | Workspace update clean; native_sim build+cycle passes; NUCLEO-L031K6 builds |
| Zephyr SDK | 0.16.8 at `~/.local/opt/zephyr-sdk-0.16.8`; ARM GCC 12.2.0 | Zephyr board build emits ELF/BIN (29,648-byte BIN; 90.48% flash, 2,976/8,192-byte RAM) |
| Renode / Robot | Renode 1.17.0 at `~/.local/opt/renode_1.17.0-portable`; isolated Robot environment at `~/.local/opt/renode-test-venv` | `renode-test` platform + custom SX1262/LIS2DW12 peripheral checks pass, and a second run loads/runs the built Zephyr ELF |
| ngspice | 47 at `~/.local/opt/ngspice-47` (`~/.local/bin/ngspice`) | Four trace-derived scenario netlists execute; rail/current CSV and summaries are written |
| CadQuery | 2.8.0 in micromamba env `~/.local/share/mamba/envs/riose-mvp2-cad` | Real headless STEP and STL exports; changing enclosure width changes both geometry hashes |
| openEMS / CSXCAD | openEMS 0.37.0-rc3 and CSXCAD 0.7.0-rc3 at `~/.local/opt/openEMS-0.37.0-rc3`; Python bindings in the isolated MVP2 venv | Version/import probe passes; a 100-cell headless FDTD smoke executes and writes field outputs |
| Sionna / CUDA | Not installed / not exposed to WSL | Preflight reports GPU and CUDA unavailable, Sionna skipped as optional |

### Activation and command entry points

From the repository root, activate the user-local environments before running the full provisioned path:

```sh
source hardware/activate-mvp2-toolchain.sh
python --version
python -m riose.products.ear_tag.digital_twin preflight
python -m riose.products.ear_tag.digital_twin validate-spec --spec hardware/spec.yaml
python -m riose.products.ear_tag.digital_twin run --spec hardware/spec.yaml --output results/mvp2 --seed 7
```

The activation script sources `~/.local/opt/riose-zephyr-env.sh`, activates the isolated MVP2 Python environment, sets library/executable paths, and adds this checkout's `src/` and root to `PYTHONPATH`. It only activates existing installations; it does not install packages or edit shell startup files. The per-project Python selector is `.python-version` (`3.12`), and locked dependencies remain in `uv.lock`.

Provisioning was performed without `sudo`: non-interactive sudo was unavailable in this WSL session. Python/CAD packages were installed in micromamba/venv environments; Renode was installed from its portable release; ngspice was built from its upstream 47 source; openEMS and its pinned CSXCAD submodule were built with the upstream headless build scripts into the user prefix. Build dependencies were supplied by the isolated micromamba environment and user-local build tools. These installations consume WSL disk space only. No package was installed through Windows.

### Results and limits recorded in the integrated run

- Python suite: **145 passed** under project Python 3.12.13, including the hardware Python test modules. C/CMake suite: **27/27 passed** in the integrated run. One existing Starlette/httpx deprecation warning remains; `git diff --check` is clean.
- The integrated command builds the Zephyr NUCLEO-L031K6 image and Renode loads it with both custom logical peripherals; those stages report `PASSED` in `results/mvp2/summary.json`.
- The four NORMAL/ACTIVE/ALERT/WORST_REASONABLE_CASE traces are converted to ngspice inputs and executed. Their modeled currents and rails retain ASSUMED/SIMULATED provenance; none are measurements.
- CadQuery emits STEP/STL and an envelope report; a +5 mm enclosure-width change changed both STEP and STL hashes. The assumed mechanical candidate now uses the official TLL-5902 body envelope (Ø14.5 × 25.2 mm), a 38 × 68 × 18.5 mm enclosure, separate PCB/battery bays, 0.5 mm modeled clearance, and an offset mounting hole. Its bounding-box fit is **COMPLETED** with no modeled collisions. Mass and fit remain assumed geometry estimates; terminals, tolerances, sealing, retention, and physical fit are not validated.
- The integrated OpenEMS run used the updated candidate and a 128 MiB estimated-memory preflight before each solver allocation. The free-space and PCB cases reached the energy stop but failed the declared mesh-convergence comparison; the battery case hit the 160,000-step limit; the animal approximation failed to bracket resonance; only the enclosure case passed the two-mesh numerical comparison. These are simulations of assumed geometry, not accepted physical RF performance. A 1 mm exploratory mesh from the historical pilot is rejected by the default memory preflight before `fdtd.Run()`.
- The Zephyr image configures the NUCLEO-L031K6 STM32 IWDG with a 10 s timeout and captures reset-cause flags; the target build passed, but no physical watchdog reset was triggered. `unexpected_reboot` remains unclassified without a persistent expected-reset contract.
- The Renode CPU remains the upstream STM32L071 surrogate for the STM32L031 target, not an exact MCU model. SX1262/LIS2DW12 are logical approximations; no analog/RF/physical sleep behavior is claimed.
- GPU/CUDA/Sionna are unavailable and explicitly optional. No physical hardware, animal, or lab measurement was used.

### Additional validation commands

```sh
uv sync --extra dev
uv run pytest -q
cmake -S hardware/tests -B /tmp/riose-mvp2-ctest -G Ninja
cmake --build /tmp/riose-mvp2-ctest
ctest --test-dir /tmp/riose-mvp2-ctest --output-on-failure
source hardware/activate-mvp2-toolchain.sh
python -m riose.digital_twin preflight
RIOSE_ZEPHYR_ELF=/tmp/riose-tag-nucleo/zephyr/zephyr.elf \
  renode-test -r /tmp/riose-renode-smoke hardware/renode/tests/platform-smoke.robot
```

The core CI workflow intentionally uses the project Python/C toolchain without requiring optional external solvers, a GPU, or hardware; preflight and the integrated fail-closed gate expose their absence. In this provisioned WSL, the explicit activation script enables the real Zephyr, Renode, CadQuery, ngspice and openEMS paths.
