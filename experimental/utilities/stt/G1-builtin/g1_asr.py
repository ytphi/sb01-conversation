"""
g1_asr.py  -  speech input from the G1's onboard ASR (DDS topic rt/audio_msg)

Messages are JSON in a String_:
  {"text": ..., "is_final": bool, "emotion": "<|HAPPY|>", ...}   ASR result
  {"play_state": 1 | 0}                                           robot playback start / finish

Partials are debounced (debounce_s, 0.8 s by default) because is_final sometimes never arrives.
Anything heard while the robot is speaking is dropped so it doesn't hear itself.
"""

import json
import queue
import threading
import time

from unitree_sdk2py.core.channel import ChannelSubscriber
from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_

ASR_TOPIC = "rt/audio_msg"


class G1ASRInput:
    def __init__(self, speaking: threading.Event, playback_done: threading.Event,
                 topic: str = ASR_TOPIC, debounce_s: float = 0.8, min_chars: int = 2):
        self.speaking      = speaking
        self.playback_done = playback_done
        self.topic         = topic
        self.debounce_s    = debounce_s
        self.min_chars     = min_chars
        self._queue: queue.Queue[tuple[str, float]] = queue.Queue()
        self.last_speech_end = self.last_turn_end = self.last_transcribed = None
        self._pending = ""
        self._timer: threading.Timer | None = None
        self._sub = None

    def start(self):
        self._sub = ChannelSubscriber(self.topic, String_)
        self._sub.Init(self._callback, 10)
        print(f"[asr] subscribed to {self.topic}")

    def listen(self) -> str | None:
        """Block until the next utterance. Drops anything queued while speaking."""
        while True:
            text, t_final = self._queue.get()
            if not self.speaking.is_set():
                # the robot decides the end of turn itself; we only see when it reported it
                self.last_speech_end, self.last_turn_end = None, t_final
                self.last_transcribed = t_final
                return text
            print(f"[asr] (dropped late ASR: {text!r})")

    def flush(self):
        """Discard anything heard so far (call after the robot finishes speaking)."""
        while not self._queue.empty():
            self._queue.get_nowait()

    # ── DDS callback ─────────────────────────────────────────────────────────

    def _callback(self, msg: String_):
        try:
            data = json.loads(msg.data)
        except (json.JSONDecodeError, AttributeError):
            return

        if "play_state" in data:
            if data["play_state"] == 0:
                self.playback_done.set()
            return

        if self.speaking.is_set():
            return

        text = data.get("text", "").strip()
        if len(text) < self.min_chars:
            return

        if self._timer:
            self._timer.cancel()
        if data.get("is_final", False):
            self._pending = ""
            self._queue.put((text, time.time()))
        else:
            self._pending = text
            self._timer = threading.Timer(self.debounce_s, self._flush_pending)
            self._timer.daemon = True
            self._timer.start()

    def _flush_pending(self):
        text, self._pending = self._pending, ""
        if text and not self.speaking.is_set():
            self._queue.put((text, time.time()))
