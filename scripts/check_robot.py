"""
Verify G1 connection and print live joint state.
Run this before teleop to confirm the robot is reachable.

Usage:
    python scripts/check_robot.py --iface eth0
"""

import argparse
import time
import sys

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default="eth0", help="Network interface connected to G1")
    args = parser.parse_args()

    ChannelFactoryInitialize(0, args.iface)

    received = []

    def on_state(msg: LowState_):
        received.append(msg)

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(on_state, 10)

    print(f"Listening on {args.iface} for G1 state (5 s)...")
    time.sleep(5.0)

    if not received:
        print("ERROR: No state received. Check:")
        print("  1. Ethernet cable connected to G1")
        print("  2. Correct network interface (run `ip link` to list interfaces)")
        print("  3. Your IP is on the 192.168.123.x subnet")
        sys.exit(1)

    msg = received[-1]
    print(f"\nReceived {len(received)} packets in 5 s ({len(received)/5:.0f} Hz)")
    print(f"\nIMU RPY (rad): {list(msg.imu_state.rpy)}")
    print(f"\nJoint positions (rad):")
    for i, ms in enumerate(msg.motor_state[:29]):
        print(f"  [{i:2d}]  q={ms.q:+.4f}  dq={ms.dq:+.4f}  tau={ms.tau_est:+.4f}")


if __name__ == "__main__":
    main()
