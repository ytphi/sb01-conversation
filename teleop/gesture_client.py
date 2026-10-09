"""
gesture_client.py  -  co-speech arm gestures for sb01 via scripts/gesture_server.py

Asks the RoboGesture sidecar for a collision-checked arm trajectory for one
utterance and plays it on rt/arm_sdk at 30 Hz while the sidecar is still
generating the rest. The 14 arm joints follow the trajectory and the 3 waist
joints are held where they were, as in Unitree's g1_arm7_sdk_dds_example;
legs and balance stay with the robot's own controller, which arm_sdk blends
against via the weight joint.

One utterance:
  start(pcm)   take the arms where they already are (weight 0 -> 1, no motion)
               while the sidecar generates the first second; returns when ready
  begin(delay) call as the audio is sent; motion starts `delay` seconds later,
               so the first frame of motion lands on the first sound
  ...          arms gesture, return to rest, and the weight ramps back to 0

Nothing is sent unless the robot reports a supported FSM state and rt/lowstate
is fresh. Every block from the sidecar is validated when it arrives, before it
can reach the 30 Hz loop. The gesture is abandoned and the weight ramped back to
0 if the robot leaves the supported state, rt/lowstate goes stale, the arms stop
following, a DDS write fails, the stream is cut short or stalls, or the client
is stopped or closed.

If the next utterance arrives while the arms are still returning, they are kept
at rest (at whatever weight they had reached) and go straight into the next
gesture.

A fixed pose from the sidecar's list (e.g. "thinking") is played the same way,
with the same checks:
  pose(name, seconds)  move there and stay up to `seconds`, then back and release
  finish_pose()        go back now instead of when the time is up
The sidecar only plans the way there (and any slight sway while staying). The
way back is always the way there in reverse, so the arms end exactly where they
were taken.
"""

import atexit
import base64
import json
import os
import queue
import signal
import threading
import time
import urllib.request

import numpy as np

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC

GESTURE_SAMPLE_RATE = 24000   # PCM rate the sidecar expects
EXPECTED_FPS        = 30      # the only playback rate this client accepts

# G1 29-DoF joint indices (unitree_hg LowCmd_.motor_cmd), as in Unitree's
# G1JointIndex.
WAIST_JOINTS = (12, 13, 14)           # yaw, roll, pitch
ARM_JOINTS   = tuple(range(15, 29))   # left 15-21, right 22-28 (arm7 layout)
WEIGHT_JOINT = 29                     # kNotUsedJoint: arm_sdk blend weight, 0 = robot's own pose
ARM_KP = 45.0
ARM_KD = 1.2
# rt/arm_sdk also takes the waist. By default nothing is sent to it: only the
# arms are commanded, as in RoboGesture's own robot code, and the waist stays
# with the robot's balance controller. This robot's waist is not mechanically
# locked, so it is part of how the robot balances, and it was seen to lean off
# balance while gesturing when the waist was held.
# SB01_GESTURE_WAIST=hold brings the earlier behaviour back: the waist is held,
# with the gains of Unitree's own arm_sdk example, where it was when the arms
# were taken.
WAIST_MODE = os.environ.get("SB01_GESTURE_WAIST", "free").strip().lower() or "free"
if WAIST_MODE not in ("hold", "free"):
    raise ValueError(f"SB01_GESTURE_WAIST is {WAIST_MODE!r}; use hold or free")
WAIST_HOLD = WAIST_MODE == "hold"
WAIST_KP = 60.0   # g1_arm7_sdk_dds_example gains
WAIST_KD = 1.5

# Joint limits (rad) of the 14 arm joints, from the G1 29-DoF model: shoulder
# pitch, roll, yaw, elbow, wrist roll, pitch, yaw; left arm then right arm.
ARM_LIMITS = np.array([
    (-3.0892, 2.6704), (-1.5882, 2.2515), (-2.618, 2.618), (-1.0472, 2.0944),
    (-1.97222, 1.97222), (-1.61443, 1.61443), (-1.61443, 1.61443),
    (-3.0892, 2.6704), (-2.2515, 1.5882), (-2.618, 2.618), (-1.0472, 2.0944),
    (-1.97222, 1.97222), (-1.61443, 1.61443), (-1.61443, 1.61443),
], dtype=np.float32)
ARM_NAMES = tuple(
    f"{side}_{joint}" for side in ("left", "right") for joint in (
        "shoulder_pitch", "shoulder_roll", "shoulder_yaw", "elbow",
        "wrist_roll", "wrist_pitch", "wrist_yaw")
)
FLOAT_TOLERANCE = 1e-3          # float32 rounding on values clipped exactly to a limit

