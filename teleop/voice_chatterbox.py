"""
voice_chatterbox.py  -  the Chatterbox voice for scripts/sb01_conversation.py

Uses the speech framework in experimental/speech-framework as it is: the same
engines, the same config.yaml (and config.local.yaml), the same voice reference
clips. This file only adapts it to what the conversation program needs:

  ChatterboxVoice().load()                    load the models once, at startup
  .synthesize(text, language, ...) -> Speech  audio for the robot's speaker, the
                                              same audio for the gesture model,
                                              and estimated word times

Chatterbox does not report when each word is spoken. Word times here are
ESTIMATES: each sentence's start and length are measured from its audio, and
words are spread across the spoken part of the sentence by their position in
the text. They are good enough to bring a gesture to the right part of a
sentence, not to the exact word.

Every failure (package or model missing, not enough GPU memory, unusable
audio) is raised as VoiceUnavailable with a plain reason, so the caller can
say it and fall back to its other voice.

Settings:
  SB01_CHATTERBOX_DEVICE   auto | cuda | cpu   overrides tts.device in config.yaml
"""

import importlib
import os
import re
import sys
from dataclasses import dataclass, field

import numpy as np

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FRAMEWORK_DIR = os.path.join(REPO_ROOT, "experimental", "speech-framework")
INSTALL_HINT = "pip install -r experimental/speech-framework/requirements.txt"

PLAYBACK_RATE = 16000       # what the robot's speaker takes
GESTURE_RATE = 24000        # what the gesture model takes
MIN_SECONDS = 0.15          # shorter than this is not speech
MAX_SECONDS = 60.0          # longer than this for one reply is a runaway generation
SENTENCE_GAP_SECONDS = 0.12   # pause put between sentences synthesized one by one
QUIET = 0.02                # of the loudest sample: below this is silence

_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+|[。！？]+")
_CJK = re.compile(r"[㐀-鿿]")
_WORD = re.compile(r"[㐀-鿿]|[^\W_]+(?:['’][^\W_]+)*")


class VoiceUnavailable(Exception):
    """Chatterbox cannot be used right now; the message says why in plain words."""


@dataclass
class Speech:
    pcm: bytes                          # 16 kHz mono 16-bit, for the robot
    gesture_pcm: bytes | None           # 24 kHz mono 16-bit, for the gesture model
    seconds: float
    words: list = field(default_factory=list)   # [(start seconds, word)], estimated


def sentences(text: str) -> list[str]:
    """The sentences of `text`, in order, with nothing left out."""
    pieces, at = [], 0
    for match in _SENTENCE_END.finditer(text):
        piece = text[at:match.end()].strip()
        if len(piece) >= 12 or _CJK.search(piece):      # "Yes." stays with what follows
            pieces.append(piece)
            at = match.end()
    rest = text[at:].strip()
    if rest:
        pieces.append(rest)
    return pieces or [text.strip()]


def _reason(exc: Exception) -> str:
    """Why loading or speaking failed, for a person."""
    name, text = type(exc).__name__, " ".join(str(exc).split())[:200]
    if isinstance(exc, ImportError):
        missing = getattr(exc, "name", None) or text
        return f"a Python package is missing ({missing}). Install it with: {INSTALL_HINT}"
    if "out of memory" in text.lower():
        return ("the graphics card ran out of memory. The gesture server may be using it; "
                "set SB01_CHATTERBOX_DEVICE=cpu or run the gesture server on another machine")
    if isinstance(exc, (OSError, FileNotFoundError)) or "huggingface" in text.lower() or "snapshot" in text.lower():
        return f"a model or voice file could not be read or downloaded ({name}: {text})"
    if isinstance(exc, TypeError):
        return f"the installed chatterbox package does not match the speech framework ({text})"
    return f"{name}: {text}"


