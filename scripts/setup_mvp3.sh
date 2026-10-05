#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ros_setup="/opt/ros/jazzy/setup.bash"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to create the repository-local Python environment." >&2
  exit 2
fi
uv sync --project "$repo_root" --extra dev

if [[ -f "$ros_setup" ]]; then
  # ROS Jazzy provides the already-installed Gazebo Harmonic vendor runtime in
  # this WSL image. Source it for this command; do not install system packages.
  # shellcheck disable=SC1090
  set +u
  source "$ros_setup"
  set -u
fi

if ! command -v gz >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Gazebo Harmonic was not found after loading /opt/ros/jazzy/setup.bash.
On supported Ubuntu 22.04/24.04 systems, follow the official binary
installation guide: https://gazebosim.org/docs/harmonic/install_ubuntu/
The repository setup script does not add system apt sources or install packages.
EOF
  exit 2
fi

version="$(gz sim --version | sed -n 's/.*version \([0-9][0-9]*\)\.\([0-9][0-9]*\)\.\([0-9][0-9]*\).*/\1.\2.\3/p')"
if [[ -z "$version" || "${version%%.*}" != "8" ]]; then
  echo "Expected Gazebo Sim 8 (Harmonic); detected: ${version:-unknown}" >&2
  exit 2
fi

echo "Gazebo Harmonic available: $(gz sim --version | head -1)"
echo "ROS setup: ${ros_setup} (sourced when present)"
echo "Repository: ${repo_root}"
