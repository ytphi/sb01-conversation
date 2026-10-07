#!/usr/bin/env bash
# test_hello_g1.sh  -  run the hello_g1 test step by step and save everything to one log.
#
# On the VM (no robot):       bash scripts/test_hello_g1.sh             # offline checks only
# At the lab (robot wired):   bash scripts/test_hello_g1.sh <interface> # e.g. eno0
#
# Optional: READ_SECONDS=20 for a longer state read, PYTHON=/path/to/python to pick the interpreter.
# Never moves the robot: the only robot step is hello_g1.py (speech, chest LED, read-only rt/lowstate).

set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

IFACE="${1:-}"
READ_SECONDS="${READ_SECONDS:-5}"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
PY="${PYTHON:-$PY}"
export PYTHONUNBUFFERED=1   # show hello_g1 output live through tee

mkdir -p logs
LOG="logs/hello_g1_$(date +%Y%m%d_%H%M%S).txt"
exec > >(tee -a "$LOG") 2>&1

PASS=0
FAIL=0
step()   { echo; echo "== $1"; }
result() {
    if [ "$1" -eq 0 ]; then echo "  -> PASS"; PASS=$((PASS + 1))
    else echo "  -> FAIL (exit $1)"; FAIL=$((FAIL + 1)); fi
}
ask() {
    local answer
    read -r -p "$1 " answer
    echo "  answer: ${answer:-<none>}"
}
summary() {
    echo
    echo "== Summary: $PASS passed, $FAIL failed"
    echo "Log saved to: $REPO/$LOG"
    echo "Bring this file back to the VM session."
}

step "1. Environment"
echo "date:      $(date '+%Y-%m-%d %H:%M:%S')"
echo "host:      $(hostname)"
echo "branch:    $(git branch --show-current 2>/dev/null)"
echo "commit:    $(git log -1 --oneline 2>/dev/null)"
if [ -n "$(git status --porcelain scripts/hello_g1.py 2>/dev/null)" ]; then
    echo "WARNING:   scripts/hello_g1.py has uncommitted changes; this run won't match the commit above."
fi
echo "python:    $PY ($($PY --version 2>&1))"
echo "interface: ${IFACE:-<none: offline checks only>}"

step "2. Unitree SDK imports"
$PY -c "
from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
print('  unitree_sdk2py imports OK')
"
result $?

step "3. Syntax check"
$PY -m py_compile scripts/hello_g1.py && echo "  scripts/hello_g1.py compiles"
result $?

step "4. Fake mode (no robot)"
$PY scripts/hello_g1.py --fake --seconds 3
result $?

if [ -z "$IFACE" ]; then
    echo
    echo "No interface given, so the robot steps were skipped."
    echo "At the lab: bash scripts/test_hello_g1.sh <interface>   (e.g. eno0)"
    summary
    exit $(( FAIL > 0 ))
fi

step "5. Network interface $IFACE"
if ! ip -br -4 addr show dev "$IFACE" 2>/dev/null | grep -q .; then
    echo "  '$IFACE' not found (or has no IPv4 address). Interfaces on the robot network:"
    ip -br -4 addr | grep "192\.168\.123\." || echo "  (none with a 192.168.123.x address)"
    result 1
    summary
    exit 1
fi
ip -br -4 addr show dev "$IFACE"
if ! ip -br -4 addr show dev "$IFACE" | grep -q "192\.168\.123\."; then
    echo "  WARNING: $IFACE has no 192.168.123.x address; the G1 network normally uses that range."
fi
result 0

step "6. Other robot programs"
OTHERS="$(pgrep -af 'sb01_conversation|gesture_server|gesture_client|teleop_session' || true)"
if [ -n "$OTHERS" ]; then
    echo "$OTHERS"
    echo "  WARNING: these use the robot too. Stop them first (one controller at a time)."
else
    echo "  none running"
fi

step "7. Before running on the robot"
echo "  - The robot is in the lab's normal safe resting state, and lab rules are followed."
echo "  - Nothing else is controlling the robot (step 6)."
echo "  - You are watching the chest LED and listening for the greeting."
read -r -p "Type yes to run hello_g1 on the robot: " OK
if [ "$OK" != "yes" ]; then
    echo "  Stopped before running on the robot."
    summary
    exit 1
fi

step "8. hello_g1 on the robot ($IFACE, ${READ_SECONDS}s of state)"
$PY scripts/hello_g1.py "$IFACE" --seconds "$READ_SECONDS"
result $?

step "9. What you saw and heard"
ask "Did you hear the greeting? Was it clear English? (describe):"
ask "Did the chest LED go red, green, blue, then off? (describe):"
ask "Did the robot stay still the whole time? (yes/no):"
ask "Any other notes:"

summary
exit $(( FAIL > 0 ))