MAX_STEP_RAD        = 0.05      # per frame, per joint: RoboGesture's own speed limit (1.5 rad/s)
MAX_HOLD_OFFSET_RAD = 0.3       # how far the sidecar may place the hold pose from the measured arms
MAX_BLOCK_FRAMES    = 300       # one message never carries more than 10 s
STILL_RAD           = 1e-4      # a frame this close to the one being commanded is "not moving"

# Robot state in which gestures are sent. FSM 802 is what this G1 (sb01) reports
# while standing in motion control, and the state in which arm gestures were run
# on the physical robot (mode_pr=0, mode_machine=5). Gestures are refused in any
# other state. To allow different or additional states on another robot or
# firmware, set SB01_GESTURE_FSM_IDS to a comma-separated list, e.g. "802,501"
# (501 is the state RoboGesture's own setup used); it replaces this default.
DEFAULT_FSM_IDS = "802"
FSM_ALLOWED = tuple(
    int(value) for value in os.environ.get("SB01_GESTURE_FSM_IDS", DEFAULT_FSM_IDS).split(",") if value.strip()
)
# Optional: only gesture when rt/lowstate reports one of these mode_machine
# values (SB01_GESTURE_MODE_MACHINE="5"). Unset = not enforced, but it may never
# change while the client is running.
MODE_MACHINE_ALLOWED = tuple(
    int(value) for value in os.environ.get("SB01_GESTURE_MODE_MACHINE", "").split(",") if value.strip()
)
FSM_POLL_SECONDS  = 0.5   # how often the FSM is re-checked during a gesture
STATE_MAX_AGE     = 0.2   # rt/lowstate older than this is stale (it arrives at 500 Hz)
TRACK_ERROR_RAD    = 0.35  # measured arm this far from its command ...
TRACK_ERROR_FRAMES = 10    # ... for this many frames in a row: something is in the way

TAKE_SECONDS          = 0.5   # weight 0 -> 1 while holding the arms in place
HOLD_SECONDS          = 5.0   # give up if speech never starts after start()
STALL_SECONDS         = 3.0   # give up if the sidecar falls this far behind
ABORT_RELEASE_SECONDS = 2.0

_END   = object()   # stream finished cleanly
_ABORT = object()   # stream broke; release the arms


class GestureFault(Exception):
    """A reason not to send (or to stop sending) arm commands."""


def _smooth(ratio):
    return ratio * ratio * (3.0 - 2.0 * ratio)


def _numeric(values, what: str) -> np.ndarray:
    try:
        array = np.asarray(values)
    except Exception as exc:
        raise ValueError(f"{what} is not a regular array: {exc}") from None
    if array.dtype.kind not in "fiu":   # floats and integers only: no bool, complex, str, object
        raise ValueError(f"{what} must hold real numbers, got {array.dtype}")
    return array.astype(np.float32)


def _check_limits(arms: np.ndarray, what: str):
    outside = (arms < ARM_LIMITS[:, 0] - FLOAT_TOLERANCE) | (arms > ARM_LIMITS[:, 1] + FLOAT_TOLERANCE)
    if outside.any():
        joint = int(np.argmax(outside.reshape(-1, 14).any(axis=0)))
        value = float(arms.reshape(-1, 14)[outside.reshape(-1, 14)[:, joint], joint][0])
        low, high = ARM_LIMITS[joint]
        raise ValueError(f"{what}: {ARM_NAMES[joint]} at {value:.3f} rad is outside [{low:.3f}, {high:.3f}]")


def validate_hold(values, reference: np.ndarray, max_offset: float) -> np.ndarray:
    """The pose the arms are taken at: 14 finite angles, within the joint
    limits, and no farther than max_offset from where the arms are."""
    hold = _numeric(values, "hold pose")
    if hold.shape != (14,):
        raise ValueError(f"hold pose has shape {hold.shape}, expected 14 arm joints")
    if not np.isfinite(hold).all():
        raise ValueError("hold pose is not finite")
    _check_limits(hold, "hold pose")
    offset = np.abs(hold - np.asarray(reference, dtype=np.float32))
    if offset.max() > max_offset + FLOAT_TOLERANCE:
        joint = int(np.argmax(offset))
        raise ValueError(f"hold pose puts {ARM_NAMES[joint]} {offset[joint]:.3f} rad from where "
                         f"the arm is (limit {max_offset} rad)")
    return hold


