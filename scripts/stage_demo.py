#!/usr/bin/env python3
"""
stage_demo.py  -  Yotie as a stage presenter: hear a question, answer aloud with
arm and waist gestures, and walk a little inside a small stage area.

  hear   the robot's own microphone and speech recognition (rt/audio_msg) when a sentence
         has "Yotie" in it, and anything typed
  think  Claude writes a short spoken reply with gesture and move marks (teleop/stage_moves.py)
  speak  edge-tts on the robot's speaker (AudioClient.PlayStream) or the computer's speakers
  move   arms + waist on rt/arm_sdk, each gesture landing on its marked word;
         walking with LocoClient.SetVelocity, so the robot's own balance controller moves
         the legs. Nothing here ever publishes rt/lowcmd.

Modes (give exactly one):
  python3 scripts/stage_demo.py enp2s0 [options]   # the real robot, on that network interface
  python3 scripts/stage_demo.py --sim [options]    # MuJoCo simulator + sim_arm_bridge.py (domain 1, lo)
  python3 scripts/stage_demo.py --fake [options]   # no robot or DDS at all: commands are printed

Rehearsal ladder on the robot (instructor present, remote in hand, area clear; the operator
puts the robot in its standing/walking mode with the remote first - this program never
changes the robot's mode):
  1. enp2s0 --dry-run          reads the robot and prints its FSM id; sends nothing, speaks on this computer
  2. enp2s0 --fsm <id>         speech + arm/waist gestures, no walking
  3. enp2s0 --fsm <id> --walk --no-arms     walking only
  4. enp2s0 --fsm <id> --walk               everything

Type at the "you>" prompt:
  any sentence     talk to Yotie (same as speaking to the robot)
  say <text>       Yotie says <text> exactly, marks included, without Claude: "say [wave] Hello!"
  stop             stop walking and release the arms now
  center           walk back to the starting spot
  map              where Yotie thinks it is on the stage
  quit             stop and exit (Ctrl+C does the same)
The remote is the emergency stop. Every turn is logged to logs/stage_demo_<time>.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from teleop import stage_moves as sm

# ── config ───────────────────────────────────────────────────────────────────
ROOT          = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROBOT_NAME    = "Yotie"
SIM_DOMAIN    = 1        # unitree_mujoco's domain; the real robot uses 0
SIM_INTERFACE = "lo"
ASR_TOPIC     = "rt/audio_msg"
LOWSTATE_TOPIC = "rt/lowstate"
ARM_SDK_TOPIC = "rt/arm_sdk"
APP_NAME      = "yotie"
# The robot's microphone only counts when its name is said (so it doesn't answer the people
# presenting next to it). Speech recognition writes "Yotie" many ways (team's list).
WAKE_NAME     = re.compile(r"\b(yotie|yoti|yotee|yodee|yodie|yody|yoty|you[- ]?tee|yo[- ]?tee)\b", re.IGNORECASE)

MODEL         = "claude-opus-5-5"
MAX_HISTORY_TURNS = 8
CLAUDE_TIMEOUT = 20.0

VOICE         = "en-US-JennyNeural"
PCM_SAMPLE_RATE = 16000
PCM_CHUNK_BYTES = 96000
AUDIO_LATENCY = {"robot": 0.35, "laptop": 0.15, "none": 0.0}   # command -> first sound, seconds

CONTROL_DT    = 0.02     # 50 Hz, like Unitree's arm_sdk examples
TAKE_SECONDS  = 0.6      # weight 0 -> 1 holding the arms where they are
RELEASE_SECONDS = 0.8    # weight 1 -> 0 after a reply
ABORT_RELEASE_SECONDS = 1.0
WEIGHT_JOINT  = 29       # motor_cmd[29].q: arm_sdk blend weight, 0 = robot's own pose
ARM_KP, ARM_KD = 45.0, 1.2     # team's gesture_client gains
WAIST_KP, WAIST_KD = 60.0, 1.5 # g1_arm7_sdk_dds_example gains
STATE_MAX_AGE = 0.25     # rt/lowstate older than this is stale (the robot sends ~500 Hz)
FSM_POLL_SECONDS = 0.5
FSM_MAX_AGE   = 2.0
TRACK_ERROR_RAD = 0.35   # measured joint this far from its command ...
TRACK_ERROR_FRAMES = 15  # ... for this many frames in a row: something is in the way

MAX_WALK_SPEED = 0.2     # m/s, hard cap
MAX_TURN_SPEED = 0.4     # rad/s, hard cap
MAX_MOVE_SECONDS = 3.0   # every walking command times out on the robot after this
SETTLE_SECONDS = 0.6     # standing still after a move before anything else

SYSTEM_PROMPT = (
    f"You are {ROBOT_NAME}, a friendly Unitree G1 humanoid robot presenting on a small stage at "
    "California State University, San Bernardino (CSUSB), in a student team's project demo. "
    "You are speaking aloud to an audience: keep every reply to 1-3 short sentences in plain words, "
    "with no markdown, lists, emoji or special characters.\n\n"
    "You gesture while you talk. Put a mark in square brackets just before the word a gesture belongs "
    "with; marks are never spoken. Gestures: [welcome] greeting the room, [wave] hello or goodbye, "
    "[bow] thanks or an ending, [yes] agreeing or confirming, [point] the screen on your left, "
    "[point_right] the screen on your right, [one_hand] then [other_hand] contrasting two ideas, "
    "[small] and [big] for size or amount, [ask] inviting questions, [think] before considering "
    "something, [shrug] when unsure, [face_left] and [face_right] turning to people on one side. "
    "Use one or two gestures in most replies and never more than three."
)
WALK_PROMPT = (
    "\n\nYou can also walk a little on the stage. Put at most one move mark at the very start of a "
    "reply: [walk_left], [walk_right], [step_forward], [step_back], [turn_left], [turn_right], or "
    "[center] to go back to your starting spot. Most replies have no move: move only when someone asks "
    "you to, or now and then to face another part of the room, at most one reply in three."
)
NO_WALK_PROMPT = "\n\nYou stay in one place on the stage; do not use any walking marks."

# scripted replies for --offline, or when Claude can't be reached: (keywords, reply)
OFFLINE_REPLIES = [
    (("hello", "hi ", "hey", "good morning", "good afternoon"),
     "[wave] Hello everyone! I am Yotie, and I am [welcome] happy to be here with you today."),
    (("your name", "who are you"),
     "[yes] I am Yotie, a Unitree G1 humanoid robot. My team taught me to talk and [big] move while I speak."),
    (("what can you do", "what do you do"),
     "[one_hand] I can listen and answer questions, and [other_hand] I can gesture with my arms while I talk."),
    (("walk", "move", "come", "over here"),
     "[walk_left] Let me come over to this side. [face_left] Hello to everyone over here!"),
    (("question",),
     "[ask] Does anyone have a question for me?"),
    (("thank", "bye", "goodbye"),
     "[bow] Thank you all for listening. [wave] Goodbye!"),
]
OFFLINE_DEFAULT = "[think] That is a great question. [shrug] I will need my team to help me with that one."


def load_env():
    """ANTHROPIC_API_KEY from .env or env in the repo root (both gitignored). Never printed."""
    for name in (".env", "env"):
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Log:
    """One JSON object per line: what was heard, said, gestured and walked, with timings."""

    def __init__(self, mode: str):
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(ROOT, "logs", f"stage_demo_{stamp}_{mode}.jsonl")
        self.lock = threading.Lock()

    def __call__(self, event: str, **fields):
        fields = {"t": datetime.now().isoformat(timespec="milliseconds"), "event": event, **fields}
        with self.lock, open(self.path, "a") as f:
            f.write(json.dumps(fields, default=str) + "\n")


def say(text: str):
    """Print without wrecking the "you>" prompt too badly."""
    print(f"\r{text}", flush=True)


# ── robot connection ─────────────────────────────────────────────────────────
class Robot:
    """DDS, state and the clients, for all three modes. In --fake mode the "robot" is a
    few numbers here that follow the commands."""

    def __init__(self, args, log: Log):
        self.mode = "fake" if args.fake else "sim" if args.sim else "robot"
        self.dry_run = args.dry_run
        self.log = log
        self.lock = threading.Lock()
        self.upper = np.zeros(17)        # measured waist + arms
        self.state_time = 0.0
        self.mode_machine = None
        self.fsm_id, self.fsm_time = None, 0.0
        self.publisher = self.audio = self.loco = self.crc = self.cmd = None
        self.speaking = threading.Event()

        if self.mode == "fake":
            self.state_time = time.monotonic()
            threading.Thread(target=self._fake_state, daemon=True).start()
            return

        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.utils.crc import CRC

        if self.mode == "sim":
            ChannelFactoryInitialize(SIM_DOMAIN, SIM_INTERFACE)
        else:
            ChannelFactoryInitialize(0, args.interface)
        ChannelSubscriber(LOWSTATE_TOPIC, LowState_).Init(self._on_lowstate, 10)
        self.cmd = unitree_hg_msg_dds__LowCmd_()
        self.crc = CRC()
        if not self.dry_run:
            self.publisher = ChannelPublisher(ARM_SDK_TOPIC, LowCmd_)
            self.publisher.Init()

        if self.mode == "robot":
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
            from unitree_sdk2py.g1.loco import g1_loco_client
            self.audio = AudioClient()
            self.audio.SetTimeout(10.0)
            self.audio.Init()
            if args.loco_service:
                g1_loco_client.LOCO_SERVICE_NAME = args.loco_service   # older firmware: "loco"
            self.loco = g1_loco_client.LocoClient()
            self.loco.SetTimeout(3.0)
            self.loco.Init()
            threading.Thread(target=self._poll_fsm, daemon=True).start()

    def _on_lowstate(self, msg):
        upper = np.array([msg.motor_state[i].q for i in sm.UPPER_JOINTS])
        with self.lock:
            self.upper, self.state_time, self.mode_machine = upper, time.monotonic(), msg.mode_machine

    def _fake_state(self):
        while True:
            with self.lock:
                self.state_time = time.monotonic()
            time.sleep(0.01)

    def _poll_fsm(self):
        while True:
            try:
                code, fsm = self.loco.GetFsmId()
            except Exception as exc:          # an RPC failure is "unknown", never "allowed"
                code, fsm = -1, None
                self.log("fsm_error", error=repr(exc))
            with self.lock:
                self.fsm_id = fsm if code == 0 else None
                self.fsm_time = time.monotonic()
            time.sleep(FSM_POLL_SECONDS)

    def state(self):
        with self.lock:
            return self.upper.copy(), time.monotonic() - self.state_time

    def fsm(self):
        """Robot: (fsm id or None, age seconds). Sim/fake: there is no FSM service."""
        with self.lock:
            return self.fsm_id, time.monotonic() - self.fsm_time

    def send_upper(self, q17: np.ndarray, weight: float) -> bool:
        """One rt/arm_sdk command: waist + arms at q17, blend weight. Returns False on failure."""
        if self.mode == "fake":
            with self.lock:   # the fake arms follow the command (when the weight has them)
                self.upper = self.upper + weight * (q17 - self.upper) * 0.5
            return True
        if self.dry_run:
            return True
        cmd = self.cmd
        for k, joint in enumerate(sm.UPPER_JOINTS):
            m = cmd.motor_cmd[joint]
            m.q, m.dq, m.tau = float(q17[k]), 0.0, 0.0
            m.kp, m.kd = (WAIST_KP, WAIST_KD) if k < 3 else (ARM_KP, ARM_KD)
        cmd.motor_cmd[WEIGHT_JOINT].q = float(weight)
        cmd.crc = self.crc.Crc(cmd)
        try:
            return bool(self.publisher.Write(cmd))
        except Exception as exc:
            self.log("arm_sdk_write_error", error=repr(exc))
            return False

    def led(self, r, g, b):
        if self.audio and not self.dry_run:
            try:
                self.audio.LedControl(r, g, b)
            except Exception:
                pass


def fsm_ok(robot: Robot, allowed) -> tuple[bool, str]:
    """Gestures and walking only while the robot reports an allowed FSM id (robot mode)."""
    if robot.mode != "robot":
        return True, ""
    fsm, age = robot.fsm()
    if fsm is None or age > FSM_MAX_AGE:
        return False, f"robot FSM unknown (GetFsmId failing or stale {age:.1f}s)"
    if fsm not in allowed:
        return False, f"robot FSM is {fsm}, not one of {sorted(allowed)} (--fsm)"
    return True, ""


# ── arms and waist ───────────────────────────────────────────────────────────
class ArmPerformer:
    """Takes the arms (weight 0 -> 1 holding them still), plays one GestureTimeline,
    and gives them back (weight 1 -> 0). Every command is clamped to the joint limits and
    speed-capped, and the run is abandoned - weight ramped back to 0 - on stop, stale
    state, a disallowed FSM, a failed write, or arms that stop following."""

    def __init__(self, robot: Robot, log: Log, allowed_fsm, enabled: bool, beat: bool):
        self.robot, self.log, self.allowed, self.enabled, self.beat = robot, log, allowed_fsm, enabled, beat

    def perform(self, cues, speech_seconds: float, start_at: float, stop: threading.Event, result: dict):
        if not self.enabled:
            result["arms"] = "off"
            return
        ok, why = self._ready()
        if not ok:
            say(f"  [arms] not moving: {why}")
            result["arms"] = f"refused: {why}"
            return
        rest, _ = self.robot.state()
        rest = sm.clamp(rest)
        timeline = sm.GestureTimeline(rest, cues, speech_seconds, beat=self.beat)
        last = rest.copy()
        peak = np.zeros(17)
        reason = None
        try:
            # take: hold the measured pose while the weight comes up
            t0 = time.monotonic()
            while (r := (time.monotonic() - t0) / TAKE_SECONDS) < 1.0:
                reason = self._tick(last, float(sm.ease(r)), stop)
                if reason:
                    raise _Abort(reason)
            # wait for the first sound, holding still
            while time.monotonic() < start_at:
                reason = self._tick(last, 1.0, stop)
                if reason:
                    raise _Abort(reason)
            # play
            lagging = 0
            while (t := time.monotonic() - start_at) < timeline.duration:
                target = timeline.at(t)
                step = sm.SPEED_CAP * CONTROL_DT
                q = last + np.clip(target - last, -step, step)
                measured, _ = self.robot.state()
                lagging = lagging + 1 if np.abs(measured - last).max() > TRACK_ERROR_RAD else 0
                if lagging >= TRACK_ERROR_FRAMES and not self.robot.dry_run:
                    raise _Abort("arms are not following their commands (blocked?)")
                reason = self._tick(q, 1.0, stop)
                if reason:
                    raise _Abort(reason)
                peak = np.maximum(peak, np.abs(q - rest))
                last = q
            # timeline ends at rest; give the arms back
            t0 = time.monotonic()
            while (r := (time.monotonic() - t0) / RELEASE_SECONDS) < 1.0:
                self._tick(last, 1.0 - sm.ease(r), None)
            self._tick(last, 0.0, None)
            result["arms"] = "done"
        except (_Abort, Exception) as abort:
            if not isinstance(abort, _Abort):
                self.log("arm_internal_error", error=repr(abort))
            say(f"  [arms] STOPPED: {abort!s}")
            result["arms"] = f"aborted: {abort!s}"
            t0 = time.monotonic()
            while (r := (time.monotonic() - t0) / ABORT_RELEASE_SECONDS) < 1.0:
                self._send(last, 1.0 - r)
                time.sleep(CONTROL_DT)
            self._send(last, 0.0)
        result["gestures"] = [g["name"] for g in timeline.plan]
        result["peak_offset_rad"] = {name: round(float(peak[k]), 3) for k, name in enumerate(UPPER_NAMES) if peak[k] > 0.02}
        if self.robot.dry_run:
            moved = ", ".join(f"{n} {v:+.2f}" for n, v in result["peak_offset_rad"].items()) or "nothing"
            say(f"  [arms] dry run, would move (rad from rest): {moved}")

    def _ready(self):
        _, age = self.robot.state()
        if age > STATE_MAX_AGE:
            return False, f"no fresh {LOWSTATE_TOPIC} (last {age:.1f}s ago)"
        return fsm_ok(self.robot, self.allowed)

    def _tick(self, q, weight, stop):
        """Send one command and wait for the next tick; returns a reason to abort, or None."""
        tick_start = time.monotonic()
        if stop is not None and stop.is_set():
            return "stop requested"
        ok, why = self._ready()
        if not ok:
            return why
        if not self._send(q, weight):
            return "rt/arm_sdk write failed"
        time.sleep(max(0.0, CONTROL_DT - (time.monotonic() - tick_start)))
        return None

    def _send(self, q, weight):
        return self.robot.send_upper(sm.clamp(q), float(np.clip(weight, 0.0, 1.0)))


class _Abort(Exception):
    pass


UPPER_NAMES = ["waist_yaw", "waist_roll", "waist_pitch"] + [
    f"{side}_{joint}" for side in ("L", "R") for joint in (
        "shoulder_pitch", "shoulder_roll", "shoulder_yaw", "elbow", "wrist_roll", "wrist_pitch", "wrist_yaw")]


# ── walking ──────────────────────────────────────────────────────────────────
class Walker:
    """Stage moves with LocoClient.SetVelocity: the robot's own controller walks.
    Each command carries its own timeout, so the robot stops by itself even if this
    program dies. Arms are never taken while walking."""

    def __init__(self, robot: Robot, stage: sm.Stage, log: Log, allowed_fsm, enabled: bool):
        self.robot, self.stage, self.log, self.allowed, self.enabled = robot, stage, log, allowed_fsm, enabled

    def run(self, name: str, stop: threading.Event) -> list:
        if not self.enabled:
            say(f"  [walk] {name}: skipped (walking is off; --walk turns it on)")
            return []
        steps = self.stage.plan(name)
        if not steps:
            say(f"  [walk] {name}: no room inside the stage box, staying put")
            return []
        done = []
        for vx, vy, vyaw, seconds in steps:
            vx = float(np.clip(vx, -MAX_WALK_SPEED, MAX_WALK_SPEED))
            vy = float(np.clip(vy, -MAX_WALK_SPEED, MAX_WALK_SPEED))
            vyaw = float(np.clip(vyaw, -MAX_TURN_SPEED, MAX_TURN_SPEED))
            seconds = float(min(seconds, MAX_MOVE_SECONDS))
            ok, why = fsm_ok(self.robot, self.allowed)
            if not ok:
                say(f"  [walk] {name}: not walking: {why}")
                break
            say(f"  [walk] {name}: vx={vx:+.2f} vy={vy:+.2f} m/s yaw={vyaw:+.2f} rad/s for {seconds:.1f}s"
                + ("  (dry run: not sent)" if self.robot.dry_run else ""))
            if self.robot.mode == "robot" and not self.robot.dry_run:
                code = self.robot.loco.SetVelocity(vx, vy, vyaw, seconds)
                if code != 0:
                    say(f"  [walk] SetVelocity returned {code}; walking turned off for this run")
                    self.log("walk_error", move=name, code=code)
                    self.enabled = False
                    break
            t_end = time.monotonic() + seconds
            interrupted = stop.wait(timeout=seconds)
            ran = seconds - max(0.0, t_end - time.monotonic())
            self.stop()
            self.stage.apply(vx, vy, vyaw, ran)
            done.append({"move": name, "vx": vx, "vy": vy, "vyaw": vyaw, "seconds": round(ran, 2)})
            time.sleep(SETTLE_SECONDS)
            if interrupted:
                break
        say(f"  [walk] {self.stage.map()}")
        return done

    def stop(self):
        if self.robot.mode == "robot" and not self.robot.dry_run and self.robot.loco is not None:
            try:
                self.robot.loco.StopMove()
            except Exception as exc:
                self.log("stop_move_error", error=repr(exc))


# ── voice ────────────────────────────────────────────────────────────────────
class Voice:
    """edge-tts with word times. Plays on the robot (PlayStream), on this computer
    (ffplay), or not at all (text only, word times estimated)."""

    def __init__(self, robot: Robot, output: str, log: Log):
        self.robot, self.output, self.log = robot, output, log
        self.player = None

    def synth(self, text: str):
        """(mp3 bytes or None, [(start s, word)], seconds)."""
        if self.output == "none" or not text:
            return None, [], sm.estimate_seconds(text)
        try:
            import edge_tts
            from pydub import AudioSegment

            async def run():
                audio, words = [], []
                async for chunk in edge_tts.Communicate(text, VOICE, boundary="WordBoundary").stream():
                    if chunk["type"] == "audio":
                        audio.append(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        words.append((chunk["offset"] / 1e7, chunk["text"]))
                return b"".join(audio), words

            mp3, words = asyncio.run(run())
            seconds = len(AudioSegment.from_mp3(io.BytesIO(mp3))) / 1000.0
            return mp3, words, seconds
        except Exception as exc:
            say(f"  [voice] edge-tts failed ({type(exc).__name__}); falling back")
            self.log("tts_error", error=repr(exc))
            return None, [], sm.estimate_seconds(text)

    def play(self, mp3, text: str, start_at: float, seconds: float, stop: threading.Event):
        """Starts the sound so that it is heard at start_at, then waits until it is over."""
        latency = AUDIO_LATENCY[self.output]
        time.sleep(max(0.0, start_at - latency - time.monotonic()))
        self.robot.speaking.set()
        try:
            if self.output == "robot":
                self.robot.led(0, 128, 0)
                if mp3 is None:
                    self.robot.audio.TtsMaker(text, 0)     # robot's built-in voice, no internet needed
                else:
                    self._play_robot(mp3)
            elif self.output == "laptop" and mp3 is not None and shutil.which("ffplay"):
                self.player = subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", "pipe:0"],
                    stdin=subprocess.PIPE)
                try:
                    self.player.stdin.write(mp3)
                    self.player.stdin.close()
                except BrokenPipeError:
                    pass
            stop.wait(timeout=max(0.0, start_at + seconds - time.monotonic()) + 0.2)
        finally:
            self.halt()
            self.robot.led(0, 0, 0)
            time.sleep(0.3)   # don't hear the tail of our own voice
            self.robot.speaking.clear()

    def _play_robot(self, mp3: bytes):
        from pydub import AudioSegment
        pcm = AudioSegment.from_mp3(io.BytesIO(mp3)).set_frame_rate(PCM_SAMPLE_RATE).set_channels(1).set_sample_width(2).raw_data
        stream_id = str(int(time.time() * 1000))

        def feed():   # same chunking as sb01_conversation.py
            for offset in range(0, len(pcm), PCM_CHUNK_BYTES):
                self.robot.audio.PlayStream(APP_NAME, stream_id, list(pcm[offset:offset + PCM_CHUNK_BYTES]))
                if offset + PCM_CHUNK_BYTES < len(pcm):
                    time.sleep(1.0)
        threading.Thread(target=feed, daemon=True).start()

    def halt(self):
        if self.player is not None and self.player.poll() is None:
            self.player.terminate()
        self.player = None
        if self.output == "robot" and self.robot.speaking.is_set():
            try:
                self.robot.audio.PlayStop(APP_NAME)
            except Exception:
                pass


# ── hearing ──────────────────────────────────────────────────────────────────
class Ears:
    """Typed lines, plus the robot's speech recognition in robot mode. While Yotie is
    speaking, recognition results are ignored (it would hear itself)."""

    def __init__(self, robot: Robot, use_asr: bool, wake: bool, log: Log, on_stop):
        self.robot, self.log, self.wake, self.on_stop = robot, log, wake, on_stop
        self.inbox = queue.Queue()
        self.busy = threading.Event()   # a reply is in progress: the mic is not taking questions
        threading.Thread(target=self._typed, daemon=True).start()
        if use_asr and robot.mode == "robot":
            from unitree_sdk2py.core.channel import ChannelSubscriber
            from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_
            ChannelSubscriber(ASR_TOPIC, String_).Init(self._on_asr, 10)
            say(f"[ears] listening on the robot's microphone ({ASR_TOPIC}) and the keyboard")
        else:
            say("[ears] listening on the keyboard")

    def _typed(self):
        while True:
            try:
                line = input("you> ")
            except EOFError:
                self.inbox.put(("typed", "quit"))
                return
            if line.strip().lower() in ("stop", "quit", "exit"):
                self.on_stop()               # act now, even in the middle of a reply
            if line.strip():
                self.inbox.put(("typed", line.strip()))

    def _on_asr(self, msg):
        try:
            data = json.loads(msg.data)
        except (json.JSONDecodeError, AttributeError):
            return
        if "play_state" in data or self.robot.speaking.is_set() or self.busy.is_set():
            return
        text = data.get("text", "").strip()
        if not data.get("is_final", False) or len(text) < 3:
            return
        if self.wake and not WAKE_NAME.search(text):
            say(f"[heard, not for {ROBOT_NAME}] {text}")
            return
        say(f"[heard] {text}")
        self.log("heard", text=text)
        self.inbox.put(("robot_mic", text))

    def get(self):
        return self.inbox.get()


# ── thinking ─────────────────────────────────────────────────────────────────
class Brain:
    def __init__(self, offline: bool, walking: bool, log: Log):
        self.log = log
        self.system = SYSTEM_PROMPT + (WALK_PROMPT if walking else NO_WALK_PROMPT)
        self.history = []
        self.client = None
        if not offline:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                say("[brain] no ANTHROPIC_API_KEY (in .env); using scripted replies")
            else:
                import anthropic
                self.anthropic = anthropic
                self.client = anthropic.Anthropic(timeout=CLAUDE_TIMEOUT, max_retries=1)

    def reply(self, text: str):
        """(reply with marks, "claude" or "offline", milliseconds)."""
        t0 = time.monotonic()
        if self.client is None:
            return self._offline(text), "offline", 0
        messages = self.history + [{"role": "user", "content": text}]
        request = dict(model=MODEL, max_tokens=4000, system=self.system, messages=messages)
        try:
            try:
                response = self.client.beta.messages.create(
                    **request,
                    output_config={"effort": "low"},          # short spoken replies: fast
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",                       # a declined request is retried server-side
                )
            except TypeError:
                # an older anthropic package (lab PC?) without these parameters: same request, raw fields
                response = self.client.messages.create(
                    **request,
                    extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                    extra_body={"output_config": {"effort": "low"}, "fallbacks": "default"},
                )
        except self.anthropic.APIConnectionError as exc:
            return self._failed(text, t0, f"no connection: {exc}")
        except self.anthropic.RateLimitError as exc:
            return self._failed(text, t0, f"rate limited: {exc}")
        except self.anthropic.APIStatusError as exc:
            return self._failed(text, t0, f"API error {exc.status_code}: {exc.message}")
        except Exception as exc:     # never let a reply problem stop the demo
            return self._failed(text, t0, repr(exc))
        ms = round((time.monotonic() - t0) * 1000)
        if response.stop_reason == "refusal":
            self.log("claude_refusal", category=getattr(response.stop_details, "category", None))
            reply = "[shrug] Sorry, I can't help with that one. [ask] Any other questions?"
        else:
            reply = " ".join(b.text for b in response.content if b.type == "text").strip() or OFFLINE_DEFAULT
        self.history = (messages + [{"role": "assistant", "content": reply}])[-2 * MAX_HISTORY_TURNS:]
        return reply, "claude", ms

    def _failed(self, text, t0, why):
        say(f"  [brain] Claude failed ({why}); using a scripted reply")
        self.log("claude_error", error=why)
        return self._offline(text), "offline", round((time.monotonic() - t0) * 1000)

    @staticmethod
    def _offline(text: str) -> str:
        lowered = f" {text.lower()} "
        for keywords, reply in OFFLINE_REPLIES:
            if any(k in lowered for k in keywords):
                return reply
        return OFFLINE_DEFAULT


# ── main ─────────────────────────────────────────────────────────────────────
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Yotie stage presenter demo: talk, gesture, walk a little.")
    p.add_argument("interface", nargs="?", help="network interface wired to the G1, e.g. enp2s0")
    p.add_argument("--sim", action="store_true", help="MuJoCo simulator (domain 1 on lo); run sim_arm_bridge.py too")
    p.add_argument("--fake", action="store_true", help="no robot or DDS: print everything")
    p.add_argument("--dry-run", action="store_true",
                   help="robot: read state and FSM, send nothing (no arms, walking, LED or robot audio)")
    p.add_argument("--fsm", default=os.environ.get("SB01_GESTURE_FSM_IDS", "802"),
                   help="comma-separated FSM ids in which arms and walking are allowed (default 802, the team's)")
    p.add_argument("--walk", action="store_true", help="allow stage moves (off by default)")
    p.add_argument("--no-arms", action="store_true", help="no arm or waist gestures")
    p.add_argument("--no-beat", action="store_true", help="no small talking motion between gestures")
    p.add_argument("--box", type=float, default=0.5, help="stage half-width in m around the start (max 1.0)")
    p.add_argument("--speed", type=float, default=0.15, help=f"walking speed m/s (max {MAX_WALK_SPEED})")
    p.add_argument("--offline", action="store_true", help="scripted replies instead of Claude")
    p.add_argument("--no-voice", action="store_true", help="print replies instead of speaking them")
    p.add_argument("--laptop-voice", action="store_true", help="robot mode: speak on this computer instead")
    p.add_argument("--typed-only", action="store_true", help="robot mode: ignore the robot's microphone")
    p.add_argument("--no-wake", action="store_true",
                   help=f"robot mic: answer everything heard, not only sentences with \"{ROBOT_NAME}\" in them")
    p.add_argument("--audio-latency", type=float, default=None,
                   help="seconds from sending audio to hearing it; tune so gestures land on their words "
                        f"(defaults: robot {AUDIO_LATENCY['robot']}, laptop {AUDIO_LATENCY['laptop']})")
    p.add_argument("--loco-service", default="", help='LocoClient service name override (older firmware: "loco")')
    args = p.parse_args(argv)
    if [bool(args.interface), args.sim, args.fake].count(True) != 1:
        p.error("give exactly one of: a network interface, --sim, --fake")
    if args.dry_run and not args.interface:
        p.error("--dry-run is for the robot; --sim and --fake send nothing to a robot anyway")
    if not 0.1 <= args.box <= 1.0:
        p.error("--box must be between 0.1 and 1.0 m")
    if not 0.05 <= args.speed <= MAX_WALK_SPEED:
        p.error(f"--speed must be between 0.05 and {MAX_WALK_SPEED} m/s")
    try:
        args.fsm_ids = {int(v) for v in args.fsm.split(",") if v.strip()}
    except ValueError:
        p.error("--fsm takes comma-separated integers, e.g. 802 or 801,802")
    return args


def main(argv=None):
    args = parse_args(argv)
    load_env()
    mode = "fake" if args.fake else "sim" if args.sim else "robot"
    log = Log(mode)
    robot = Robot(args, log)
    stage = sm.Stage(box=args.box, speed=args.speed, turn_speed=0.3, max_seconds=MAX_MOVE_SECONDS)
    arms = ArmPerformer(robot, log, args.fsm_ids, enabled=not args.no_arms, beat=not args.no_beat)
    walker = Walker(robot, stage, log, args.fsm_ids, enabled=args.walk)
    if args.no_voice:
        output = "none"
    elif mode == "robot" and not args.dry_run and not args.laptop_voice:
        output = "robot"
    else:
        output = "laptop"
    if args.audio_latency is not None:
        AUDIO_LATENCY[output] = args.audio_latency
    voice = Voice(robot, output, log)
    brain = Brain(args.offline, args.walk, log)

    target = f"robot on {args.interface}" if mode == "robot" else ("simulator (domain 1, lo)" if mode == "sim" else "fake robot")
    say(f"=== {ROBOT_NAME} stage demo: {target}{'  DRY RUN: nothing is sent to the robot' if args.dry_run else ''} ===")
    say(f"  arms: {'off' if args.no_arms else 'on'}   walking: {'on' if args.walk else 'off'} "
        f"(box ±{args.box:.2f} m, {args.speed:.2f} m/s)   voice: {output}   "
        f"replies: {'scripted' if brain.client is None else MODEL}")
    if mode == "robot":
        say(f"  arms and walking only in FSM {sorted(args.fsm_ids)}; this program never changes the robot's mode")
    log("start", mode=mode, args={k: v for k, v in vars(args).items() if k != "fsm_ids"}, voice=output)

    # preflight: is the robot (or simulator) there?
    deadline = time.monotonic() + 5.0
    while robot.state()[1] > STATE_MAX_AGE and time.monotonic() < deadline:
        time.sleep(0.1)
    _, age = robot.state()
    if age > STATE_MAX_AGE:
        hint = "is the simulator running?" if mode == "sim" else "check the cable and interface name"
        say(f"WARNING: no {LOWSTATE_TOPIC} yet; {hint}. Gestures will be refused until it arrives.")
    if mode == "robot":
        time.sleep(1.0)
        fsm, _ = robot.fsm()
        say(f"  robot reports FSM id {fsm} (mode_machine {robot.mode_machine}); "
            f"{'allowed' if fsm in args.fsm_ids else 'NOT in --fsm, so no arms or walking'}")
        log("preflight", fsm=fsm, mode_machine=robot.mode_machine)

    stop = threading.Event()

    def stop_now():
        stop.set()
        walker.stop()

    ears = Ears(robot, use_asr=not args.typed_only, wake=not args.no_wake, log=log, on_stop=stop_now)
    say(f"Say something to {ROBOT_NAME}, or type: say <text> | stop | center | map | quit")
    try:
        while True:
            source, text = ears.get()
            command = text.lower().strip()
            if command in ("quit", "exit"):
                break
            if command == "stop":
                stop_now()
                say("  stopped")
                continue
            if command == "map":
                say(f"  {stage.map()}")
                continue
            stop.clear()
            if command == "center":
                log("move", moves=walker.run("center", stop), stage=[stage.x, stage.y, stage.yaw])
                continue
            ears.busy.set()
            turn = {"source": source, "heard": text}
            if command.startswith("say "):
                reply, how, ms = text[4:].strip(), "typed", 0
            else:
                robot.led(0, 0, 128)
                reply, how, ms = brain.reply(text)
                robot.led(0, 0, 0)
            spoken, marks, moves = sm.split_marks(reply)
            say(f"[{ROBOT_NAME}] {reply}   ({how}, {ms} ms)")
            turn.update(reply=reply, replied_by=how, claude_ms=ms, spoken=spoken)

            for move in moves:
                turn.setdefault("moves", []).extend(walker.run(move, stop))
            if stop.is_set() or not spoken:
                log("turn", **turn)
                ears.busy.clear()
                continue

            t0 = time.monotonic()
            mp3, words, seconds = voice.synth(spoken)
            cues = sm.time_marks(marks, words, spoken)
            turn.update(tts_ms=round((time.monotonic() - t0) * 1000), speech_seconds=round(seconds, 2), cues=cues)
            start_at = time.monotonic() + TAKE_SECONDS + 0.3
            result = {}
            performer = threading.Thread(target=arms.perform, args=(cues, seconds, start_at, stop, result))
            performer.start()
            voice.play(mp3, spoken, start_at, seconds, stop)
            performer.join()
            turn.update(result)
            log("turn", **turn)
            ears.busy.clear()
    except KeyboardInterrupt:
        say("\nInterrupted.")
    finally:
        stop.set()
        walker.stop()
        voice.halt()
        log("stop", stage=[stage.x, stage.y, stage.yaw])
        say(f"Stopped. Log: {os.path.relpath(log.path, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
