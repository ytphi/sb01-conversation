"""
Talking gestures for sb01: arm motion while Yotie speaks, matched to what she says.

Drives only the arms through rt/arm_sdk while the robot's walk-mode controller keeps
balancing the legs. Slot 29 of the LowCmd is the blend weight (0 = controller owns the
arms, 1 = we do), as in unitree_sdk2_python/example/g1/high_level/g1_arm7_sdk_dds_example.py.

Each sentence gets a style from its words (pick_style), e.g.
  self     "I'm Yotie…"            right hand to the chest
  open     "welcome", "thank you"  both arms open outward
  you      "you / your …"          one hand reaches toward the listener
  list     "first… also… next"     repeated small counting beats
  wide     "all / every / huge"    wide sweep with both arms
  shrug    "not sure", "maybe"     open-hands shrug
  explain  everything else          alternating beats
  fistbump greeting someone Yotie recognizes (cue from sb01)
Between strokes the arms rest in a "ready to talk" posture with a slow sway; long
sentences get extra explain beats. Poses are offsets from the arm pose measured when
speech starts, so nothing jumps; at the end the arms return there and the controller
takes back over.

G1 sign conventions (MuJoCo FK on the real walk-mode rest pose, 2026-10-07):
  shoulder pitch − → hand forward/up · elbow − → forearm up/forward (elbow + goes BACK)
  shoulder roll: left + / right − → out to the side. Poses below are written for the
  LEFT arm; the right arm uses the mirror (roll, yaw and wrist roll/yaw flip sign).
Every pose was checked in MuJoCo for arm–body / arm–arm contact with a 3 cm margin.
Stroke durations stretch automatically so no joint exceeds ~85% of MAX_VEL.

Safety: every command goes through teleop.safety.SafetyFilter (joint limits, MAX_VEL),
from the g1-qtm-teleop repo (pip install -e ../g1-qtm-teleop).
Yields to the built-in arm actions (wave, handshake…) and to the `x` arm stop, and lets go
for good if the torso tilts or spins (balance watchdog: TILT_LIMIT, GYRO_LIMIT).

TalkMotion is pure (no robot I/O) so scripts/preview_talk_gestures.py can render it.
"""

import math
import random
import re
import threading
import time

import numpy as np

# ── poses: per-arm offsets from rest, LEFT-arm convention (rad) ───────────────
# order: shoulder pitch, shoulder roll (+ out), shoulder yaw, elbow, wrist roll, pitch, yaw
def _p(sp=0.0, sr=0.0, sy=0.0, el=0.0):
    return np.array([sp, sr, sy, el, 0.0, 0.0, 0.0])

READY = _p(sp=-0.25, sr=0.08, el=-0.55)           # hands up in front between strokes
SWAY_AMP = _p(sp=0.04, el=0.06)
SWAY_HZ = (0.31, 0.47)

# style → strokes: (arm, delay s, rise s, hold s, fall s, pose)
#   arm: "one" (alternates L/R), "same" (as the previous stroke), "right", "both";
#   pose is absolute (offset from rest), blended from the READY posture and back.
STYLES = {
    "explain":  [("one", 0.0, 0.50, 0.00, 0.55, READY + _p(sp=-0.22, sr=0.15, el=-0.35))],
    # right hand 20 cm in front of the chest centre; turns the upper arm (yaw) instead of
    # rolling it inward, which put the shoulder into the torso. ≥3 cm clearance in MuJoCo.
    "self":     [("right", 0.0, 1.40, 0.70, 1.10, _p(sp=-0.80, sr=0.20, sy=-0.90, el=-1.40))],
    "open":     [("both", 0.0, 0.70, 0.60, 0.70, _p(sp=-0.15, sr=0.40, el=-0.25))],
    "you":      [("one", 0.0, 0.60, 0.50, 0.70, _p(sp=-0.55, sr=0.05, el=0.10))],
    "list":     [("one", 0.0, 0.30, 0.00, 0.30, READY + _p(sp=-0.12, el=-0.22)),
                 ("same", 0.75, 0.30, 0.00, 0.30, READY + _p(sp=-0.12, el=-0.22)),
                 ("same", 1.50, 0.30, 0.00, 0.30, READY + _p(sp=-0.12, el=-0.22))],
    "wide":     [("both", 0.0, 0.80, 0.50, 0.80, _p(sp=-0.10, sr=0.55, el=-0.15))],
    "shrug":    [("both", 0.0, 0.70, 0.60, 0.70, _p(sp=-0.20, sr=0.30, el=-0.55))],
    "fistbump": [("right", 0.0, 1.20, 2.00, 1.20, _p(sp=-1.00, el=-0.15))],
}

