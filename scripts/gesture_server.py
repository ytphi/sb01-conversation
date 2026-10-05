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
                  "legs": [12], "waist": [3], "arms": [14]}   (measured, rad)
  ->  newline-delimited JSON, flushed as each line is ready:
        {"hold": [14 arm q], "fps": 30}        sent at once, before any model work
        {"frames": [[14 arm q + weight] x 30]} one per second of speech
        {"frames": [...]}                      return to the hold pose, then release
        {"done": true, ...timing and collision stats}
      or {"error": "..."} if generation fails part-way.
  The client takes the arms at the hold pose (they do not move) while the first
  block is generated. The first frame of motion belongs to the first sample of
  audio; the last frames ramp the weight back to 0.
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
        return pcm, legs, waist, arms

    def stream(self, pcm, legs, waist, arms):
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
        if self.path != "/gesture":
            self._reply(404, {"error": "unknown path"})
            return
        try:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            args = self.planner.parse(request)
        except (KeyError, ValueError, TypeError) as exc:
            self._reply(400, {"error": str(exc)})
            return

        # No Content-Length: the stream ends when the connection closes.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            for message in self.planner.stream(*args):
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
