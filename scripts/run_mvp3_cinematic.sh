#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RIOSE_ZEPHYR_ELF="$("$repo_root/scripts/build_mvp3_renode_firmware.sh")"
exec uv run --project "$repo_root" riose mvp3 cinematic "$@"
