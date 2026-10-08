"""
stage_moves.py  -  whole-body stage gestures and stage moves for scripts/stage_demo.py

A reply from Claude carries marks that are never spoken:
  gestures  "Welcome [welcome] everyone. This [point] diagram ..."   arms + waist, land on a word
  moves     "[walk_left] Let me come over here."                       walking, done before speaking

Pure logic, no robot, network or audio code, so all of it can be checked on the VM:
  split_marks(text)                   -> (spoken text, [(gesture, char position)], [move names])
  time_marks(marks, words, spoken)    -> [{"name": gesture, "time": seconds into the audio}]
  GestureTimeline(rest, cues, speech) -> .at(t): 17 targets (waist 3 + arms 14) at t seconds
  Stage(...)                          -> dead-reckoned position; clips moves to the stage box

The 8 teaching gestures and their arm angles come from the team's
robogesture-w/teaching-gestures branch (scripts/gesture_server.py CUES, which were
collision-checked there); the marks parsing follows its teleop/gesture_cues.py.
"""

import math
import re

import numpy as np

# ── joints ───────────────────────────────────────────────────────────────────
# Targets in this module are 17 values: waist yaw, roll, pitch, then the left arm
# and right arm (shoulder pitch, roll, yaw, elbow, wrist roll, pitch, yaw), i.e.
# G1 motor indexes 12..28 in order.
UPPER_JOINTS = tuple(range(12, 29))
WAIST = slice(0, 3)
LEFT = slice(3, 10)
RIGHT = slice(10, 17)

# Joint limits (rad) from the G1 29-DoF model.
ARM_LIMITS = [
    (-3.0892, 2.6704), (-1.5882, 2.2515), (-2.618, 2.618), (-1.0472, 2.0944),
    (-1.97222, 1.97222), (-1.61443, 1.61443), (-1.61443, 1.61443),
    (-3.0892, 2.6704), (-2.2515, 1.5882), (-2.618, 2.618), (-1.0472, 2.0944),
    (-1.97222, 1.97222), (-1.61443, 1.61443), (-1.61443, 1.61443),
]
# The waist is kept to small moves while standing: it shares the balance job with
# the legs. Roll is never used.
WAIST_LIMITS = [(-0.35, 0.35), (0.0, 0.0), (0.0, 0.12)]
LIMIT_MARGIN = 0.05
LOWER = np.array([lo for lo, _ in WAIST_LIMITS] + [lo + LIMIT_MARGIN for lo, _ in ARM_LIMITS])
UPPER = np.array([hi for _, hi in WAIST_LIMITS] + [hi - LIMIT_MARGIN for _, hi in ARM_LIMITS])

MAX_SPEED = 1.0        # rad/s, arm joints, at the fastest point of a move into or out of a pose
WAIST_SPEED = 0.4      # rad/s, waist: slower, it shares the balance job with the legs
# Hard per-joint speed caps the robot loop enforces on every command (team's arm limit: 1.5 rad/s).
# The wave and "yes" bobs peak at 1.4 and 0.85 rad/s, under the arm cap.
SPEED_CAP = np.array([0.5] * 3 + [1.5] * 14)


def clamp(q: np.ndarray) -> np.ndarray:
    return np.clip(q, LOWER, UPPER)


def mirror(angles):
    """Left arm angles as the same pose on the right arm (or the other way round)."""
    return tuple(a * s for a, s in zip(angles, (1, -1, -1, 1, -1, 1, -1)))


# ── gestures ─────────────────────────────────────────────────────────────────
# "left"/"right": 7 arm angles; an arm not listed stays at rest.
# "waist": (yaw, roll, pitch) offsets from rest. "stay": seconds held on the word.
# "early": seconds to arrive before the word. "bob": (arm, joint 0-6, size rad, Hz).
_ONE_HAND = (0.15, 0.34, 0.18, -0.02, 0.0, 0.03, 0.13)
_POINT = (-0.55, 0.34, 0.90, 0.13, 0.0, 0.07, 0.11)
_SMALL = (-0.31, 0.36, -0.30, -0.29, 0.0, 0.08, 0.04)
_BIG = (0.07, 0.34, 0.65, 0.03, 0.0, 0.05, 0.13)
_ASK = (-0.05, 0.34, 0.17, -0.14, 0.0, 0.05, 0.08)
_WELCOME = (-0.35, 0.40, 0.45, 0.10, 0.0, 0.0, 0.0)
_SHRUG = (-0.10, 0.25, 0.75, -0.25, 0.0, 0.0, 0.0)
# right arm raised out to the side; the wave swings shoulder yaw outward of this, never toward
# the head (checked on the model: hand stays 25-33 cm right of the head centerline)
_WAVE = (-1.00, -0.80, -0.40, 0.10, 0.0, 0.0, 0.0)

