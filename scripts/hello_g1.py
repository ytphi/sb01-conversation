#!/usr/bin/env python3
"""
hello_g1.py  -  First G1 test program: speak, cycle the chest LED, read state.

Proves the VM -> GitHub -> lab computer -> G1 loop using nothing that moves the robot:
  - AudioClient TtsMaker + LedControl (audio and LED only, no joints)
  - read-only subscriber on rt/lowstate: IMU roll/pitch/yaw, shoulder temperatures
No LocoClient, and no publishers on rt/lowcmd or rt/arm_sdk.
(The stock example/g1/audio/g1_audio_client_example.py calls WaveHand(), which
moves the arm; this script deliberately does not.)

Usage:
  python3 scripts/hello_g1.py <network_interface> [--seconds N]   # on the robot, e.g. eno0
  python3 scripts/hello_g1.py --fake [--seconds N]                # offline, no robot
"""

import sys
import math
import time
import random
import argparse
import threading

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowState_
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

# ── config ───────────────────────────────────────────────────────────────────
LOWSTATE_TOPIC = "rt/lowstate"
GREETING       = "Hello, I am the G1. Test program running."
SPEAKER_ID     = 0
LED_SEQUENCE   = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
LED_STEP_SEC   = 1.0
WATCH_JOINTS   = {15: "L-shoulder-pitch", 22: "R-shoulder-pitch"}  # arm7 layout
FAKE_RATE_HZ   = 50   # the real robot publishes lowstate much faster (~500 Hz)


# ── fakes for --fake mode ────────────────────────────────────────────────────
class FakeAudioClient:
    """Stands in for AudioClient: prints instead of calling the robot."""

    def SetTimeout(self, timeout: float):
        pass

    def Init(self):
        pass

    def TtsMaker(self, text: str, speaker_id: int):
        print(f"[TTS] {text!r} (speaker {speaker_id})")
        return 0

    def LedControl(self, R: int, G: int, B: int):
        print(f"[LED] {R},{G},{B}")
        return 0


class FakeLowStateFeed(threading.Thread):
    """Builds real LowState_ objects with plausible values and hands them to
    the same handler the DDS subscriber would call."""

    def __init__(self, handler):
        super().__init__(daemon=True)
        self.handler = handler
        self.stop_event = threading.Event()

    def run(self):
        t0 = time.time()
        while not self.stop_event.is_set():
            t = time.time() - t0
            msg = unitree_hg_msg_dds__LowState_()
            msg.tick = int(t * 1000)
            msg.imu_state.rpy = [
                0.01 * math.sin(t) + random.gauss(0, 0.001),
                -0.02 + random.gauss(0, 0.001),
                0.05 * t,
            ]
            for i in WATCH_JOINTS:
                msg.motor_state[i].temperature = [34 + i % 3, 36 + i % 2]
            self.handler(msg)
            time.sleep(1.0 / FAKE_RATE_HZ)

    def stop(self):
        self.stop_event.set()


# ── lowstate handling ────────────────────────────────────────────────────────
class LowStateMonitor:
    """Keeps the latest LowState_ and a message count. Called from the DDS thread."""

    def __init__(self):
        self.lock   = threading.Lock()
        self.latest = None
        self.count  = 0

    def on_lowstate(self, msg: LowState_):
        with self.lock:
            self.latest = msg
            self.count += 1

    def snapshot(self):
        with self.lock:
            return self.latest, self.count


def format_state(msg: LowState_, count: int, elapsed: float) -> str:
    rpy = list(msg.imu_state.rpy)
    rad = ", ".join(f"{v:+.3f}" for v in rpy)
    deg = ", ".join(f"{math.degrees(v):+6.1f}" for v in rpy)
    temps = "  ".join(
        f"{name}={msg.motor_state[i].temperature[0]}/{msg.motor_state[i].temperature[1]}C"
        for i, name in WATCH_JOINTS.items()
    )
    return (f"[{elapsed:4.1f}s] msgs={count:<6d} mode_machine={msg.mode_machine}  "
            f"rpy rad=({rad}) deg=({deg})  {temps}")


# ── steps ────────────────────────────────────────────────────────────────────
def check(code: int, what: str):
    if code != 0:
        print(f"  warning: {what} returned code {code}")


def say_hello(audio):
    print("Speaking...")
    check(audio.TtsMaker(GREETING, SPEAKER_ID), "TtsMaker")


def cycle_led(audio):
    print("Cycling chest LED...")
    try:
        for r, g, b in LED_SEQUENCE:
            check(audio.LedControl(r, g, b), "LedControl")
            time.sleep(LED_STEP_SEC)
    finally:
        check(audio.LedControl(0, 0, 0), "LedControl off")


def watch_lowstate(monitor: LowStateMonitor, seconds: float) -> int:
    print(f"Reading {LOWSTATE_TOPIC} for {seconds:g}s (read-only)...")
    t0 = time.time()
    next_print = t0 + 1.0
    end = t0 + seconds
    while time.time() < end:
        time.sleep(max(0.0, min(next_print, end) - time.time()))
        msg, count = monitor.snapshot()
        elapsed = time.time() - t0
        if msg is None:
            print(f"[{elapsed:4.1f}s] no lowstate yet")
        else:
            print(format_state(msg, count, elapsed))
        next_print += 1.0
    _, count = monitor.snapshot()
    return count


# ── main ─────────────────────────────────────────────────────────────────────
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="G1 hello test: speak, LED, read-only lowstate.")
    p.add_argument("interface", nargs="?", help="network interface wired to the G1, e.g. eno0")
    p.add_argument("--fake", action="store_true", help="offline mode, no robot connection")
    p.add_argument("--seconds", type=float, default=5.0, help="how long to read lowstate (default 5)")
    args = p.parse_args(argv)
    if args.fake == bool(args.interface):
        p.error("give either a network interface or --fake (not both)")
    if args.seconds <= 0:
        p.error("--seconds must be positive")
    return args


def main(argv=None):
    args = parse_args(argv)
    monitor = LowStateMonitor()
    feed = None

    if args.fake:
        print("=== hello_g1 (FAKE mode: no robot connection) ===")
        audio = FakeAudioClient()
    else:
        print(f"=== hello_g1 on interface {args.interface} ===")
        ChannelFactoryInitialize(0, args.interface)
        audio = AudioClient()

    audio.SetTimeout(10.0)
    audio.Init()

    try:
        say_hello(audio)
        cycle_led(audio)
        # start listening only now, so the message count covers just the read window
        if args.fake:
            feed = FakeLowStateFeed(monitor.on_lowstate)
            feed.start()
        else:
            sub = ChannelSubscriber(LOWSTATE_TOPIC, LowState_)
            sub.Init(monitor.on_lowstate, 10)
        count = watch_lowstate(monitor, args.seconds)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    finally:
        if feed:
            feed.stop()

    if count == 0:
        print(f"WARNING: no {LOWSTATE_TOPIC} received; check the Ethernet cable and interface name.")
        return 1
    print(f"Done: {count} lowstate messages in {args.seconds:g}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