def validate_block(values, previous: np.ndarray) -> np.ndarray:
    """One streamed block: [N][14 arm angles + weight]. Checked once, vectorized,
    when it arrives; `previous` is the arm pose it must continue from.
    Returns float32 [N, 15] or raises ValueError."""
    block = _numeric(values, "block")
    if block.ndim != 2 or block.shape[1] != 15:
        raise ValueError(f"block has shape {block.shape}, expected N x 15 (14 arm joints + weight)")
    if not 1 <= len(block) <= MAX_BLOCK_FRAMES:
        raise ValueError(f"block has {len(block)} frames, expected 1 to {MAX_BLOCK_FRAMES}")
    if not np.isfinite(block).all():
        raise ValueError("block has non-finite values")
    arms, weight = block[:, :14], block[:, 14]
    if weight.min() < 0.0 or weight.max() > 1.0:
        raise ValueError(f"block weight outside 0..1 ({weight.min():.3f} to {weight.max():.3f})")
    _check_limits(arms, "block")
    steps = np.abs(np.diff(np.concatenate((previous[None, :14], arms)), axis=0))
    if steps.max() > MAX_STEP_RAD + FLOAT_TOLERANCE:
        frame, joint = np.unravel_index(int(np.argmax(steps)), steps.shape)
        raise ValueError(f"block moves {ARM_NAMES[joint]} {steps[frame, joint]:.3f} rad in one frame "
                         f"at frame {frame} (limit {MAX_STEP_RAD} rad)")
    return block


