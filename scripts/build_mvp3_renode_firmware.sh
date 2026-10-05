#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="${TMPDIR:-/tmp}/riose-mvp3-renode-build"
renode_conf="${TMPDIR:-/tmp}/riose-mvp3-renode.conf"

if [[ -n "${RIOSE_ZEPHYR_ELF:-}" && -f "$RIOSE_ZEPHYR_ELF" ]]; then
  printf '%s\n' "$RIOSE_ZEPHYR_ELF"
  exit 0
fi

# This board profile needs enough Zephyr stack/heap for the existing app when
# it is driven under Renode. Keep generated configuration/build output in /tmp.
set +u
source "$repo_root/hardware/activate-mvp2-toolchain.sh"
set -u
cat >"$renode_conf" <<'EOF'
CONFIG_MAIN_STACK_SIZE=2048
CONFIG_HEAP_MEM_POOL_SIZE=1024
EOF
west build -b nucleo_l031k6 -d "$build_dir" "$repo_root/hardware/firmware/zephyr" \
  -- -DEXTRA_CONF_FILE="$renode_conf" >&2
elf="$build_dir/zephyr/zephyr.elf"
[[ -f "$elf" ]] || { echo "Zephyr build completed without ELF: $elf" >&2; exit 2; }
printf '%s\n' "$elf"