class ChatterboxVoice:
    """One per program. Models are loaded once and kept."""

    def __init__(self, synth=None, to_pcm16=None, preload: bool = True):
        """`synth` and `to_pcm16` are given only by tests; normally they come
        from experimental/speech-framework."""
        self.device = os.environ.get("SB01_CHATTERBOX_DEVICE", "").strip().lower() or None
        self.preload = preload
        self.loaded = False
        self._synth, self._to_pcm16 = synth, to_pcm16
        if synth is None:
            self._synth, self._to_pcm16, self.preload = self._framework()

    def _framework(self):
        if not os.path.isfile(os.path.join(FRAMEWORK_DIR, "speech_synth.py")):
            raise VoiceUnavailable("experimental/speech-framework is not in this copy of the project")
        if FRAMEWORK_DIR not in sys.path:
            sys.path.insert(0, FRAMEWORK_DIR)
        try:
            importlib.import_module("_paths")    # puts the framework's utilities on the import path
            from settings import load_settings
            from speech_synth import SpeechSynth
            from tts_common import to_pcm16
            config = dict(load_settings().get("tts") or {})
        except Exception as exc:
            raise VoiceUnavailable(_reason(exc)) from None
        if self.device:
            config["device"] = self.device
        self.device = config.get("device", "auto")
        return SpeechSynth(config), to_pcm16, bool(config.get("preload", True))

    def load(self):
        """Load the models now, so the first reply does not wait for them and a
        missing model is found at startup. Safe to call again."""
        if self.loaded:
            return
        try:
            if self.preload:
                self._synth.preload()
        except Exception as exc:
            self._free_gpu()
            raise VoiceUnavailable(_reason(exc)) from None
        self.loaded = True

    @staticmethod
    def _free_gpu():
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def synthesize(self, text: str, language: str = "en", split: bool = False,
                   gesture_audio: bool = True) -> Speech:
        """Speak `text`. With `split`, each sentence is synthesized on its own
        so that its start time is measured, for placing gestures."""
        text = text.strip()
        if not text:
            raise VoiceUnavailable("there was nothing to say")
        pieces = sentences(text) if split else [text]
        waves, rate, words, clock = [], None, [], 0.0
        try:
            for piece in pieces:
                wave, piece_rate = self._synth.synthesize(piece, language)
                wave = self._checked(wave, piece_rate)
                if rate is None:
                    rate = piece_rate
                elif piece_rate != rate:
                    raise VoiceUnavailable("the voice changed its sample rate between sentences")
                if waves:
                    gap = np.zeros(round(SENTENCE_GAP_SECONDS * rate), dtype=np.float32)
                    waves.append(gap)
                    clock += len(gap) / rate
                words += self._word_times(piece, wave, rate, clock)
                waves.append(wave)
                clock += len(wave) / rate
            whole = np.concatenate(waves)
            seconds = len(whole) / rate
            if not MIN_SECONDS <= seconds <= MAX_SECONDS:
                raise VoiceUnavailable(f"the audio came out {seconds:.1f} s long, which cannot be right")
            pcm = self._to_pcm16(whole, rate, PLAYBACK_RATE)
            gesture_pcm = self._to_pcm16(whole, rate, GESTURE_RATE) if gesture_audio else None
        except VoiceUnavailable:
            raise
        except Exception as exc:
            self._free_gpu()
            raise VoiceUnavailable(_reason(exc)) from None
        if not pcm or len(pcm) % 2:
            raise VoiceUnavailable("the audio could not be converted for the robot's speaker")
        return Speech(pcm, gesture_pcm, seconds, words)

    @staticmethod
    def _checked(wave, rate) -> np.ndarray:
        """A usable piece of audio, or VoiceUnavailable saying what is wrong with it."""
        if not isinstance(rate, (int, np.integer)) or isinstance(rate, bool) or rate < 8000:
            raise VoiceUnavailable(f"the voice reported an impossible sample rate ({rate!r})")
        try:
            wave = np.asarray(wave, dtype=np.float32).reshape(-1)
        except Exception:
            raise VoiceUnavailable("the voice returned something that is not audio") from None
        if wave.size == 0:
            raise VoiceUnavailable("the voice returned no audio")
        if not np.isfinite(wave).all():
            raise VoiceUnavailable("the voice returned damaged audio (not-a-number samples)")
        if float(np.abs(wave).max()) < 1e-4:
            raise VoiceUnavailable("the voice returned silence")
        return wave

    @staticmethod
    def _word_times(piece: str, wave: np.ndarray, rate: int, start: float) -> list:
        """ESTIMATED start time of each word of one sentence: the spoken part of
        its audio (silence at either end left out), shared out by position in the text."""
        loud = np.flatnonzero(np.abs(wave) >= QUIET * float(np.abs(wave).max()))
        first, last = (loud[0], loud[-1]) if loud.size else (0, len(wave) - 1)
        begin, length = first / rate, max(0.0, (last - first) / rate)
        span = max(1, len(piece))
        return [(round(start + begin + length * match.start() / span, 3), match.group(0))
                for match in _WORD.finditer(piece)]
