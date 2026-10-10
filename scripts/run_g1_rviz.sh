#!/usr/bin/env bash
# Show the G1 live in RViz2.
#   scripts/run_g1_rviz.sh            # real robot on eno0
#   scripts/run_g1_rviz.sh wlan0      # other interface
#   scripts/run_g1_rviz.sh --demo     # no robot, fake arm motion
# Starts: g1_rviz_bridge (rt/lowstate -> /joint_states + TF), robot_state_publisher, rviz2.
set -eo pipefail   # no -u: ROS setup.bash references unset vars

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"
G1_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="/home/aloha/robotics/platforms/unitree/env/bin/python3"
URDF="$G1_ROOT/rviz/g1_29dof.urdf"
RVIZ_CFG="$G1_ROOT/rviz/g1.rviz"

source /opt/ros/jazzy/setup.bash
export LD_LIBRARY_PATH="$G1_ROOT/.cyclonedds-home/lib:$LD_LIBRARY_PATH"

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

/opt/ros/jazzy/lib/robot_state_publisher/robot_state_publisher \
    --ros-args -p robot_description:="$(cat "$URDF")" &
pids+=($!)

"$VENV_PY" "$SCRIPT_DIR/g1_rviz_bridge.py" "${@:-eno0}" &
pids+=($!)

rviz2 -d "$RVIZ_CFG"
