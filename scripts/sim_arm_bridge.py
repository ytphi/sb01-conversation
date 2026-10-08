#!/usr/bin/env python3
"""
sim_arm_bridge.py  -  Stand-in for the G1's arm controller, for the MuJoCo simulator only.

On the real G1, gesture code publishes rt/arm_sdk and the robot's own motion
controller mixes those arm and waist targets into its balance control. Unitree's
simulator (unitree_mujoco) has no such controller: it only obeys rt/lowcmd.
This bridge fills the gap:

  rt/arm_sdk  (your script) ---+
                               +--> bridge --> rt/lowcmd --> simulator
  rt/lowstate (simulator) -----+

  - legs: held at the zero pose (the simulator's elastic band carries the weight)
  - waist + arms (indexes 12-28): blended between that hold pose and the arm_sdk
    target by the weight in motor_cmd[29].q (0 = robot's pose, 1 = your target)
  - no arm_sdk message for STALE_SEC: the weight eases back to 0
  - warns when an arm_sdk target is outside the model's joint limits

It only ever talks on domain 1 over lo, the simulator's network, so it can't reach
a real robot. The blending is an approximation of Unitree's controller, not a copy:
a gesture that looks right here still has to pass the robot safety rules.

Usage (start the simulator first):
  python3 scripts/sim_arm_bridge.py [--model PATH]
"""

import os
import sys
import time
import argparse
import threading
import xml.etree.ElementTree as ET

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_

# ── config ───────────────────────────────────────────────────────────────────
SIM_DOMAIN     = 1      # unitree_mujoco's domain; the real robot uses 0
SIM_INTERFACE  = "lo"
DEFAULT_MODEL  = os.path.expanduser("~/Robotics/unitree_mujoco/unitree_robots/g1/g1_29dof.xml")

NUM_MOTOR      = 29
WEIGHT_INDEX   = 29     # motor_cmd[29].q carries the arm_sdk blend weight
BLEND_JOINTS   = range(12, 29)   # waist 12-14, left arm 15-21, right arm 22-28
CONTROL_DT     = 0.005  # matches the simulator's physics step
STALE_SEC      = 0.5    # arm_sdk silence before the weight starts easing back
RELEASE_SEC    = 1.0    # time to ease the weight from 1 back to 0
STATUS_SEC     = 2.0
LIMIT_WARN_SEC = 1.0    # at most one limit warning per joint per this many seconds

# hold gains, from unitree_sdk2_python/example/g1/low_level/g1_low_level_example.py
HOLD_KP = [60, 60, 60, 100, 40, 40,   60, 60, 60, 100, 40, 40,   60, 40, 40] + [40] * 14
HOLD_KD = [1, 1, 1, 2, 1, 1,          1, 1, 1, 2, 1, 1,          1, 1, 1] + [1] * 14


def load_joint_limits(model_path: str):
    """Returns [(name, lo, hi)] in motor order, read from the MJCF actuator list."""
    root = ET.parse(model_path).getroot()
    ranges = {j.get("name"): j.get("range") for j in root.iter("joint") if j.get("range")}
    limits = []
    for motor in root.find("actuator").iter("motor"):
        joint = motor.get("joint")
        lo, hi = (float(v) for v in ranges[joint].split())
        limits.append((joint.removesuffix("_joint"), lo, hi))
    if len(limits) != NUM_MOTOR:
        raise ValueError(f"{model_path}: expected {NUM_MOTOR} motors, found {len(limits)}")
    return limits


