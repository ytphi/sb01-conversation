"""
stt_common.py  -  shared pieces for speech recognition on the PC

  STTEngine      - interface every engine implements: transcribe(audio) → text
  AudioSource    - anything that delivers 16 kHz int16 PCM (mono, or interleaved multi-channel)
  PcMicSource    - the PC's own microphone (sounddevice)
  LocalASRInput  - SpeechInput: audio source → Silero VAD end-of-turn → STT engine

Unlike the G1's onboard ASR (fixed ~1-2 s silence before is_final), the end of a
turn is decided here, after `min_silence_ms` of silence.
"""

import queue
import threading
import time
from abc import ABC, abstractmethod
from collections import deque

import numpy as np

SAMPLE_RATE = 16000
VAD_FRAME   = 512                    # samples per Silero VAD step at 16 kHz (32 ms)


class STTEngine(ABC):
    name = "base"

    @abstractmethod
    def load(self):
        """Load model weights (called lazily on first use, or eagerly at startup)."""

    @abstractmethod
    def transcribe(self, audio: np.ndarray) -> str:
        """audio: mono float32 in [-1, 1] at 16 kHz. Returns the text ('' if nothing usable)."""


class AudioSource(ABC):
    """Pushes 16 kHz int16 PCM bytes (any chunk size) into self.chunks.

    `channels` > 1 means interleaved frames (e.g. a future stereo or array mic).
    Speech recognition reads channel 0; the other channels are passed through
    untouched for later use (direction of arrival, Phase 6).
    """
    name = "base"
    hint = ""
    channels = 1

    def __init__(self):
        self.chunks: queue.Queue[bytes] = queue.Queue()

    @abstractmethod
    def start(self):
        ...


class PcMicSource(AudioSource):
    name = "pc-mic"
    hint = "check the PC's default input device (or set stt.pc_mic.device)"

    def __init__(self, device=None):
        super().__init__()
        self.device  = device
        self._stream = None

    def start(self):
        import sounddevice as sd
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=VAD_FRAME,
            device=self.device, callback=lambda data, *_: self.chunks.put(bytes(data)))
        self._stream.start()
        print(f"[stt] listening on PC microphone ({self.device or 'default'})")


class LocalASRInput:
    """SpeechInput that finds the end of each utterance and transcribes it on the PC.

    A background thread runs Silero VAD over the audio in real time. Audio heard
    while the robot is speaking (and any utterance still queued) is discarded so
    it doesn't hear itself.
    """

    def __init__(self, source: AudioSource, engine: STTEngine, speaking: threading.Event,
                 threshold: float = 0.5, min_silence_ms: int = 500, preroll_ms: int = 300,
                 max_utterance_s: float = 20.0, min_chars: int = 2):
        self.source         = source
        self.engine         = engine
        self.speaking       = speaking
        self.threshold      = threshold
        self.min_silence_ms = min_silence_ms
        self.preroll_frames = max(1, int(preroll_ms / 1000 * SAMPLE_RATE / VAD_FRAME))
        self.max_frames     = int(max_utterance_s * SAMPLE_RATE / VAD_FRAME)
        self.min_chars      = min_chars
        self._utterances: queue.Queue[tuple[np.ndarray, float]] = queue.Queue()
        self._vad = None
        self.heard_audio      = threading.Event()   # set once the source delivers anything
        self.last_speech_end  = None                # time.time() the user stopped talking (estimate)
        self.last_turn_end    = None                # time.time() VAD decided the turn was over
        self.last_transcribed = None                # time.time() when its transcript was ready

    def start(self):
        from silero_vad import VADIterator, load_silero_vad
        self.engine.load()
        self._vad = VADIterator(load_silero_vad(), threshold=self.threshold,
                                sampling_rate=SAMPLE_RATE,
                                min_silence_duration_ms=self.min_silence_ms)
        self.source.start()
        threading.Thread(target=self._segment_loop, daemon=True).start()
        print(f"[stt] {self.engine.name} on {self.source.name}, "
              f"end of turn after {self.min_silence_ms} ms of silence")

    def listen(self) -> str | None:
        """Block until the next utterance and return its transcript."""
        while True:
            audio, t_end = self._utterances.get()
            t0 = time.time()
            text = self.engine.transcribe(audio).strip()
            print(f"[timing] stt {time.time() - t0:.2f}s for {len(audio) / SAMPLE_RATE:.1f}s of audio")
            if len(text) >= self.min_chars:
                self.last_turn_end    = t_end
                self.last_speech_end  = t_end - self.min_silence_ms / 1000
                self.last_transcribed = time.time()
                return text

    def flush(self):
        """Discard utterances that haven't been transcribed yet."""
        while not self._utterances.empty():
            self._utterances.get_nowait()

    # ── background segmentation ─────────────────────────────────────────────

    def _frames(self):
        """Re-chunk the source's PCM into VAD-sized float32 frames (channel 0 only)."""
        channels    = max(1, getattr(self.source, "channels", 1))
        frame_bytes = VAD_FRAME * 2 * channels
        buf = bytearray()
        warned = False
        while True:
            try:
                chunk = self.source.chunks.get(timeout=5.0)
            except queue.Empty:
                if not warned:
                    print(f"[stt] no audio from {self.source.name} for 5 s - {self.source.hint}")
                    warned = True
                continue
            warned = False
            self.heard_audio.set()
            buf.extend(chunk)
            while len(buf) >= frame_bytes:
                frame = np.frombuffer(bytes(buf[:frame_bytes]), dtype=np.int16)
                del buf[:frame_bytes]
                if channels > 1:
                    frame = frame.reshape(-1, channels)[:, 0]
                yield frame.astype(np.float32) / 32768.0

    def _segment_loop(self):
        import torch
        preroll: deque[np.ndarray] = deque(maxlen=self.preroll_frames)
        speech: list[np.ndarray] | None = None

        for frame in self._frames():
            if self.speaking.is_set():
                if speech is not None or preroll:
                    speech = None
                    preroll.clear()
                    self._vad.reset_states()
                self.flush()
                continue

            event = self._vad(torch.from_numpy(frame), return_seconds=False)
            if speech is None:
                preroll.append(frame)     # VAD fires a little late; keep the lead-in
                if event and "start" in event:
                    speech = list(preroll)
                continue

            speech.append(frame)
            if (event and "end" in event) or len(speech) >= self.max_frames:
                self._utterances.put((np.concatenate(speech), time.time()))
                speech = None
                preroll.clear()
                self._vad.reset_states()
