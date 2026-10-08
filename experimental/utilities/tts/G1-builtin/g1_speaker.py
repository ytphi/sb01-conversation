"""
g1_speaker.py  -  play 16 kHz mono int16 PCM through the G1 speaker (AudioClient.PlayStream)

Playback is a queue: begin() opens one stream id for the whole reply, play()
adds audio as each sentence is synthesized, finish() waits for the end. A
sender thread pushes chunks back to back, never more than `lead_s` ahead of
real time, so there is no fixed sleep between chunks and the end of speech is
timed from the total audio queued.

Also wraps the LED ring so the conversation loop can show listening / thinking / speaking.
"""

import queue
import threading
import time

import numpy as np
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

from tts_common import to_pcm16

PCM_SAMPLE_RATE = 16000
BYTES_PER_S     = PCM_SAMPLE_RATE * 2

LED_OFF       = (0, 0, 0)
LED_LISTENING = (0, 128, 0)
LED_THINKING  = (128, 128, 0)
LED_SPEAKING  = (0, 0, 128)

_END = object()


class G1Speaker:
    def __init__(self, app_name: str = "sb01", volume: int = 100,
                 playback_grace_s: float = 0.5, echo_tail_s: float = 0.3,
                 chunk_s: float = 1.0, lead_s: float = 1.5):
        self.app_name      = app_name
        self.volume        = volume
        self.playback_grace_s = playback_grace_s   # max wait past the audio's length for play_state 0
        self.echo_tail_s      = echo_tail_s        # extra deaf time for the room echo to die
        self.chunk_bytes   = int(chunk_s * BYTES_PER_S) // 2 * 2
        self.lead_s        = lead_s                # how far ahead of real time chunks may be sent
        self.audio         = AudioClient()
        self.speaking      = threading.Event()   # shared with speech input (deaf while set)
        self.playback_done = threading.Event()   # set by G1ASRInput on play_state == 0
        self._q: queue.Queue | None = None
        self._sender: threading.Thread | None = None
        self._timer = None
        self._clock_start = 0.0                   # time the stream started playing
        self._sent_s = 0.0                        # seconds of audio sent so far

    def start(self):
        self.audio.SetTimeout(10.0)
        self.audio.Init()
        self.audio.SetVolume(self.volume)

    def led(self, rgb: tuple[int, int, int]):
        self.audio.LedControl(*rgb)

    # ── streaming playback (AudioPlayer interface) ──────────────────────────

    def begin(self, timer=None):
        self.speaking.set()
        self.playback_done.clear()
        self._timer = timer
        self._q = queue.Queue()
        self._sent_s = 0.0
        self._clock_start = 0.0
        stream_id = str(int(time.time() * 1000))
        self._sender = threading.Thread(target=self._send_loop, args=(stream_id, self._q),
                                        daemon=True, name="g1-playback")
        self._sender.start()

    def play(self, wav: np.ndarray, sr: int):
        self._q.put(to_pcm16(wav, sr, PCM_SAMPLE_RATE))

    def finish(self):
        try:
            if self._q is not None:
                self._q.put(_END)
                self._sender.join()
            if self._sent_s > 0:
                # play_state 0 isn't guaranteed for PlayStream, so don't wait much past
                # the audio's own length
                expected_end = self._clock_start + self._sent_s + self.playback_grace_s
                self.playback_done.wait(timeout=max(expected_end - time.time(), 0.0))
                time.sleep(self.echo_tail_s)
        finally:
            self._q = None
            self.led(LED_OFF)
            self.speaking.clear()

    def _send_loop(self, stream_id: str, q: queue.Queue):
        first = True
        pending = b""
        while True:
            item = q.get()
            if item is _END:
                if pending:
                    self._send(stream_id, pending, first)
                return
            pending += item
            while len(pending) >= self.chunk_bytes:
                chunk, pending = pending[:self.chunk_bytes], pending[self.chunk_bytes:]
                self._send(stream_id, chunk, first)
                first = False
            if pending and q.empty():         # nothing else waiting: don't hold audio back
                self._send(stream_id, pending, first)
                first, pending = False, b""

    def _send(self, stream_id: str, chunk: bytes, first: bool):
        now = time.time()
        if first:
            self.led(LED_SPEAKING)
            self._clock_start = now
            if self._timer:
                self._timer.mark("first_audio_played", now)
        else:
            played_until = self._clock_start + self._sent_s
            if now > played_until:              # ran dry (TTS slower than speech): restart the clock
                self._clock_start = now - self._sent_s
            else:
                wait = played_until - now - self.lead_s
                if wait > 0:
                    time.sleep(wait)
        self.audio.PlayStream(self.app_name, stream_id, list(chunk))
        self._sent_s += len(chunk) / BYTES_PER_S

    # ── one-shot helper ─────────────────────────────────────────────────────

    def play_pcm(self, pcm: bytes):
        """Play one finished clip (16 kHz mono int16)."""
        self.begin()
        self._q.put(pcm)
        self.finish()