GESTURES = {
    # from the team's teaching gestures
    "yes":         {"right": (-0.27, -0.34, 0.18, -0.01, 0.0, 0.03, -0.07), "bob": ("right", 3, 0.09, 1.5), "stay": 1.4},
    "point":       {"left": _POINT, "waist": (0.15, 0.0, 0.0), "early": 0.6},   # to the robot's left
    "point_right": {"right": mirror(_POINT), "waist": (-0.15, 0.0, 0.0), "early": 0.6},
    "one_hand":    {"left": _ONE_HAND},
    "other_hand":  {"right": mirror(_ONE_HAND)},
    "small":       {"left": _SMALL, "right": mirror(_SMALL)},
    "big":         {"left": _BIG, "right": mirror(_BIG)},
    "ask":         {"left": _ASK, "right": mirror(_ASK)},
    # whole-body presenter gestures
    "welcome":     {"left": _WELCOME, "right": mirror(_WELCOME), "waist": (0.0, 0.0, 0.06), "stay": 1.2},
    "wave":        {"right": _WAVE, "bob": ("right", 2, 0.22, 1.0), "stay": 2.0},
    "bow":         {"waist": (0.0, 0.0, 0.12), "stay": 1.0},
    "think":       {"left": (-0.40, 0.80, -1.08, -0.22, 0.0, -0.10, 0.0),
                    "right": (-0.41, -0.30, 0.42, -0.53, 0.0, -0.19, 0.0), "stay": 1.5},
    "shrug":       {"left": _SHRUG, "right": mirror(_SHRUG), "stay": 1.0},
    "face_left":   {"left": _ONE_HAND, "waist": (0.30, 0.0, 0.0), "stay": 1.5},
    "face_right":  {"right": mirror(_ONE_HAND), "waist": (-0.30, 0.0, 0.0), "stay": 1.5},
}

IN_SECONDS = 0.8       # shortest way into a gesture
STAY_SECONDS = 0.7
OUT_SECONDS = 1.2
EARLY_SECONDS = 0.2    # arrive just before the word, as people do
EASE_PEAK = 1.875      # peak speed of the quintic ease, as a multiple of distance / time

# small "talking" motion while speaking, so the arms don't look frozen
BEAT_SIZE = 0.05       # rad
BEAT_HZ = 0.9
BEAT_FADE = 0.6        # seconds to fade the beat in and out


def ease(r):
    """0 to 1 with no sudden start or stop in speed or acceleration."""
    r = np.clip(r, 0.0, 1.0)
    return r * r * r * (r * (r * 6.0 - 15.0) + 10.0)


# ── stage moves ──────────────────────────────────────────────────────────────
# Body frame: x forward, y left, yaw counter-clockwise. (x m, y m, yaw rad) per move.
MOVES = {
    "walk_left":    (0.0, 0.30, 0.0),
    "walk_right":   (0.0, -0.30, 0.0),
    "step_forward": (0.25, 0.0, 0.0),
    "step_back":    (-0.20, 0.0, 0.0),
    "turn_left":    (0.0, 0.0, 0.35),
    "turn_right":   (0.0, 0.0, -0.35),
    "center":       None,   # back to the start, facing the front
}


# ── marks ────────────────────────────────────────────────────────────────────
MAX_GESTURES = 4
MAX_MOVES = 1
_ANY_MARK = re.compile(r"\[([a-z_]{2,20})\]\s*", re.IGNORECASE)


def split_marks(text: str):
    """Take the marks out of `text`. Unknown marks are removed too, so a mistaken
    one is never read aloud. Returns (spoken, [(gesture, position)], [moves])."""
    gestures, moves, spoken, at = [], [], [], 0
    for found in _ANY_MARK.finditer(text):
        spoken.append(text[at:found.start()])
        name = found.group(1).lower()
        if name in GESTURES and len(gestures) < MAX_GESTURES:
            gestures.append((name, sum(len(part) for part in spoken)))
        elif name in MOVES and len(moves) < MAX_MOVES:
            moves.append(name)
        at = found.end()
    spoken.append(text[at:])
    joined = "".join(spoken)
    clean = re.sub(r"\s+([,.!?;:])", r"\1", joined.strip())
    clean = re.sub(r"\s{2,}", " ", clean)
    # positions shift with the clean-up; map each mark to the same word in `clean`
    marks = [(name, _word_offset(clean, len(joined[:position].split()))) for name, position in gestures]
    return clean, marks, moves


