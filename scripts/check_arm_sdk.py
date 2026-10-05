#!/usr/bin/env python3
"""
check_arm_sdk.py  -  prove the gesture client's command path on the real SDK,
without moving the robot

Not a simulation: it uses the installed unitree_sdk2py, its native CRC library
and a real DDS publisher. It builds one rt/arm_sdk command with exactly the code
the gestures use, computes its CRC, and (with --publish) sends it once with the
blend weight at 0, which leaves the arms with the robot's own controller.

Checks:
  1. the SDK's CRC library loads on this computer
  2. the CRC of the command matches the SDK's reference implementation
  3. DDS accepts the command and delivers it intact
  4. with a robot: rt/lowstate is arriving, and what FSM id, mode_pr and
     mode_machine it reports (the values the gesture client gates on)
  5. with --publish and a robot: the arms did not move

Usage:
  python3 scripts/check_arm_sdk.py [network_interface]             # build + CRC only
  python3 scripts/check_arm_sdk.py [network_interface] --publish   # also send one weight-0 command
  python3 scripts/check_arm_sdk.py lo --publish --no-robot         # no robot attached (developer machine)
"""

import argparse
import os
import sys
import time

import numpy as np

from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from teleop.gesture_client import ARM_JOINTS, FSM_ALLOWED, WAIST_JOINTS, WEIGHT_JOINT, GestureClient

MOVE_TOLERANCE_RAD = 0.02   # arm drift allowed while standing still for a second

failures = []


def check(ok: bool, text: str):
    print(f"  [{'ok' if ok else 'FAIL'}] {text}")
    if not ok:
        failures.append(text)
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("interface", nargs="?", default="eno0")
    parser.add_argument("--publish", action="store_true",
                        help="send one command with blend weight 0 (does not move the robot)")
    parser.add_argument("--no-robot", action="store_true",
                        help="no robot attached: skip the rt/lowstate and FSM checks")
    args = parser.parse_args()

    ChannelFactoryInitialize(0, args.interface)

    print("1. SDK and CRC library")
    try:
        client = GestureClient("http://127.0.0.1:9")   # no sidecar needed; never contacted
    except OSError as exc:
        check(False, f"CRC library did not load: {exc}")
        return 1
    check(True, "unitree_sdk2py CRC library loaded")

    received = []
    listener = ChannelSubscriber("rt/arm_sdk", LowCmd_)
    listener.Init(received.append, 10)

    q = np.zeros(29)
    mode_pr = mode_machine = 0
    if not args.no_robot:
        print("2. Robot state")
        deadline = time.monotonic() + 3.0
        while client._state is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not check(client._state is not None, "rt/lowstate is arriving"):
            client.close()
            return 1
        q, mode_pr, mode_machine, _ = client._fresh_state()
        fsm = client._fsm_id()
        print(f"      FSM id = {fsm}   mode_pr = {mode_pr}   mode_machine = {mode_machine}")
        print(f"      waist (rad) = {np.round(q[12:15], 3).tolist()}")
        print(f"      arms  (rad) = {np.round(q[15:29], 3).tolist()}")
        check(fsm is not None, "FSM id can be read")
        if fsm in FSM_ALLOWED:
            print(f"      gestures WOULD run in this state (allowed FSM ids: {FSM_ALLOWED})")
        else:
            print(f"      gestures would be REFUSED in this state (allowed FSM ids: {FSM_ALLOWED})")

    print("3. One command, built by the gesture client's own code")
    client._waist = q[12:15].copy()
    frame = np.zeros(15, dtype=np.float32)
    frame[:14] = q[15:29]      # arms exactly where they are
    frame[14] = 0.0            # blend weight 0: the robot's own controller keeps the arms

    # Build without sending: point the client at a publisher that only records.
    built = []
    real_pub = client._pub
    client._pub = type("Recorder", (), {"Write": lambda self, cmd: built.append(cmd) or True})()
    client._write(frame, mode_pr, mode_machine)
    client._pub = real_pub
    cmd = built[0]
    check(cmd.motor_cmd[WEIGHT_JOINT].q == 0.0, "blend weight in the command is 0")
    check(all(abs(cmd.motor_cmd[j].q - q[j]) < 1e-6 for j in ARM_JOINTS + WAIST_JOINTS),
          "arm and waist targets equal the measured positions")
    check(cmd.mode_pr == mode_pr and cmd.mode_machine == mode_machine,
          "mode_pr and mode_machine copied from rt/lowstate")
    crc = cmd.crc
    check(isinstance(crc, int) and crc != 0, f"CRC computed: 0x{crc:08x}")
    try:
        packed = client._crc._CRC__PackHGLowCmd(cmd)
        reference = client._crc._crc_py(packed)
        check(reference == crc, f"native CRC matches the SDK's reference implementation (0x{reference:08x})")
    except AttributeError:
        print("  [skip] this SDK version has no reference CRC to compare against")
    cmd.motor_cmd[WEIGHT_JOINT].q = 1.0
    changed = client._crc.Crc(cmd)
    cmd.motor_cmd[WEIGHT_JOINT].q = 0.0
    check(changed != crc, "CRC changes when the command changes")

    if not args.publish:
        print("\nNothing was published. Re-run with --publish to send this one weight-0 command.")
    else:
        print("4. Publish it once on rt/arm_sdk")
        before = None if args.no_robot else client._fresh_state()[0][15:29].copy()
        ok = client._write(frame, mode_pr, mode_machine)
        check(ok, "DDS Write() returned True")
        deadline = time.monotonic() + 2.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.02)
        if check(bool(received), "the command came back from DDS"):
            echo = received[-1]
            check(echo.crc == crc, "received CRC equals the one computed")
            check(client._crc.Crc(echo) == echo.crc, "received command passes its own CRC check")
            check(echo.motor_cmd[WEIGHT_JOINT].q == 0.0, "received blend weight is 0")
        if before is not None:
            time.sleep(1.0)
            after = client._fresh_state()[0][15:29]
            moved = float(np.abs(after - before).max())
            check(moved < MOVE_TOLERANCE_RAD, f"arms did not move (largest change {moved:.4f} rad)")

    listener.Close()
    client.close()
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
