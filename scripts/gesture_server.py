#!/usr/bin/env python3
"""
gesture_server.py  -  RoboGesture sidecar for sb01_conversation.py

Turns one utterance of TTS audio into a collision-checked arm trajectory,
streamed one second at a time so the robot can start speaking after the first
block instead of waiting for the whole utterance.
Owns the GPU and the RoboGesture models; never touches DDS or the robot.

sb01 has the stock rigid hands, not the BrainCo Revo2 hands RoboGesture was
trained with, so finger motion is discarded: hands are pinned open in the
collision model (as a stand-in for the rigid hand) and only the 14 arm joints
plus the arm_sdk blend weight are returned.

Run with RoboGesture's own interpreter, not the sb01 one:
  ~/RoboGesture/.venv/bin/python scripts/gesture_server.py [--host H] [--port P]

Protocol:
  POST /gesture  {"pcm": base64 PCM16LE mono 24 kHz,
                  "legs": [12], "waist": [3], "arms": [14],   (measured, rad)
                  "cues": [{"name": from CUES, "time": seconds into the audio}]}  (optional)
  ->  newline-delimited JSON, flushed as each line is ready:
        {"hold": [14 arm q], "fps": 30}        sent at once, before any model work
        {"frames": [[14 arm q + weight] x 30]} one per second of speech
        {"frames": [...]}                      return to the hold pose, then release
        {"done": true, ...timing and collision stats}
      or {"error": "..."} if generation fails part-way.
  The client takes the arms at the hold pose (they do not move) while the first
  block is generated. The first frame of motion belongs to the first sample of
  audio; the last frames ramp the weight back to 0.

  POST /pose     {"pose": name from POSES, "seconds": how long to stay there,
                  "legs": [12], "waist": [3], "arms": [14]}
  ->  the same kind of stream, without the model: move to the pose and stay
      there, swaying slightly if the pose has a sway. The first line also has
      "path": how many of the frames are the way there. Every frame goes
      through the same collision filter. The client plays the way there in
      reverse to go back, then releases.
"""

import argparse
import base64
import contextlib
import io
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import torch

from robogesture.collision import CollisionFilter
from robogesture.config import (
    ARMS,
    AUDIO_RATE,
    HANDS,
    MOTION_DIM,
    MOTION_RATE,
    RELEASE_FRAMES,
    RESTORE_FRAMES,
    TAKEOVER_FRAMES,
    WAIST,
    WEIGHT_INDEX,
)
from robogesture.model import MotionGenerator
from robogesture.motion import (
    _interpolate,
    _smoothstep,
    model_to_robot,
    smooth_model_blocks,
)

MAX_AUDIO_SECONDS = 30.0
SAMPLER_STEPS = 50

# Fixed poses, played without the model. Arm angles in rad: shoulder pitch, roll,
# yaw, elbow, wrist roll, pitch, yaw. An arm that is not listed stays where it is.
# The collision model has no separate head: the body is one box that also covers
# the head, except its top 3 cm. Poses keep well clear of the head by design.
POSES = {
    # The thinker: right hand raised to just under the chin, left forearm across
    # the body with the hand in front of the right elbow. Measured on the full
    # mesh model, every gap (head, torso, other arm) is 7 cm or more.
    "thinking": {"left":  (-0.40, 0.80, -1.08, -0.22, 0.0, -0.10, 0.0),
                 "right": (-0.41, -0.30, 0.42, -0.53, 0.0, -0.19, 0.0)},
}
# Small movement while staying in a pose, so it does not look frozen:
# (arm, joint 0-6, size in rad, where in the cycle it starts as a fraction).
# The G1 has no joints that lift or roll the shoulders themselves and the waist
# is left alone for balance, so "shifting the shoulders" is the two shoulder
# pitch joints rocking in opposite directions, with a little roll and yaw.
POSE_SWAY = {
    "thinking": (("left", 0, 0.06, 0.0), ("right", 0, 0.06, 0.5),
                 ("left", 1, 0.03, 0.25), ("right", 2, 0.04, 0.25)),
}
POSE_SWAY_HZ = 0.4            # one full shift every 2.5 s
POSE_SWAY_EASE_SECONDS = 0.6  # the sway grows from nothing and fades out again
POSE_ARMS = {"left": ARMS[:7], "right": ARMS[7:]}
POSE_MAX_SECONDS = 10.0
POSE_STEP = 0.045         # rad per frame at the fastest point of the move (the limit is 0.05)
POSE_SETTLE_FRAMES = 10   # at the pose, through the filter, before it is simply held
POSE_BLOCK_FRAMES = 150   # frames per message


