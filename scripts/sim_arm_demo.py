#!/usr/bin/env python3
"""
sim_arm_demo.py  -  First arm routine, for the MuJoCo simulator only: arms out, wave, back.

Publishes rt/arm_sdk the way Unitree's example/g1/high_level/g1_arm7_sdk_dds_example.py
does (blend weight in motor_cmd[29].q), so sim_arm_bridge.py has to be running.
Hard-wired to domain 1 on lo, so it can't reach a real robot. A robot version
needs the SB01_Workflow.md section 4 checklist first.

  1. take over:  weight 0 -> 1 while the arms go from where they are to zero
  2. arms out:   shoulders roll out
  3. wave:       right elbow swings
  4. return:     back to zero, then weight 1 -> 0

Every target is clamped to the joint limits minus a margin before it is sent.

Usage (simulator and sim_arm_bridge.py running):
  python3 scripts/sim_arm_demo.py
"""

import sys
import math
import time
import threading

import numpy as np

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_

from sim_arm_bridge import SIM_DOMAIN, SIM_INTERFACE, DEFAULT_MODEL, WEIGHT_INDEX, load_joint_limits

# ── config ───────────────────────────────────────────────────────────────────
CONTROL_DT   = 0.02    # 50 Hz, like Unitree's arm example
KP, KD       = 60.0, 1.5
LIMIT_MARGIN = 0.05    # rad kept clear of each joint limit

# arm7 layout plus the waist, the joints arm_sdk controls
L_SH_PITCH, L_SH_ROLL, L_SH_YAW, L_ELBOW = 15, 16, 17, 18
R_SH_PITCH, R_SH_ROLL, R_SH_YAW, R_ELBOW = 22, 23, 24, 25
ARM_JOINTS = list(range(12, 29))

ARMS_OUT = {L_SH_ROLL: +1.2, R_SH_ROLL: -1.2, L_ELBOW: 0.5, R_ELBOW: 0.5}
WAVE_JOINT, WAVE_CENTER, WAVE_AMPLITUDE, WAVE_HZ = R_ELBOW, 0.8, 0.4, 1.0

# (stage name, seconds)
STAGES = [("take over", 2.0), ("arms out", 2.0), ("wave", 3.0), ("return", 2.0), ("release", 1.0)]


def smooth(x: float) -> float:
    """0 -> 1 with zero velocity at both ends."""
    x = min(max(x, 0.0), 1.0)
    return 0.5 - 0.5 * math.cos(math.pi * x)


def pose(overrides: dict) -> np.ndarray:
    q = np.zeros(29)
    for i, v in overrides.items():
        q[i] = v
    return q


class Demo:
    def __init__(self, limits):
        self.lo = np.array([lo for _, lo, _ in limits]) + LIMIT_MARGIN
        self.hi = np.array([hi for _, _, hi in limits]) - LIMIT_MARGIN
        self.lock = threading.Lock()
        self.state = None
        self.cmd = unitree_hg_msg_dds__LowCmd_()

    def on_lowstate(self, msg: LowState_):
        with self.lock:
            self.state = msg

    def current_q(self) -> np.ndarray:
        with self.lock:
            return np.array([self.state.motor_state[i].q for i in range(29)])

    def build(self, q: np.ndarray, weight: float) -> LowCmd_:
        q = np.clip(q, self.lo, self.hi)
        self.cmd.motor_cmd[WEIGHT_INDEX].q = weight
        for i in ARM_JOINTS:
            m = self.cmd.motor_cmd[i]
            m.q, m.dq, m.tau, m.kp, m.kd = float(q[i]), 0.0, 0.0, KP, KD
        return self.cmd

    def target(self, stage: str, t: float, duration: float, start_q: np.ndarray):
        """Returns (q, weight) for time t into a stage."""
        s = smooth(t / duration)
        zero, out = pose({}), pose(ARMS_OUT)
        if stage == "take over":
            return (1 - s) * start_q + s * zero, s
        if stage == "arms out":
            return (1 - s) * zero + s * out, 1.0
        if stage == "wave":
            q = out.copy()
            # ease into and out of the swing so the elbow doesn't jump
            envelope = smooth(t / 0.5) * smooth((duration - t) / 0.5)
            swing = WAVE_AMPLITUDE * math.sin(2 * math.pi * WAVE_HZ * t) * envelope
            q[WAVE_JOINT] = (1 - envelope) * out[WAVE_JOINT] + envelope * WAVE_CENTER + swing
            return q, 1.0
        if stage == "return":
            return (1 - s) * out + s * zero, 1.0
        return zero, 1 - s   # release


def main():
    limits = load_joint_limits(DEFAULT_MODEL)
    print(f"=== sim_arm_demo (SIM only: domain {SIM_DOMAIN} on {SIM_INTERFACE}) ===")
    ChannelFactoryInitialize(SIM_DOMAIN, SIM_INTERFACE)
    demo = Demo(limits)

    pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
    pub.Init()
    ChannelSubscriber("rt/lowstate", LowState_).Init(demo.on_lowstate, 10)

    print("Waiting for the simulator's rt/lowstate...")
    deadline = time.monotonic() + 10.0
    while demo.state is None:
        if time.monotonic() > deadline:
            print("No rt/lowstate after 10 s: is the simulator running?")
            return 1
        time.sleep(0.1)
    start_q = demo.current_q()

    try:
        for stage, duration in STAGES:
            print(f"Stage: {stage} ({duration:g}s)")
            t0 = time.perf_counter()
            next_tick = t0
            while (t := time.perf_counter() - t0) < duration:
                q, weight = demo.target(stage, t, duration, start_q)
                pub.Write(demo.build(q, weight))
                next_tick += CONTROL_DT
                time.sleep(max(0.0, next_tick - time.perf_counter()))
        pub.Write(demo.build(pose({}), 0.0))
    except KeyboardInterrupt:
        # stop publishing; the bridge eases the weight back to 0 on its own
        print("\nInterrupted; the bridge will release the arms.")
        return 130

    q = demo.current_q()
    print("Done. Final arm joints (rad): " +
          " ".join(f"{i}:{q[i]:+.2f}" for i in (L_SH_ROLL, R_SH_ROLL, L_ELBOW, R_ELBOW)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
