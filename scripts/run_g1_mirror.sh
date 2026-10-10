#!/usr/bin/env bash
# Live MuJoCo mirror of the G1: solid = measured (rt/lowstate), ghost = target (rt/lowcmd).
# Runs g1_mujoco_mirror.py in the unitree/env venv with the project's Cyclone DDS lib.
# All args pass through, e.g.:  scripts/run_g1_mirror.sh --demo
set -euo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"
G1_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="/home/aloha/robotics/platforms/unitree/env/bin/python3"

exec env LD_LIBRARY_PATH="$G1_ROOT/.cyclonedds-home/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$VENV_PY" "$SCRIPT_DIR/g1_mujoco_mirror.py" "$@"