def _mirror(angles):
    """Left arm angles as the same pose on the right arm, or the other way round."""
    return tuple(a * sign for a, sign in zip(angles, (1, -1, -1, 1, -1, 1, -1)))


# Teaching gestures ("cues"): a pose with a meaning, laid over the generated
# motion so that it arrives on a chosen word and then melts back into it. Arms
# only; the waist is never moved. Every frame still goes through the collision
# filter. Angles as in POSES; elbows are kept out from the body.
_ONE_HAND = (0.15, 0.34, 0.18, -0.02, 0.0, 0.03, 0.13)
_POINT = (-0.55, 0.34, 0.90, 0.13, 0.0, 0.07, 0.11)
_SMALL = (-0.31, 0.36, -0.30, -0.29, 0.0, 0.08, 0.04)
_BIG = (0.07, 0.34, 0.65, 0.03, 0.0, 0.05, 0.13)
_ASK = (-0.05, 0.34, 0.17, -0.14, 0.0, 0.05, 0.08)
CUES = {
    # "Yes!" / "Correct.": the right hand reaches forward and bobs gently twice.
    "yes":         {"right": (-0.27, -0.34, 0.18, -0.01, 0.0, 0.03, -0.07), "bob": ("right", 3, 0.09), "stay": 1.4},
    # A long reach that the collision filter slows, so it sets off earlier.
    "point":       {"left": _POINT, "early": 0.6},      # to something on the robot's left
    "point_right": {"right": _mirror(_POINT), "early": 0.6},
    "one_hand":    {"left": _ONE_HAND},                 # "on one hand ..."
    "other_hand":  {"right": _mirror(_ONE_HAND)},       # "... on the other hand"
    "small":       {"left": _SMALL, "right": _mirror(_SMALL)},   # hands close together
    "big":         {"left": _BIG, "right": _mirror(_BIG)},       # hands wide apart
    "ask":         {"left": _ASK, "right": _mirror(_ASK)},       # open to the room: "any questions?"
}
CUE_MAX = 12              # per utterance
CUE_STRENGTH = 0.85       # how much of the arm the cue takes; the rest keeps the speech rhythm
CUE_SPEED = 0.04          # rad per frame at the fastest point of the way in (the limit is 0.05)
CUE_IN_SECONDS = 0.9      # shortest way in; longer when the pose is far away
CUE_STAY_SECONDS = 0.7
CUE_OUT_SECONDS = 1.3
CUE_EARLY_SECONDS = 0.2   # aim to be there just before the word, as people do
CUE_FAR_MARGIN = 0.25     # rad added to the distance: the arms are rarely still at rest
CUE_BOB_HZ = 1.5


def _ease(ratio):
    """0 to 1 with no sudden start or stop in speed or acceleration."""
    r = np.clip(ratio, 0.0, 1.0)
    return r * r * r * (r * (r * 6.0 - 15.0) + 10.0)