class GestureClient:
    """Call from the main thread, after ChannelFactoryInitialize. Call close()
    when done; it is also run at interpreter exit and on SIGTERM / SIGHUP."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self._lock = threading.Lock()
        self._state = None                     # (q[29], mode_pr, mode_machine, monotonic time)
        self._mode_machine: int | None = None  # first value seen; must never change
        self._player: threading.Thread | None = None
        self._held: np.ndarray | None = None   # frame still commanded between gestures
        self._waist: np.ndarray | None = None  # waist angles being held
        self._stop  = threading.Event()   # abort and release
        self._chain = threading.Event()   # next utterance is waiting: skip the release
        self._ready = threading.Event()   # arms taken and first block (or a failure) in
        self._go    = threading.Event()   # audio is starting
        self._wrap  = threading.Event()   # the pose is no longer wanted: go back now
        self._posing = False              # the current stream is a fixed pose, not speech
        self._path_frames: int | None = None   # how many of a pose's frames are the way there
        self._ready_ok = False
        self._go_at = 0.0
        self._fault: str | None = None    # why the guard thread stopped the gesture
        self._closed = False
        self.stats: dict = {}             # timings and tracking error of the latest gesture
        self._cmd = unitree_hg_msg_dds__LowCmd_()
        self._crc = CRC()

        self._loco = LocoClient()         # only ever used to read the FSM id
        self._loco.SetTimeout(1.0)
        self._loco.Init()
        self._loco_lock = threading.Lock()

        self._pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
        self._pub.Init()
        self._sub = ChannelSubscriber("rt/lowstate", LowState_)
        self._sub.Init(self._lowstate_callback, 10)

        print("[gesture] waist: " + ("held in place while the arms are held (SB01_GESTURE_WAIST=hold)" if WAIST_HOLD else
                                     "left to the robot's own balance controller; only the arms are commanded "
                                     "(SB01_GESTURE_WAIST=hold holds it instead)"))
        atexit.register(self.close)
        self._install_signal_handlers()

    def _install_signal_handlers(self):
        """On SIGTERM / SIGHUP start releasing the arms at once, then raise
        SystemExit so `finally` blocks and atexit run. Ctrl-C already raises
        KeyboardInterrupt, which the caller handles."""
        if threading.current_thread() is not threading.main_thread():
            return

        def _exit(signum, _frame):
            self._stop.set()
            raise SystemExit(128 + signum)

        for name in ("SIGTERM", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is not None and signal.getsignal(number) is signal.SIG_DFL:
                signal.signal(number, _exit)

    def _lowstate_callback(self, msg: LowState_):
        q = np.asarray([msg.motor_state[i].q for i in range(29)])
        mode_machine = int(msg.mode_machine)
        first = False
        with self._lock:
            if self._mode_machine is None:
                self._mode_machine = mode_machine
                first = True
            self._state = (q, int(msg.mode_pr), mode_machine, time.monotonic())
        if first:
            print(f"[gesture] robot reports mode_pr={int(msg.mode_pr)} mode_machine={mode_machine}")

    # ── robot state checks ───────────────────────────────────────────────────

    def _fresh_state(self):
        """Latest rt/lowstate, or GestureFault if it is missing or stale."""
        with self._lock:
            state = self._state
        if state is None:
            raise GestureFault("no rt/lowstate received yet")
        age = time.monotonic() - state[3]
        if age > STATE_MAX_AGE:
            raise GestureFault(f"rt/lowstate is stale ({age:.2f} s old)")
        return state

    def _fsm_id(self) -> int | None:
        with self._loco_lock:
            try:
                code, value = self._loco.GetFsmId()
            except Exception:
                return None
        return int(value) if code == 0 and value is not None else None

    def _require_supported_state(self):
        """Fresh state, a consistent robot type and a supported FSM, or GestureFault
        naming the actual FSM id, mode_pr and mode_machine."""
        q, mode_pr, mode_machine, _ = self._fresh_state()
        fsm = self._fsm_id()
        seen = f"FSM={'unreadable' if fsm is None else fsm} mode_pr={mode_pr} mode_machine={mode_machine}"
        if mode_machine != self._mode_machine:
            raise GestureFault(f"mode_machine changed from {self._mode_machine} ({seen})")
        if MODE_MACHINE_ALLOWED and mode_machine not in MODE_MACHINE_ALLOWED:
            raise GestureFault(f"mode_machine not in {MODE_MACHINE_ALLOWED} ({seen})")
        if fsm not in FSM_ALLOWED:
            raise GestureFault(f"robot is not in a supported arm-control state {FSM_ALLOWED} ({seen})")
        return q

    def _guard(self, player: threading.Thread, stop: threading.Event):
        """Re-check the FSM while a gesture plays, off the 30 Hz loop."""
        while player.is_alive() and not stop.wait(FSM_POLL_SECONDS):
            if not player.is_alive():
                return
            try:
                self._require_supported_state()
            except GestureFault as fault:
                self._fault = str(fault)
                stop.set()
                return

    # ── control ──────────────────────────────────────────────────────────────

    def start(self, pcm: bytes, timeout: float = 10.0, pose: str | None = None,
              seconds: float = 0.0, cues: list | None = None) -> bool:
        """Prepare to gesture to this utterance (PCM16LE mono 24 kHz), or, with
        `pose`, to play that fixed pose for `seconds` (see pose()). `cues` are
        teaching gestures for the sidecar to lay over the speech motion:
        [{"name": ..., "time": seconds into the audio}].

        Returns True once the arms are held at full weight and the first second
        of motion is ready: send the audio and call begin(). Returns False if
        gestures are unavailable or refused; the arms are then released.
        """
        if self._closed:
            return False
        held = self._finish_previous()
        resp = None
        try:
            q = self._require_supported_state()
            # Arms still held from the last gesture are where they were left,
            # not where the robot's own controller would put them.
            arms = held[:14] if held is not None else q[15:29]
            if held is None or self._waist is None:
                self._waist = q[12:15].copy()
            what = ({"pcm": base64.b64encode(pcm).decode()} if pose is None
                    else {"pose": pose, "seconds": float(seconds)})
            if cues and pose is None:
                what["cues"] = list(cues)
            body = json.dumps({
                **what,
                "legs":  q[0:12].tolist(),
                "waist": self._waist.tolist(),
                "arms":  np.asarray(arms, dtype=float).tolist(),
            }).encode()
            req = urllib.request.Request(
                self.url + ("/gesture" if pose is None else "/pose"), data=body,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=timeout)
            first = json.loads(resp.readline())
            if "error" in first:
                raise RuntimeError(first["error"])
            if first.get("fps") != EXPECTED_FPS:
                raise ValueError(f"sidecar plays at {first.get('fps')!r} fps, expected {EXPECTED_FPS}")
            hold = validate_hold(
                first.get("hold"), arms,
                MAX_STEP_RAD if held is not None else MAX_HOLD_OFFSET_RAD,
            )
        except Exception as exc:
            print(f"[gesture] not gesturing: {exc}")
            if resp is not None:
                resp.close()
            if held is not None:
                self._release(held, 1.0 / EXPECTED_FPS)
            return False

        for event in (self._stop, self._chain, self._ready, self._go, self._wrap):
            event.clear()
        self._ready_ok = False
        self._posing = pose is not None
        path = first.get("path")
        self._path_frames = path if type(path) is int and path > 0 else None
        self._fault = None
        self.stats = {"blocks": 0, "validate_ms": [], "track_error_max": 0.0}
        frame_queue: queue.Queue = queue.Queue()
        threading.Thread(
            target=self._read, args=(resp, frame_queue, self._stop, hold, self.stats),
            daemon=True,
        ).start()
        # Not a daemon: the interpreter waits for it, so a gesture is never cut
        # off with the weight left above 0.
        self._player = threading.Thread(
            target=self._play, args=(frame_queue, 1.0 / EXPECTED_FPS, held, hold)
        )
        self._player.start()
        threading.Thread(
            target=self._guard, args=(self._player, self._stop), daemon=True
        ).start()

        if not self._ready.wait(timeout) or not self._ready_ok:
            print("[gesture] no motion from the sidecar, releasing arms")
            self.stop()
            return False
        return True

    def begin(self, delay: float = 0.0):
        """Start the motion `delay` seconds from now. Call as the audio is sent."""
        self._go_at = time.monotonic() + max(0.0, delay)
        self._go.set()

    def pose(self, name: str, seconds: float = 5.0) -> bool:
        """Move the arms to a fixed pose from the sidecar's list, stay there up
        to `seconds`, then go back the way they came and release. Same state
        checks, validation and faults as a speech gesture. Returns False if it
        was refused."""
        if not self.start(b"", pose=name, seconds=seconds):
            return False
        self.begin()
        return True

    def finish_pose(self):
        """The pose is no longer wanted: the arms turn round now, wherever they
        are on the way there, and go back through the frames already played.
        If they are already on their way back, nothing changes."""
        self._wrap.set()

    def wait(self):
        """Block until the current gesture has fully released the arms."""
        if self._player is not None:
            self._player.join()
            self._player = None
        if self._held is not None:   # a chained hold nobody picked up
            self._release(self._held, 1.0 / EXPECTED_FPS)
            self._held = None

    def stop(self):
        """Abort the current gesture; arms ramp back to the robot's own pose."""
        self._stop.set()
        self.wait()

    def close(self):
        """Release the arms and shut the DDS channels. Safe to call twice; also
        runs at interpreter exit and after SIGTERM / SIGHUP."""
        if self._closed:
            return
        self._closed = True
        try:
            self.stop()
        finally:
            for channel in (self._sub, self._pub):
                try:
                    channel.Close()
                except Exception:
                    pass

    def _finish_previous(self) -> np.ndarray | None:
        """Let the last gesture bring the arms back to rest, but keep hold of
        them there. Returns the frame still being commanded, or None if released."""
        if self._player is not None:
            self._chain.set()
            self._player.join()
            self._player = None
        held, self._held = self._held, None
        return held

    # ── stream ───────────────────────────────────────────────────────────────

    @staticmethod
    def _read(resp, frame_queue: queue.Queue, stop: threading.Event, hold: np.ndarray,
              stats: dict):
        """Receive and validate blocks. Everything expensive happens here, so
        the 30 Hz loop only ever sees frames that have already passed."""
        result = _ABORT
        previous = hold
        try:
            for line in resp:
                if stop.is_set():
                    break
                message = json.loads(line)
                if message.get("done"):
                    result = _END
                    break
                if "error" in message:
                    raise RuntimeError(message["error"])
                started = time.perf_counter()
                block = validate_block(message.get("frames"), previous)
                stats["validate_ms"].append((time.perf_counter() - started) * 1000.0)
                stats["blocks"] += 1
                previous = block[-1]
                for frame in block:
                    frame_queue.put(frame)
            else:
                print("[gesture] sidecar stream ended early; releasing arms")
        except Exception as exc:
            print(f"[gesture] stream rejected: {exc}; releasing arms")
        finally:
            resp.close()
            frame_queue.put(result)

    # ── playback ─────────────────────────────────────────────────────────────

    def _write(self, frame: np.ndarray, mode_pr: int, mode_machine: int) -> bool:
        """Build one command, add its CRC and publish it. True if DDS took it."""
        self._cmd.mode_pr = mode_pr
        self._cmd.mode_machine = mode_machine
        for joint, q in zip(ARM_JOINTS, frame[:14]):
            motor = self._cmd.motor_cmd[joint]
            motor.q, motor.dq, motor.tau = float(q), 0.0, 0.0
            motor.kp, motor.kd = ARM_KP, ARM_KD
        if WAIST_HOLD and self._waist is not None:
            for joint, q in zip(WAIST_JOINTS, self._waist):
                motor = self._cmd.motor_cmd[joint]
                motor.q, motor.dq, motor.tau = float(q), 0.0, 0.0
                motor.kp, motor.kd = WAIST_KP, WAIST_KD
        self._cmd.motor_cmd[WEIGHT_JOINT].q = float(np.clip(frame[14], 0.0, 1.0))
        self._cmd.crc = self._crc.Crc(self._cmd)
        return bool(self._pub.Write(self._cmd))

    def _publish(self, frame: np.ndarray, track: dict):
        """One step of the 30 Hz loop: fresh-state check, tracking check on the
        previous command, then CRC and publish. Raises GestureFault to abandon."""
        q, mode_pr, mode_machine, _ = self._fresh_state()
        previous = track["last"]
        if previous is not None and previous[14] >= 0.999 and track["full"] >= TRACK_ERROR_FRAMES:
            error = float(np.abs(q[15:29] - previous[:14]).max())
            if error > self.stats.get("track_error_max", 0.0):
                self.stats["track_error_max"] = error
            track["bad"] = track["bad"] + 1 if error > TRACK_ERROR_RAD else 0
            if track["bad"] >= TRACK_ERROR_FRAMES:
                joint = int(np.argmax(np.abs(q[15:29] - previous[:14])))
                raise GestureFault(f"{ARM_NAMES[joint]} is {error:.2f} rad from its command "
                                   f"for {TRACK_ERROR_FRAMES} frames")
        if not self._write(frame, mode_pr, mode_machine):
            raise GestureFault("DDS write to rt/arm_sdk failed")
        track["full"] = track["full"] + 1 if frame[14] >= 0.999 else 0
        track["last"] = frame

    def _play(self, frame_queue: queue.Queue, interval: float,
              held: np.ndarray | None, hold: np.ndarray):
        # last: frame currently commanded (None while released); full: frames in
        # a row at full weight; bad: frames in a row the arms were not following
        track = {"last": held, "full": 0, "bad": 0}
        chained = False
        try:
            # 1. Take the arms at the hold pose. Released arms are already
            #    there, so only the weight moves; held arms are already ours.
            start_weight = 0.0 if held is None else float(held[14])
            start_pose = hold if held is None else held[:14]
            steps = max(1, round(TAKE_SECONDS * (1.0 - start_weight) / interval))
            deadline = time.monotonic()
            for step in range(1, steps + 1):
                if self._stop.is_set():
                    return
                blend = _smooth(step / steps)
                frame = np.empty(15, dtype=np.float32)
                frame[:14] = start_pose + (hold - start_pose) * blend
                frame[14] = start_weight + (1.0 - start_weight) * blend
                delay = deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._publish(frame, track)
                deadline += interval

            # 2. Hold until the first block is in and the audio is starting.
            waited = 0.0
            while True:
                if self._stop.is_set():
                    return
                if not self._ready.is_set() and not frame_queue.empty():
                    head = frame_queue.queue[0]
                    if head is _END or head is _ABORT:
                        return
                    self._ready_ok = True
                    self._ready.set()
                if self._ready_ok and self._go.is_set():
                    remaining = self._go_at - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(interval, remaining))
                else:
                    waited += interval
                    if waited > HOLD_SECONDS + (0.0 if self._ready_ok else STALL_SECONDS):
                        print("[gesture] speech never started, releasing arms")
                        return
                    if self._go.is_set():
                        time.sleep(interval)
                    else:
                        self._go.wait(interval)   # wakes the moment begin() is called
                self._publish(track["last"], track)

            # 3. Play, one frame per 1/30 s from the moment the audio starts.
            #    Frames were validated on arrival; this loop only keeps time,
            #    checks the robot is still there and following, and publishes.
            deadline = time.monotonic()
            stalled = 0.0
            # A fixed pose: `trail` is the path played so far. When the stream
            # ends, breaks or finish_pose() is called, `way_back` (the trail in
            # reverse, then the release) is played instead of the stream.
            trail = [track["last"]] if self._posing else None
            taken = 0                 # frames of the stream played so far
            way_back = None
            while not self._stop.is_set():
                if trail is not None and self._wrap.is_set():
                    way_back, trail = iter(self._way_back(trail, interval, track["last"])), None
                if way_back is not None:
                    frame = next(way_back, _END)
                else:
                    try:
                        frame = frame_queue.get(timeout=interval)
                    except queue.Empty:
                        # Sidecar is behind: hold the pose; motion resumes late.
                        stalled += interval
                        if stalled > STALL_SECONDS:
                            print("[gesture] sidecar stalled, releasing arms")
                            break
                        self._publish(track["last"], track)
                        continue
                if (frame is _END or frame is _ABORT) and trail is not None:
                    way_back, trail = iter(self._way_back(trail, interval, track["last"])), None
                    continue
                if frame is _END or frame is _ABORT:
                    break
                if self._chain.is_set() and frame[14] < track["last"][14]:
                    # Arms are back at rest and the next utterance is waiting:
                    # keep them instead of releasing and taking them again.
                    chained = True
                    break
                if trail is not None:
                    taken += 1
                    on_the_way = self._path_frames is None or taken <= self._path_frames
                    if on_the_way and float(np.abs(frame - trail[-1]).max()) >= STILL_RAD:
                        trail.append(frame)    # not frames that stay put, nor sway at the pose
                stalled = 0.0
                delay = deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._publish(frame, track)
                deadline = max(deadline + interval, time.monotonic())
        except GestureFault as fault:
            print(f"[gesture] {fault}; releasing arms")
        except Exception as exc:
            print(f"[gesture] playback failed ({type(exc).__name__}: {exc}); releasing arms")
        finally:
            if self._fault:
                print(f"[gesture] {self._fault}; releasing arms")
            self._ready.set()   # never leave start() waiting
            last = track["last"]
            if chained:
                self._held = last
            elif last is not None and last[14] > 0.0:
                self._release(last, interval)

    @staticmethod
    def _way_back(trail: list, interval: float, current: np.ndarray) -> list:
        """Frames that take the arms back along `trail` (frames already played,
        so already validated) to where they were taken, then ramp the weight to 0.
        If the arms have swayed off the end of the trail, they first step straight
        back onto it: a short line between two validated poses, in steps under
        the speed limit."""
        frames = []
        gap = float(np.abs(current - trail[-1]).max())
        if gap >= STILL_RAD:
            steps = int(np.ceil(gap / (0.8 * MAX_STEP_RAD)))
            frames = [current + (trail[-1] - current) * (step / steps) for step in range(1, steps + 1)]
        frames += trail[-2::-1]
        home = trail[0]
        for blend in _smooth(np.linspace(0.0, 1.0, max(2, int(ABORT_RELEASE_SECONDS / interval)))):
            frame = home.copy()
            frame[14] = home[14] * (1.0 - blend)
            frames.append(frame)
        return frames

    def _release(self, last: np.ndarray, interval: float):
        """Hold the pose and ramp the weight to 0. Best effort: it keeps going
        through stale state and failed writes, and says so."""
        with self._lock:
            state = self._state
        if state is None:
            return
        count = max(2, int(ABORT_RELEASE_SECONDS / interval))
        frame = last.copy()
        failed = 0
        for blend in _smooth(np.linspace(0.0, 1.0, count)):
            frame[14] = last[14] * (1.0 - blend)
            with self._lock:
                _, mode_pr, mode_machine, _ = self._state
            try:
                if not self._write(frame, mode_pr, mode_machine):
                    failed += 1
            except Exception:
                failed += 1
            time.sleep(interval)
        if failed:
            print(f"[gesture] WARNING: {failed} of {count} release commands were not sent; "
                  f"the arms may still be held. Use the remote (L2+B) if they are.")
