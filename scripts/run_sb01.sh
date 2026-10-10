#!/usr/bin/env bash
# Launcher for the sb01 conversation loop.
# Runs sb01_conversation.py inside the `unitree/env310` venv (Python 3.10 +
# the official cyclonedds 0.10.2 wheel, which bundles its own C library).
# The old `unitree/env` (Python 3.12, cyclonedds built from source against
# .cyclonedds-home) can't create DDS topics: DDS_RETCODE_PRECONDITION_NOT_MET.
# Any extra args are passed through to the script.
#
#   sb01                  demo mode (visitor persona + arm gestures)
#   sb01 --no-gestures    demo mode, arms stay still (fallback)
#   sb01 --class          classroom assistant (original behavior, no gestures)
set -euo pipefail

# Resolve through symlinks so this works when called via a ~/.local/bin/sb01 link.
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"
G1_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VENV_PY="/home/aloha/robotics/platforms/unitree/env310/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "error: venv python not found at $VENV_PY" >&2
    exit 1
fi

# Demo mode is the default; --class switches back to the classroom assistant.
ARGS=()
DEMO=1
for a in "$@"; do
    if [[ "$a" == "--class" ]]; then DEMO=0; else ARGS+=("$a"); fi
done
[[ $DEMO -eq 1 ]] && ARGS=(--demo "${ARGS[@]}")

cd "$SCRIPT_DIR"
exec env -u LD_LIBRARY_PATH "$VENV_PY" sb01_conversation.py "${ARGS[@]}"