def plan_cues(cues, hold):
    """When each cue fades in, stays and fades out, in frames from the start of
    the audio. A pose that is far from where the arms start gets a longer way
    in, and a cue too early to be reached gently arrives late instead of fast."""
    plan = []
    for cue in sorted(cues, key=lambda c: c["time"]):
        spec = CUES[cue["name"]]
        far = max(float(np.abs(np.asarray(spec[side]) - hold[list(POSE_ARMS[side])]).max())
                  for side in POSE_ARMS if side in spec) + CUE_FAR_MARGIN
        way_in = max(round(CUE_IN_SECONDS * MOTION_RATE), int(np.ceil(1.875 * far / CUE_SPEED)))
        early = spec.get("early", CUE_EARLY_SECONDS)
        arrive = max(round((cue["time"] - early) * MOTION_RATE), way_in)
        for earlier in plan:              # the same arm is not asked for two poses at once
            if any(side in spec and side in CUES[earlier["name"]] for side in POSE_ARMS):
                arrive = max(arrive, earlier["leave"] + way_in // 2)
        stay = round((spec.get("stay", CUE_STAY_SECONDS) + early) * MOTION_RATE)   # still there on the word
        plan.append({"name": cue["name"], "start": arrive - way_in, "arrive": arrive, "leave": arrive + stay,
                     "end": arrive + stay + round(CUE_OUT_SECONDS * MOTION_RATE)})
    return plan


def lay_cues(gesture, first_frame, plan):
    """Blend the planned cues into frames [first_frame, first_frame + len) of the
    generated motion, in place."""
    index = first_frame + np.arange(len(gesture), dtype=np.float32)
    for side, joints in POSE_ARMS.items():
        joints = list(joints)
        weights, poses = [], []
        for cue in plan:
            spec = CUES[cue["name"]]
            if side not in spec or cue["end"] <= index[0] or cue["start"] > index[-1]:
                continue
            weight = np.where(index < cue["arrive"],
                              _ease((index - cue["start"]) / max(1, cue["arrive"] - cue["start"])),
                              1.0 - _ease((index - cue["leave"]) / max(1, cue["end"] - cue["leave"])))
            pose = np.repeat(np.asarray(spec[side], dtype=np.float32)[None], len(gesture), axis=0)
            if spec.get("bob", (None,))[0] == side:
                _, joint, size = spec["bob"]
                through = np.clip((index - cue["arrive"]) / max(1, cue["leave"] - cue["arrive"]), 0.0, 1.0)
                seconds = (index - cue["arrive"]) / MOTION_RATE
                pose[:, joint] += size * np.sin(np.pi * through) ** 2 * np.sin(2.0 * np.pi * CUE_BOB_HZ * seconds)
            weights.append(CUE_STRENGTH * weight)
            poses.append(pose)
        if not weights:
            continue
        total = np.sum(weights, axis=0)
        scale = np.where(total > CUE_STRENGTH, CUE_STRENGTH / np.maximum(total, 1e-6), 1.0)
        blended = gesture[:, joints] * (1.0 - total * scale)[:, None]
        for weight, pose in zip(weights, poses):
            blended += (weight * scale)[:, None] * pose
        gesture[:, joints] = blended


@torch.inference_mode()
def stream_motion(generator: MotionGenerator, pcm: bytes):
    """MotionGenerator.generate(), yielding each 30-frame block as it is sampled.

    Same sampler and seed. The audio and motion-history conditioning does not
    depend on the denoising step, so it is encoded once per block rather than
    once per step as in MotionModel.forward.
    """
    model, device = generator.model, generator.device
    torch.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    mimi_cache = None
    audio_cache = torch.zeros(1, 25, 2, dtype=torch.long, device=device)
    motion_cache = torch.zeros(1, 60, 41, device=device)
    # Two seconds of silence are model history and never become audible.
    pending = bytearray(bytes(sum(generator.CHUNK_SAMPLES) * 2))
    pending.extend(pcm)
    index = 0
    while pending or index < 3:
        sample_count = generator.CHUNK_SAMPLES[index % 2]
        size = sample_count * 2
        raw = bytes(pending[:size])
        del pending[:size]
        samples = np.zeros(sample_count, dtype=np.float32)
        decoded = np.frombuffer(raw, dtype="<i2")
        samples[: len(decoded)] = decoded.astype(np.float32) / 32768.0
        waveform = torch.from_numpy(samples).reshape(1, 1, -1).to(device)
        encoded = generator.mimi.encode(
            waveform, padding_cache=mimi_cache, num_quantizers=2, use_streaming=True
        )
        mimi_cache = encoded.padding_cache
        tokens = encoded.audio_codes.transpose(1, 2)
        if index < 2:
            motion = torch.zeros(1, 30, 41, device=device)
        else:
            window = torch.cat((audio_cache, tokens), dim=1)
            if window.shape[1] < 38:
                padding = torch.zeros(
                    1, 38 - window.shape[1], 2, dtype=torch.long, device=device
                )
                window = torch.cat((padding, window), dim=1)
            audio = model.encode_audio(window)
            semantic, _ = model.semantic(audio["high_seq"])
            cond_low = model.audio_proj(audio["full"])
            cond_high = model.motion_proj(model.motion_enc(motion_cache))
            cond_first = model.audio_first_proj(audio["first_layer"])
            x_t = torch.randn(1, 30, 41, device=device)
            for step in torch.linspace(0.0, 0.98, SAMPLER_STEPS, device=device):
                generated = model.generator(
                    model.pos_enc(model.dit_input_proj(x_t)),
                    cond_low=cond_low,
                    cond_high=cond_high,
                    cond_audio_first=cond_first,
                    film_low_emb=semantic,
                    film_high_emb=semantic,
                    t_emb=model.t_embedder(step.expand(1)),
                )
                x_t = x_t + model.final_proj(generated) / SAMPLER_STEPS
            motion = x_t.clamp(-5.0, 5.0)
            yield model.denormalize_motion(motion)[0].cpu().numpy()
        audio_cache = torch.cat((audio_cache, tokens), dim=1)[:, -25:]
        motion_cache = torch.cat((motion_cache, motion), dim=1)[:, -60:]
        index += 1


class RigidHandFilter(CollisionFilter):
    """CollisionFilter fed block by block, with the fingers sb01 lacks frozen."""

    def _frame(self, requested, fixed=()):
        # Never let the solver 'fix' a collision by moving fingers.
        return super()._frame(requested, fixed=HANDS)

    def begin(self, legs, measured_frame):
        """Same reset as CollisionFilter.filter(), before the first block."""
        self.legs = np.asarray(legs, dtype=np.float64).copy()
        self.last_control = self.last_frame = None
        self.last_delta.fill(0.0)
        baseline = np.asarray(measured_frame, dtype=np.float64).copy()
        baseline[WEIGHT_INDEX] = 0.0
        self._frame(baseline)
        self.counts = {"safe": 0, "corrected": 0, "held": 0}
        self.minimum_clearance = float("inf")

    def check(self, frames) -> np.ndarray:
        output = np.empty_like(np.asarray(frames), dtype=np.float32)
        for index, requested in enumerate(frames):
            frame, distances, status = self._frame(requested)
            output[index] = frame
            self.counts[status] += 1
            clearance = float(np.min(distances - self.safe_distances))
            self.minimum_clearance = min(self.minimum_clearance, clearance)
        return output


class GesturePlanner:
    def __init__(self, device="cuda"):
        print("[gesture] loading Mimi and the motion checkpoint...", flush=True)
        with contextlib.redirect_stdout(io.StringIO()):   # RoboGesture logs in Chinese
            self.generator = MotionGenerator(device=device)
        self.collision = RigidHandFilter()
        # First CUDA call is slow; pay for it here, not on the first utterance.
        for _ in stream_motion(self.generator, bytes(AUDIO_RATE * 2)):
            pass

    @staticmethod
    def parse(request: dict):
        pcm = base64.b64decode(request["pcm"])
        if not pcm or len(pcm) % 2:
            raise ValueError("pcm must be nonempty PCM16LE")
        if len(pcm) / 2 / AUDIO_RATE > MAX_AUDIO_SECONDS:
            raise ValueError(f"audio longer than {MAX_AUDIO_SECONDS:.0f}s")
        legs = np.asarray(request["legs"], dtype=np.float64)
        waist = np.asarray(request["waist"], dtype=np.float32)
        arms = np.asarray(request["arms"], dtype=np.float32)
        if legs.shape != (12,) or waist.shape != (3,) or arms.shape != (14,):
            raise ValueError("expected legs[12], waist[3], arms[14]")
        if not all(np.isfinite(a).all() for a in (legs, waist, arms)):
            raise ValueError("measured pose must be finite")
        cues = request.get("cues") or []
        if not isinstance(cues, list) or len(cues) > CUE_MAX:
            raise ValueError(f"cues must be a list of at most {CUE_MAX}")
        for cue in cues:
            if not isinstance(cue, dict) or cue.get("name") not in CUES:
                raise ValueError(f"unknown cue; known: {sorted(CUES)}")
            if type(cue.get("time")) not in (int, float) or not 0.0 <= cue["time"] <= MAX_AUDIO_SECONDS:
                raise ValueError("cue time must be seconds into the audio")
        return pcm, legs, waist, arms, cues

    def stream(self, pcm, legs, waist, arms, cues=()):
        """Yield protocol messages (dicts) for one utterance."""
        started = time.perf_counter()
        # Measured pose; hands stay 0 (open) everywhere.
        listening = np.zeros(MOTION_DIM, dtype=np.float32)
        listening[list(WAIST)] = waist
        listening[list(ARMS)] = arms
        self.collision.begin(legs, listening)
        # Where the filter wants the arms before any motion: the measured pose,
        # nudged only if that pose is itself inside a safety margin. The client
        # holds this pose at full weight while the first block is generated.
        hold = np.asarray(self.collision.last_frame, dtype=np.float32)
        yield {"hold": hold[list(ARMS)].tolist(), "fps": MOTION_RATE}

        def emit(frames):
            checked = self.collision.check(frames)
            return {"frames": checked[:, list(ARMS) + [WEIGHT_INDEX]].tolist()}

        total = 0
        first_ms = None
        previous_raw = last = None
        plan = plan_cues(cues, hold)
        played = 0                      # frames of generated motion so far
        for block in stream_motion(self.generator, pcm):
            raw = model_to_robot(block)
            if previous_raw is None:
                gesture = raw.copy()
            else:
                # The bridge over a block boundary only rewrites the new block.
                gesture = smooth_model_blocks(np.concatenate((previous_raw, raw)))[
                    len(previous_raw):
                ]
            previous_raw = raw
            gesture[:, list(WAIST)] = listening[list(WAIST)]
            gesture[:, list(HANDS)] = 0.0
            if plan:
                lay_cues(gesture, played, plan)
            played += len(gesture)
            if last is None:
                # Ease from the hold pose into the gesture during the first
                # second of speech, so the arms start moving as the voice does
                # instead of a second before it.
                count = min(TAKEOVER_FRAMES, len(gesture))
                blend = _smoothstep(TAKEOVER_FRAMES)[:count, None]
                arms = list(ARMS)
                gesture[:count, arms] = (1.0 - blend) * hold[arms] + blend * gesture[:count, arms]
            message = emit(gesture)
            if first_ms is None:
                first_ms = round((time.perf_counter() - started) * 1000)
            last = gesture[-1]
            total += len(message["frames"])
            yield message

        restore = _interpolate(last, listening, RESTORE_FRAMES)
        restore[:, WEIGHT_INDEX] = 1.0
        release = np.repeat(listening[None], RELEASE_FRAMES, axis=0)
        release[:, WEIGHT_INDEX] = 1.0 - _smoothstep(RELEASE_FRAMES)
        message = emit(np.concatenate((restore, release)))
        total += len(message["frames"])
        yield message
        yield {
            "done": True,
            "frames_total": total,
            "first_ms": first_ms,
            "total_ms": round((time.perf_counter() - started) * 1000),
            **self.collision.counts,
            "minimum_clearance": self.collision.minimum_clearance,
        }

    @staticmethod
    def parse_pose(request: dict):
        name = request["pose"]
        if name not in POSES:
            raise ValueError(f"unknown pose {name!r}; known: {sorted(POSES)}")
        seconds = float(request.get("seconds", 0.0))
        if not 0.0 <= seconds <= POSE_MAX_SECONDS:
            raise ValueError(f"seconds must be 0 to {POSE_MAX_SECONDS:.0f}")
        legs = np.asarray(request["legs"], dtype=np.float64)
        waist = np.asarray(request["waist"], dtype=np.float32)
        arms = np.asarray(request["arms"], dtype=np.float32)
        if legs.shape != (12,) or waist.shape != (3,) or arms.shape != (14,):
            raise ValueError("expected legs[12], waist[3], arms[14]")
        if not all(np.isfinite(a).all() for a in (legs, waist, arms)):
            raise ValueError("measured pose must be finite")
        return name, seconds, legs, waist, arms

    def pose(self, name, seconds, legs, waist, arms):
        """Yield protocol messages for one fixed pose: the way there, then stay.
        The client goes back along the same frames, so there is no way back here."""
        started = time.perf_counter()
        listening = np.zeros(MOTION_DIM, dtype=np.float32)
        listening[list(WAIST)] = waist
        listening[list(ARMS)] = arms
        self.collision.begin(legs, listening)
        hold = np.asarray(self.collision.last_frame, dtype=np.float32)

        start = listening.copy()
        start[list(ARMS)] = hold[list(ARMS)]
        start[WEIGHT_INDEX] = 1.0
        target = start.copy()
        for side, angles in POSES[name].items():
            target[list(POSE_ARMS[side])] = angles
        # A smoothstep moves 1.5 times its average speed at its fastest point.
        count = max(MOTION_RATE // 2, int(np.ceil(1.5 * np.abs(target - start).max() / POSE_STEP)) + 1)
        yield {"hold": hold[list(ARMS)].tolist(), "fps": MOTION_RATE, "path": count + POSE_SETTLE_FRAMES}

        # There: through the filter, which may stop short of the pose to keep clear.
        there = np.concatenate((_interpolate(start, target, count),
                                np.repeat(target[None], POSE_SETTLE_FRAMES, axis=0)))
        checked = self.collision.check(there)
        first_ms = round((time.perf_counter() - started) * 1000)
        # Stay: the last checked frame, repeated, plus the pose's sway if it has one.
        stay = np.repeat(checked[-1:], round(seconds * MOTION_RATE), axis=0)
        if len(stay) and name in POSE_SWAY:
            clock = np.arange(len(stay), dtype=np.float32) / MOTION_RATE
            ease = np.clip(np.minimum(clock, clock[-1] - clock) / POSE_SWAY_EASE_SECONDS, 0.0, 1.0)
            ease = ease * ease * (3.0 - 2.0 * ease)
            for side, joint, size, phase in POSE_SWAY[name]:
                stay[:, POSE_ARMS[side][joint]] += ease * size * np.sin(
                    2.0 * np.pi * (POSE_SWAY_HZ * clock + phase))
            stay = self.collision.check(stay)
        columns = list(ARMS) + [WEIGHT_INDEX]
        total = 0
        for part in (checked, stay):
            for offset in range(0, len(part), POSE_BLOCK_FRAMES):
                total += len(part[offset:offset + POSE_BLOCK_FRAMES])
                yield {"frames": part[offset:offset + POSE_BLOCK_FRAMES, columns].tolist()}
        yield {
            "done": True,
            "frames_total": total,
            "first_ms": first_ms,
            "total_ms": round((time.perf_counter() - started) * 1000),
            **self.collision.counts,
            "minimum_clearance": self.collision.minimum_clearance,
        }


class Handler(BaseHTTPRequestHandler):
    planner: GesturePlanner

    def _reply(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _line(self, message: dict):
        self.wfile.write(json.dumps(message).encode() + b"\n")
        self.wfile.flush()

    def do_GET(self):
        self._reply(200, {"status": "ok"})

    def do_POST(self):
        routes = {"/gesture": (self.planner.parse, self.planner.stream),
                  "/pose": (self.planner.parse_pose, self.planner.pose)}
        if self.path not in routes:
            self._reply(404, {"error": "unknown path"})
            return
        parse, stream = routes[self.path]
        try:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            args = parse(request)
        except (KeyError, ValueError, TypeError) as exc:
            self._reply(400, {"error": str(exc)})
            return

        # No Content-Length: the stream ends when the connection closes.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            for message in stream(*args):
                self._line(message)
                if message.get("done"):
                    print(
                        f"[gesture] {message['frames_total']} frames, "
                        f"first block {message['first_ms']}ms, "
                        f"total {message['total_ms']}ms, "
                        f"corrected={message['corrected']} held={message['held']}",
                        flush=True,
                    )
        except (BrokenPipeError, ConnectionResetError):
            print("[gesture] client disconnected mid-utterance", flush=True)
        except Exception as exc:
            print(f"[gesture] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                self._line({"error": f"{type(exc).__name__}: {exc}"})
            except OSError:
                pass

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()

    Handler.planner = GesturePlanner(device=args.device)
    server = HTTPServer((args.host, args.port), Handler)
    print(f"[gesture] ready on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[gesture] shutting down")


if __name__ == "__main__":
    main()