# ── bridge ───────────────────────────────────────────────────────────────────
class ArmBridge:
    def __init__(self, limits):
        self.limits = limits
        self.lock = threading.Lock()
        self.arm_cmd = None        # latest rt/arm_sdk LowCmd_
        self.arm_time = 0.0        # when it arrived
        self.arm_count = 0
        self.state_count = 0
        self.mode_machine = 0
        self.weight = 0.0          # weight actually applied (eases back when stale)
        self.last_warn = {}
        self.low_cmd = unitree_hg_msg_dds__LowCmd_()

    def on_arm_sdk(self, msg: LowCmd_):
        with self.lock:
            self.arm_cmd = msg
            self.arm_time = time.monotonic()
            self.arm_count += 1

    def on_lowstate(self, msg: LowState_):
        with self.lock:
            self.mode_machine = msg.mode_machine
            self.state_count += 1

    def warn_limits(self, cmd: LowCmd_, now: float):
        for i in BLEND_JOINTS:
            name, lo, hi = self.limits[i]
            q = cmd.motor_cmd[i].q
            if not lo <= q <= hi and now - self.last_warn.get(i, 0.0) >= LIMIT_WARN_SEC:
                self.last_warn[i] = now
                print(f"  LIMIT: joint {i} {name} target {q:+.3f} rad is outside [{lo:+.3f}, {hi:+.3f}]")

    def step(self) -> LowCmd_:
        now = time.monotonic()
        with self.lock:
            arm, fresh, mode_machine = self.arm_cmd, now - self.arm_time < STALE_SEC, self.mode_machine

        if arm is not None and fresh:
            self.weight = min(max(arm.motor_cmd[WEIGHT_INDEX].q, 0.0), 1.0)
            self.warn_limits(arm, now)
        else:
            self.weight = max(0.0, self.weight - CONTROL_DT / RELEASE_SEC)

        cmd = self.low_cmd
        cmd.mode_pr = 0
        cmd.mode_machine = mode_machine
        for i in range(NUM_MOTOR):
            m = cmd.motor_cmd[i]
            m.mode = 1
            m.q, m.dq, m.tau, m.kp, m.kd = 0.0, 0.0, 0.0, HOLD_KP[i], HOLD_KD[i]
            if arm is not None and i in BLEND_JOINTS and self.weight > 0.0:
                a, w = arm.motor_cmd[i], self.weight
                m.q   = w * a.q
                m.dq  = w * a.dq
                m.tau = w * a.tau
                m.kp  = (1 - w) * HOLD_KP[i] + w * a.kp
                m.kd  = (1 - w) * HOLD_KD[i] + w * a.kd
        return cmd

    def status(self) -> str:
        with self.lock:
            age = time.monotonic() - self.arm_time if self.arm_cmd is not None else None
            arm_count, state_count = self.arm_count, self.state_count
        arm = "none yet" if age is None else ("fresh" if age < STALE_SEC else f"stale {age:.1f}s")
        return (f"lowstate msgs={state_count:<6d} arm_sdk msgs={arm_count:<6d} "
                f"arm_sdk={arm:<12s} weight={self.weight:.2f}")


# ── main ─────────────────────────────────────────────────────────────────────
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="arm_sdk -> lowcmd bridge for the MuJoCo simulator (domain 1, lo).")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"G1 MJCF file for joint limits (default {DEFAULT_MODEL})")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    limits = load_joint_limits(args.model)

    print(f"=== sim_arm_bridge (SIM only: domain {SIM_DOMAIN} on {SIM_INTERFACE}) ===")
    ChannelFactoryInitialize(SIM_DOMAIN, SIM_INTERFACE)
    bridge = ArmBridge(limits)

    lowcmd_pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    lowcmd_pub.Init()
    ChannelSubscriber("rt/arm_sdk", LowCmd_).Init(bridge.on_arm_sdk, 10)
    ChannelSubscriber("rt/lowstate", LowState_).Init(bridge.on_lowstate, 10)

    print("Waiting for the simulator's rt/lowstate...")
    try:
        while bridge.state_count == 0:
            time.sleep(0.1)
        print("Simulator found. Holding the zero pose; arms follow rt/arm_sdk. Ctrl+C to stop.")

        next_tick = time.perf_counter()
        next_status = time.monotonic() + STATUS_SEC
        while True:
            lowcmd_pub.Write(bridge.step())
            if time.monotonic() >= next_status:
                print(bridge.status())
                next_status += STATUS_SEC
            next_tick += CONTROL_DT
            time.sleep(max(0.0, next_tick - time.perf_counter()))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