# sentence words → style (first match wins; checked in this order)
STYLE_RULES = [
    ("self",  r"\b(i'?m yotie|my name|i am yotie|i'?m a (humanoid|robot|unitree)|i am a (humanoid|robot))\b"),
    ("shrug", r"\b(not sure|don'?t know|maybe|might be|it depends|hard to say|i can'?t say|no idea)\b"),
    ("list",  r"\b(first|second|third|also|another|next|finally|then we)\b"),
    ("wide",  r"\b(all|every|everyone|everything|whole|huge|many|lots|world|big)\b"),
    ("open",  r"\b(welcome|thank|thanks|glad|nice to meet|great to|hello|hi there|good to see)\b|\?$"),
    ("you",   r"^(you|your)\b|\b(you can|you'll|you might|you could|for you)\b"),
]

ENGAGE_S = 1.0                  # ease in / out of the READY posture
EXTRA_BEAT_GAP_S = (1.8, 2.8)   # explain beats inside long sentences
# Roll / yaw / wrist roll / wrist yaw flip sign between the arms.
_MIRROR = np.array([1, -1, -1, 1, -1, 1, -1])

ARM_JOINTS = list(range(15, 29))        # left 15–21, right 22–28 (motor order)
WAIST_JOINTS = [12, 13, 14]             # held where they were (as in Unitree's example)
WEIGHT_SLOT = 29
CONTROL_DT = 0.02                       # 50 Hz
# First robot run (2026-10-08, v2 at full size, 1.5 rad/s, kd 1.5): the walk controller
# stepped backward and the arms shook. Moves are now half size and slower, with more damping.
KP, KD = 60.0, 3.0                      # kp as Unitree's arm_sdk example; kd as xr_teleoperate
# Hand-back to the walk controller. At 0.5 s the robot stepped backward right after talking
# (gantry, 2026-10-08); xr_teleoperate takes 2 s and Unitree's arm7 example 5 s.
WEIGHT_RAMP_S = 2.0                     # normal hand-back, after the pose is back at rest
YIELD_RAMP_S = 0.5                      # faster hand-back: built-in gesture, arm stop, balance stop
MAX_VEL = 1.0                           # rad/s, via SafetyFilter
AMPLITUDE = 0.5                         # scales every pose (and so every speed)
# Balance watchdog: if the torso tilts or rotates this much while we hold the arms, the walk
# controller is struggling — hand the arms back and stay off for the session (like `x`).
TILT_LIMIT = 0.10                       # rad change in torso roll/pitch since speech start
GYRO_LIMIT = 0.8                        # rad/s torso angular speed


def pick_style(text: str) -> str:
    lower = text.lower().strip()
    for style, pattern in STYLE_RULES:
        if re.search(pattern, lower):
            return style
    return "explain"