def _word_offset(text: str, index: int) -> int:
    """Character offset of word number `index` in `text` (or the end)."""
    for n, found in enumerate(re.finditer(r"\S+", text)):
        if n == index:
            return found.start()
    return len(text)


def time_marks(marks, words, spoken):
    """When each gesture's word is spoken. `words` is [(start seconds, word)] from the
    speech synthesizer, in order. With no word times, words are spread evenly over
    `estimate_seconds(spoken)`."""
    if not marks:
        return []
    if not words:
        words = estimated_words(spoken)
    lowered, at, placed = spoken.lower(), 0, []
    for start, word in words:
        position = lowered.find(word.lower().strip(), at)
        if position < 0:
            position = at
        placed.append((position, float(start)))
        at = position + max(1, len(word.strip()))
    cues = []
    for name, position in marks:
        when = next((start for where, start in placed if where >= position), placed[-1][1])
        cues.append({"name": name, "time": round(when, 3)})
    return cues


WORDS_PER_SECOND = 2.6


def estimate_seconds(spoken: str) -> float:
    return max(1.0, len(spoken.split()) / WORDS_PER_SECOND)


def estimated_words(spoken: str):
    return [(i / WORDS_PER_SECOND, w) for i, w in enumerate(spoken.split())]


# ── gesture timeline ─────────────────────────────────────────────────────────
class GestureTimeline:
    """Targets for the waist and arms over one utterance. t = 0 is the first sound.

    rest: the 17 measured joint values when the arms were taken (the pose the robot's
    own controller had them in). Each gesture eases from rest to its pose, stays on
    its word, and eases back; overlapping gestures on the same arm are queued."""

    def __init__(self, rest, cues, speech_seconds: float, beat: bool = True):
        self.rest = np.asarray(rest, dtype=float)
        self.speech = float(speech_seconds)
        self.beat = beat
        self.plan = []
        busy = {"left": 0.0, "right": 0.0, "waist": 0.0}
        for cue in sorted(cues, key=lambda c: c["time"]):
            spec = GESTURES[cue["name"]]
            target = self.target(spec)
            moved = np.abs(target - self.rest)
            far = max(float(moved[3:].max()) / MAX_SPEED, float(moved[WAIST].max()) / WAIST_SPEED)
            way_in = max(IN_SECONDS, EASE_PEAK * far)
            parts = [p for p in ("left", "right", "waist") if p in spec]
            arrive = max(cue["time"] - spec.get("early", EARLY_SECONDS), way_in,
                         max(busy[p] for p in parts) + way_in)
            leave = arrive + spec.get("stay", STAY_SECONDS)
            way_out = max(OUT_SECONDS, EASE_PEAK * far)
            end = leave + way_out
            for p in parts:
                busy[p] = end
            self.plan.append({"name": cue["name"], "spec": spec, "target": target,
                              "start": arrive - way_in, "arrive": arrive, "leave": leave, "end": end})
        last = max([g["end"] for g in self.plan], default=0.0)
        self.duration = max(self.speech + 0.3, last)

    def target(self, spec) -> np.ndarray:
        q = self.rest.copy()
        if "left" in spec:
            q[LEFT] = spec["left"]
        if "right" in spec:
            q[RIGHT] = spec["right"]
        if "waist" in spec:
            q[WAIST] = self.rest[WAIST] + np.asarray(spec["waist"])
        return clamp(q)

    def weight(self, g, t: float) -> float:
        if t <= g["start"] or t >= g["end"]:
            return 0.0
        if t < g["arrive"]:
            return float(ease((t - g["start"]) / (g["arrive"] - g["start"])))
        if t <= g["leave"]:
            return 1.0
        return float(1.0 - ease((t - g["leave"]) / (g["end"] - g["leave"])))

    def at(self, t: float) -> np.ndarray:
        q = self.rest.copy()
        groups = {"waist": WAIST, "left": LEFT, "right": RIGHT}
        for part, sl in groups.items():
            active = [(self.weight(g, t), g) for g in self.plan if part in g["spec"]]
            active = [(w, g) for w, g in active if w > 0.0]
            total = sum(w for w, _ in active)
            scale = 1.0 / total if total > 1.0 else 1.0
            for w, g in active:
                offset = g["target"][sl] - self.rest[sl]
                bob = g["spec"].get("bob")
                if bob and bob[0] == part and g["arrive"] <= t <= g["leave"]:
                    _, joint, size, hz = bob
                    through = (t - g["arrive"]) / max(1e-6, g["leave"] - g["arrive"])
                    offset = offset.copy()
                    offset[joint] += size * math.sin(math.pi * through) ** 2 * math.sin(2 * math.pi * hz * (t - g["arrive"]))
                q[sl] += w * scale * offset
            if self.beat and part != "waist" and 0.0 < t < self.speech:
                free = 1.0 - min(1.0, total)
                fade = min(1.0, t / BEAT_FADE, (self.speech - t) / BEAT_FADE)
                phase = 0.0 if part == "left" else 1.3
                b = BEAT_SIZE * free * fade * math.sin(2 * math.pi * BEAT_HZ * t + phase)
                q[sl.start + 0] += b          # shoulder pitch
                q[sl.start + 3] -= 0.8 * b    # elbow
        return clamp(q)


