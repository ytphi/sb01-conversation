"""
timing_log.py  -  per-turn latency log (JSONL, timings only: never any text)

One line per turn in logs/turns.jsonl (config: conversation.timing_log), e.g.

  {"turn": 3, "ts": 1791406968.2, "speech_end": 0.0, "turn_detected": 0.5,
   "transcript_ready": 0.64, "llm_first_token": 1.2, "first_audio_ready": 1.6,
   "first_audio_played": 1.62, "playback_end": 4.9, "sentences": 2, "audio_s": 3.4,
   "voice": "teto", "stream": true}

Every stamp is seconds after speech_end, which is estimated as the moment VAD
ended the turn minus vad.min_silence_ms (or the first stamp when the turn came
from the keyboard). Missing stamps are left out.
"""

import json
import os
import threading
import time

STAGES = ("speech_end", "turn_detected", "transcript_ready", "llm_first_token", "llm_done",
          "first_audio_ready", "first_audio_played", "playback_end")


class TurnTimer:
    def __init__(self, turn: int, log: "TimingLog | None" = None):
        self.turn   = turn
        self.log    = log
        self.marks: dict[str, float] = {}
        self.extra: dict = {}
        self._lock  = threading.Lock()

    def mark(self, stage: str, t: float | None = None, once: bool = True) -> float:
        t = time.time() if t is None else t
        with self._lock:
            if not (once and stage in self.marks):
                self.marks[stage] = t
        return t

    def since(self, stage: str = "speech_end") -> float | None:
        t0 = self.marks.get(stage) or (min(self.marks.values()) if self.marks else None)
        return None if t0 is None else time.time() - t0

    def add(self, **extra):
        with self._lock:
            for key, value in extra.items():
                if isinstance(value, (int, float)) and isinstance(self.extra.get(key), (int, float)):
                    self.extra[key] += value
                else:
                    self.extra[key] = value

    def record(self) -> dict:
        with self._lock:
            t0 = self.marks.get("speech_end") or (min(self.marks.values()) if self.marks else time.time())
            rec = {"turn": self.turn, "ts": round(t0, 3)}
            for stage in STAGES:
                if stage in self.marks:
                    rec[stage] = round(self.marks[stage] - t0, 3)
            rec.update(self.extra)
        return rec

    def finish(self) -> dict:
        rec = self.record()
        if self.log:
            self.log.write(rec)
        return rec

    def summary(self) -> str:
        rec = self.record()
        parts = [f"{k.replace('_', ' ')} {rec[k]:.2f}s" for k in STAGES[1:] if k in rec]
        return " | ".join(parts)


class TimingLog:
    def __init__(self, path: str | None):
        self.path  = path
        self.turns = 0
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def new_turn(self) -> TurnTimer:
        self.turns += 1
        return TurnTimer(self.turns, self)

    def write(self, record: dict):
        if not self.path:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
