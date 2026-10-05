#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$repo_root/scripts/setup_mvp3.sh"
# Generated visuals are reproducible from repository sources. CadQuery is
# already part of the project's MVP2 toolchain; no editor step is required.
set +u
source "$repo_root/hardware/activate-mvp2-toolchain.sh"
set -u
python "$repo_root/scripts/build_mvp3_visual_assets.py"