# ── stage position ───────────────────────────────────────────────────────────
class Stage:
    """Where the robot is, from the moves it was told to make (no sensors, so it
    drifts: someone still watches the robot). Start = (0, 0), facing the audience."""

    def __init__(self, box: float = 0.5, max_yaw: float = 0.6, speed: float = 0.15,
                 turn_speed: float = 0.3, max_seconds: float = 3.0):
        self.box, self.max_yaw = box, max_yaw
        self.speed, self.turn_speed, self.max_seconds = speed, turn_speed, max_seconds
        self.x = self.y = self.yaw = 0.0

    def plan(self, name: str):
        """The velocity commands for a named move, clipped to the stage box:
        [(vx, vy, vyaw, seconds)], possibly empty if there is no room."""
        if MOVES.get(name) is None:
            return self.center()
        dx, dy, dyaw = MOVES[name]
        return self._clip(dx, dy, dyaw)

    def center(self):
        steps = []
        if abs(self.yaw) > 0.05:
            steps += self._velocity(0.0, 0.0, -self.yaw)
        # after turning back to yaw 0, the body frame is the stage frame
        if math.hypot(self.x, self.y) > 0.03:
            steps += self._velocity(-self.x, -self.y, 0.0)
        return steps

    def _clip(self, dx, dy, dyaw):
        if dyaw:
            new_yaw = min(max(self.yaw + dyaw, -self.max_yaw), self.max_yaw)
            dyaw = new_yaw - self.yaw
            return self._velocity(0.0, 0.0, dyaw) if abs(dyaw) > 0.05 else []
        # stage-frame displacement, then shrink it until the end point is inside the box
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        wx, wy = c * dx - s * dy, s * dx + c * dy
        scale = 1.0
        for pos, d in ((self.x, wx), (self.y, wy)):
            if d > 0:
                scale = min(scale, max(0.0, (self.box - pos) / d))
            elif d < 0:
                scale = min(scale, max(0.0, (-self.box - pos) / d))
        if scale * math.hypot(dx, dy) < 0.05:
            return []
        return self._velocity(dx * scale, dy * scale, 0.0)

    def _velocity(self, dx, dy, dyaw):
        """Split a body-frame displacement into commands within the speed and time limits."""
        if dyaw:
            total = abs(dyaw) / self.turn_speed
        else:
            total = math.hypot(dx, dy) / self.speed
        chunks = max(1, math.ceil(total / self.max_seconds))
        seconds = total / chunks
        return [(dx / total, dy / total, dyaw / total, seconds)] * chunks if total > 0 else []

    def apply(self, vx, vy, vyaw, seconds):
        """Record a command as done (straight line, then turn: moves are one or the other)."""
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        self.x += (c * vx - s * vy) * seconds
        self.y += (s * vx + c * vy) * seconds
        self.yaw += vyaw * seconds

    def map(self, width: int = 21) -> str:
        """A one-line picture of the stage: | . . R . . | (left of the audience's view is
        the robot's right, so this is drawn from behind the robot)."""
        cells = ["."] * width
        i = round((-self.y + self.box) / (2 * self.box) * (width - 1))
        cells[min(max(i, 0), width - 1)] = "R"
        return (f"stage (from behind): |{''.join(cells)}|  x={self.x:+.2f} m  y={self.y:+.2f} m  "
                f"yaw={math.degrees(self.yaw):+.0f} deg")