def _smoothstep(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


class TalkMotion:
    """Arm offsets (14,) as a function of time, driven by speech events."""

    def __init__(self, amplitude: float = AMPLITUDE, seed: int | None = None):
        self.amp = amplitude
        self.rng = random.Random(seed)
        self.reset()

    def reset(self):
        self.speaking = False
        self.t_begin = self.t_end = None
        # strokes: (start, rise, hold, fall, arm index 0/1, pose (7,), left-arm convention)
        self.strokes: list[tuple] = []
        self.busy_until = [0.0, 0.0]
        self.next_extra = None
        self.next_arm = self.rng.randint(0, 1)
        self.last_style = None
        self.phase = (self.rng.uniform(0, 6.28), self.rng.uniform(0, 6.28))

    def begin(self, t: float):
        self.reset()
        self.speaking, self.t_begin = True, t

    def resume(self, t: float):
        """New speech while the last reply's arms are still settling: keep going from the
        current posture (no reset, so nothing jumps) instead of starting over."""
        e = self.engaged(t)
        lo, hi = 0.0, 1.0                       # invert smoothstep: find x with smoothstep(x) = e
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if _smoothstep(mid) < e else (lo, mid)
        self.t_begin = t - lo * ENGAGE_S
        self.t_end, self.speaking, self.last_style = None, True, None

    def sentence(self, t: float, text: str = "", style: str | None = None):
        """A sentence starts playing: one stroke in its style, extra beats while it lasts."""
        if not self.speaking:
            return
        style = style or pick_style(text)
        if style == self.last_style and style not in ("explain", "fistbump") and self.rng.random() < 0.5:
            style = "explain"           # don't repeat the same big move sentence after sentence
        self.last_style = style
        # a cue (fist bump) starts right away; others wait until the hands are up
        start = t if style == "fistbump" else max(t, self.t_begin + ENGAGE_S)
        self._play(style, start)
        self.next_extra = max(self.busy_until) + self.rng.uniform(*EXTRA_BEAT_GAP_S)

    def end(self, t: float):
        if self.speaking:
            self.speaking, self.t_end, self.next_extra = False, t, None

    def _play(self, style: str, t: float):
        arm = self.next_arm
        for who, delay, rise, hold, fall, pose in STYLES[style]:
            # long moves get more time: smoothstep peaks at 1.5× the average speed
            dist = max(np.abs(pose - READY).max(), np.abs(pose).max()) + 0.08    # + sway
            min_t = 1.5 * dist / (0.85 * MAX_VEL)
            rise, fall = max(rise, min_t), max(fall, min_t)
            arms = {"one": [arm], "same": [arm], "right": [1], "both": [0, 1]}[who]
            start = t + delay
            if any(self.busy_until[a] > start for a in arms):
                continue                # that arm is still moving: never stack strokes
            for a in arms:
                self.strokes.append((start, rise, hold, fall, a, pose))
                self.busy_until[a] = start + rise + hold + fall
        self.next_arm = 1 - arm

    def _t_leave(self) -> float:
        # don't leave while a stroke (e.g. the fist-bump hold) is still playing
        return max(self.t_end, *self.busy_until)

    def engaged(self, t: float) -> float:
        if self.t_begin is None:
            return 0.0
        up = _smoothstep((t - self.t_begin) / ENGAGE_S)
        if self.t_end is None:
            return up
        return up * (1 - _smoothstep((t - self._t_leave()) / ENGAGE_S))

    def done(self, t: float) -> bool:
        return self.t_end is not None and t - self._t_leave() >= ENGAGE_S

    def offsets(self, t: float) -> np.ndarray:
        if self.speaking and self.next_extra is not None and t >= self.next_extra \
                and all(b <= t for b in self.busy_until):
            self._play("explain", t)
            self.next_extra = t + self.rng.uniform(*EXTRA_BEAT_GAP_S)
        e = self.engaged(t)
        arms = []
        for a in (0, 1):
            ph = self.phase if a == 0 else self.phase[::-1]
            sway = SWAY_AMP * np.array([math.sin(2 * math.pi * SWAY_HZ[0] * t + ph[0]), 0, 0,
                                        math.sin(2 * math.pi * SWAY_HZ[1] * t + ph[1]), 0, 0, 0])
            base = (READY + sway) * e
            pose = base
            for start, rise, hold, fall, arm, target in self.strokes:
                if arm != a or not start <= t <= start + rise + hold + fall:
                    continue
                s = t - start
                k = (_smoothstep(s / rise) if s < rise else
                     1.0 if s < rise + hold else
                     1 - _smoothstep((s - rise - hold) / fall))
                pose = base + (target - base) * k
            arms.append(pose)
        self.strokes = [s for s in self.strokes if t <= s[0] + s[1] + s[2] + s[3]]
        return self.amp * np.concatenate([arms[0], arms[1] * _MIRROR])


class TalkGestures:
    """Runs TalkMotion on the robot through rt/arm_sdk at 50 Hz.

    begin() / sentence() / end() come from the speech loop. busy() returns True while a
    built-in arm action plays; talking gestures then hand the arms back and sit out the
    rest of that reply. stop() is the arm stop: arms back, disabled for the session.
    """

    def __init__(self, enabled: bool, busy=lambda: False, amplitude: float = AMPLITUDE):
        self.enabled = enabled
        self.busy = busy
        self.motion = TalkMotion(amplitude)
        self._lock = threading.Lock()
        self._state = None          # latest LowState_
        self._weight = 0.0
        self._active = False        # publishing (engaged or releasing)
        self._yielded = False       # sat out this reply for a built-in gesture
        self._rest = None           # 29 joint positions measured at begin()
        self._rpy0 = None           # torso roll/pitch measured at begin()

    def init(self):
        if not self.enabled:
            return
        try:
            from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
            from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
            from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
            from unitree_sdk2py.utils.crc import CRC
            from teleop.safety import SafetyFilter

            self._cmd = unitree_hg_msg_dds__LowCmd_()
            self._crc = CRC()
            self._safety = SafetyFilter(dt=CONTROL_DT, max_vel=MAX_VEL)
            self._pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
            self._pub.Init()
            self._sub = ChannelSubscriber("rt/lowstate", LowState_)
            self._sub.Init(self._on_state, 10)
            threading.Thread(target=self._loop, daemon=True, name="talk_gestures").start()
            print("[talk] talking gestures enabled (rt/arm_sdk)")
        except Exception as exc:
            print(f"[talk] could not start ({exc}); talking gestures disabled")
            self.enabled = False

    def _on_state(self, msg):
        self._state = msg

    # ── speech events ────────────────────────────────────────────────────────
    def begin(self):
        if not self.enabled:
            return
        with self._lock:
            if self._active:
                if not self._yielded:   # still settling from the last reply: carry on from here
                    self.motion.resume(time.monotonic())
                return
            if self._state is None:
                print("[talk] no rt/lowstate yet (robot off or not in walk mode?); skipping")
                return
            if self.busy():
                return                  # a built-in gesture owns the arms this reply
            self._rest = np.array([self._state.motor_state[i].q for i in range(29)])
            self._rpy0 = np.array(self._state.imu_state.rpy[:2], dtype=float)
            self._safety.reset(self._rest)
            self._yielded = False
            self.motion.begin(time.monotonic())
            self._active = True

    def sentence(self, text: str = "", style: str | None = None):
        """A sentence starts playing; its words pick the movement (or a cue like 'fistbump')."""
        if self._active and not self._yielded:
            self.motion.sentence(time.monotonic(), text, style)

    def end(self):
        if self._active:
            self.motion.end(time.monotonic())

    def stop(self):
        """Arm stop: release now and stay off for the rest of the session."""
        with self._lock:
            self.enabled = False
            if self._active:
                self.motion.end(time.monotonic())
                self._yielded = True

    # ── 50 Hz control loop ───────────────────────────────────────────────────
    def _loop(self):
        next_t = time.monotonic()
        while True:
            next_t += CONTROL_DT
            try:
                self._tick()
            except Exception as exc:
                print(f"[talk] control error ({exc}); releasing arms")
                self._release_now()
            time.sleep(max(0.0, next_t - time.monotonic()))

    def _tick(self):
        with self._lock:
            if not self._active:
                return
            now = time.monotonic()
            if self.enabled and not self._yielded and not self._balance_ok():
                self.enabled = False        # stay off for the session, like the `x` arm stop
            if (self.busy() or not self.enabled) and not self._yielded:
                self._yielded = True        # built-in gesture or arm stop: hand back fast
                self.motion.end(now)
            if self._yielded:
                target_w, offsets = 0.0, np.zeros(14)
            else:
                offsets = self.motion.offsets(now)
                target_w = 0.0 if self.motion.done(now) else 1.0
            step = CONTROL_DT / (YIELD_RAMP_S if self._yielded else WEIGHT_RAMP_S)
            # Take the arms instantly (pose = measured, so no jump); give them back gently.
            # (was `target_w > self._weight`: at weight 1 that dropped a step every other tick,
            # so the arms flickered between us and the walk controller at 25 Hz)
            self._weight = 1.0 if target_w > 0.0 else max(0.0, self._weight - step)

            target = self._rest.copy()
            target[ARM_JOINTS] += offsets
            q = self._safety(target)
            self._publish(q, self._weight)
            if self._weight <= 0.0 and (self._yielded or self.motion.done(now)):
                self._active = False

    def _balance_ok(self) -> bool:
        imu = self._state.imu_state
        tilt = np.abs(np.array(imu.rpy[:2], dtype=float) - self._rpy0).max()
        spin = float(np.linalg.norm(np.array(imu.gyroscope, dtype=float)))
        if tilt > TILT_LIMIT or spin > GYRO_LIMIT:
            print(f"[talk] BALANCE STOP — torso tilt {tilt:.2f} rad, spin {spin:.2f} rad/s; "
                  "releasing arms, talking gestures off for this session")
            return False
        return True

    def _publish(self, q: np.ndarray, weight: float):
        for j in ARM_JOINTS + WAIST_JOINTS:
            c = self._cmd.motor_cmd[j]
            c.q, c.dq, c.tau, c.kp, c.kd = float(q[j]), 0.0, 0.0, KP, KD
        self._cmd.motor_cmd[WEIGHT_SLOT].q = float(weight)
        self._cmd.crc = self._crc.Crc(self._cmd)
        self._pub.Write(self._cmd)

    def _release_now(self):
        try:
            self._cmd.motor_cmd[WEIGHT_SLOT].q = 0.0
            self._cmd.crc = self._crc.Crc(self._cmd)
            self._pub.Write(self._cmd)
        finally:
            self._active, self._weight = False, 0.0
