"""
voice_chatterbox.py  -  the Chatterbox voice for scripts/sb01_conversation.py

Uses the speech framework in experimental/speech-framework as it is: the same
engines, the same config.yaml (and config.local.yaml), the same voice reference
clips. This file only adapts it to what the conversation program needs:

  ChatterboxVoice().load()                    load the models once, at startup
  .synthesize(text, language, ...) -> Speech  audio for the robot's speaker, the
                                              same audio for the gesture model,
                                              and estimated word times
  .close()                                    let go of the models and their memory

Where it runs. Chatterbox runs on the processor (cpu) unless a graphics card is
asked for by name, because its two engines need about 5.7 GB of graphics memory
and the gesture server uses the same card. "auto" means cpu here. When cuda is
asked for, the free graphics memory is checked first; if there is not enough,
Chatterbox runs on the processor and says so. On a processor it is slow: a
reply takes longer to synthesize than to say. The free-memory figure comes from
the graphics driver; it is a check before loading, not a guarantee.

Word times. Chatterbox does not report when each word is spoken. Word times
here are ESTIMATES: each sentence's start and length are measured from its
audio, and words are spread across the spoken part of the sentence by their
position in the text. They bring a gesture to the right part of a sentence,
not to the exact word.

Failures. Every failure (package or model missing, bad voice setting, not
enough memory, unusable audio) is raised as VoiceUnavailable with a plain
reason, after the models and their memory have been let go, so the caller can
say it and use its other voice.

Settings:
  SB01_CHATTERBOX_DEVICE   cpu (default) | cuda   overrides tts.device in config.yaml
  SB01_CHATTERBOX_GPU_GB   free graphics memory required before cuda is used (default 7)
"""

import gc
import importlib
import os
import re
import sys
from dataclasses import dataclass, field

import numpy as np

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FRAMEWORK_DIR = os.path.join(REPO_ROOT, "experimental", "speech-framework")
INSTALL_HINT = "pip install -r experimental/speech-framework/requirements.txt"

PLAYBACK_RATE = 16000       # what the robot's speaker takes: 16 kHz, one channel, 16-bit
GESTURE_RATE = 24000        # what the gesture model takes
MIN_SECONDS = 0.15          # shorter than this is not speech
MAX_SECONDS = 60.0          # longer than this for one reply is a runaway generation
GPU_FREE_GB_NEEDED = 7.0    # both engines measured at about 5.7 GB, plus room to work
ENGINES = ("nano", "multilingual")

# Sentences synthesized one by one are joined with a pause. Each piece keeps at
# most this much of its own silence and is faded at both ends, so a join can
# neither click nor leave a long gap.
SENTENCE_GAP_SECONDS = 0.12
LEAD_SILENCE_SECONDS = 0.08
TAIL_SILENCE_SECONDS = 0.18
FADE_SECONDS = 0.005
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
    words: list = field(default_factory=list)   # [(start seconds, word)]: ESTIMATES
    pieces: int = 1                     # how many sentences were synthesized separately


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


def _out_of_memory(exc: Exception) -> bool:
    return "outofmemory" in type(exc).__name__.lower() or "out of memory" in str(exc).lower()


def _reason(exc: Exception) -> str:
    """Why loading or speaking failed, for a person."""
    name, text = type(exc).__name__, " ".join(str(exc).split())[:200]
    if isinstance(exc, ImportError):
        missing = getattr(exc, "name", None) or text
        return f"a Python package is missing ({missing}). Install it with: {INSTALL_HINT}"
    if _out_of_memory(exc):
        return ("the graphics card ran out of memory. Leave SB01_CHATTERBOX_DEVICE unset (or cpu) "
                "so the voice runs on the processor and the card is left to the gesture server")
    if isinstance(exc, OSError) or "huggingface" in text.lower() or "snapshot" in text.lower():
        return f"a model or voice file could not be read or downloaded ({name}: {text})"
    if isinstance(exc, TypeError):
        return (f"the installed chatterbox package does not match the speech framework ({text}). "
                f"Reinstall with: {INSTALL_HINT}")
    return f"{name}: {text}"


def gpu_free_gb() -> float | None:
    """Free graphics memory in GB, or None if there is no usable CUDA card."""
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        return torch.cuda.mem_get_info()[0] / 2 ** 30
    except Exception:
        return None


def choose_device(asked: str | None) -> tuple[str, str]:
    """(device to use, why) for what was asked for. Only an explicit "cuda"
    can put Chatterbox on the graphics card, and only if enough of it is free."""
    asked = (asked or "").strip().lower()
    if asked in ("", "auto", "cpu"):
        why = "" if asked == "cpu" else "the processor is the default, so the graphics card is left to the gesture server"
        return "cpu", why
    if asked == "mps":
        return "mps", ""
    if not asked.startswith("cuda"):
        raise VoiceUnavailable(f"SB01_CHATTERBOX_DEVICE / tts.device is {asked!r}; use cpu or cuda")
    try:
        needed = float(os.environ.get("SB01_CHATTERBOX_GPU_GB", "") or GPU_FREE_GB_NEEDED)
    except ValueError:
        needed = GPU_FREE_GB_NEEDED
    free = gpu_free_gb()
    if free is None:
        return "cpu", "cuda was asked for but no usable graphics card was found, so it runs on the processor"
    if free < needed:
        return "cpu", (f"cuda was asked for but only {free:.1f} GB of graphics memory is free and Chatterbox "
                       f"needs about {needed:.1f} GB, so it runs on the processor")
    return asked, f"{free:.1f} GB of graphics memory was free"


class ChatterboxVoice:
    """One per program. Models are loaded once and kept until close()."""

    def __init__(self, synth=None, to_pcm16=None, preload: bool = True, device: str | None = None):
        """`synth`, `to_pcm16` and `device` are given only by tests; normally they
        come from experimental/speech-framework and the settings."""
        self.preload = preload
        self.loaded = False
        self.device, self.device_note = "cpu", ""
        self._synth, self._to_pcm16 = synth, to_pcm16
        if synth is None:
            self._synth, self._to_pcm16, self.preload = self._framework()
        elif device is not None:
            self.device, self.device_note = choose_device(device)

    def _framework(self):
        if not os.path.isfile(os.path.join(FRAMEWORK_DIR, "speech_synth.py")):
            raise VoiceUnavailable("experimental/speech-framework is not in this copy of the project")
        if FRAMEWORK_DIR not in sys.path:
            sys.path.insert(0, FRAMEWORK_DIR)
        try:
            importlib.import_module("_paths")    # puts the framework's utilities on the import path
            from settings import load_settings, resolve_path
            from speech_synth import SpeechSynth
            from tts_common import to_pcm16
            config = dict(load_settings().get("tts") or {})
        except Exception as exc:
            raise VoiceUnavailable(_reason(exc)) from None
        self._check_voices(config, resolve_path)
        asked = os.environ.get("SB01_CHATTERBOX_DEVICE", "").strip() or str(config.get("device", ""))
        self.device, self.device_note = choose_device(asked)
        config["device"] = self.device           # never "auto": that would pick the graphics card
        try:
            synth = SpeechSynth(config)
        except Exception as exc:
            raise VoiceUnavailable(_reason(exc)) from None
        return synth, to_pcm16, bool(config.get("preload", True))

    @staticmethod
    def _check_voices(config: dict, resolve_path):
        """Find a wrong engine name or a missing voice clip at startup, not mid-conversation."""
        languages = config.get("languages")
        if not isinstance(languages, dict) or not languages:
            raise VoiceUnavailable("the speech framework's config has no tts.languages")
        for language, entry in languages.items():
            entry = entry or {}
            if not isinstance(entry, dict):
                raise VoiceUnavailable(f"tts.languages.{language} in the speech framework's config is not a mapping")
            engine = entry.get("engine", "multilingual")
            if engine not in ENGINES:
                raise VoiceUnavailable(f"tts.languages.{language}.engine is {engine!r}; use one of {', '.join(ENGINES)}")
            clip = resolve_path(entry.get("voice"))
            if clip and not os.path.isfile(clip):
                raise VoiceUnavailable(f"the voice clip for {language!r} was not found: {os.path.basename(clip)} "
                                       f"(set in tts.languages.{language}.voice)")

    def load(self):
        """Load the models now, so the first reply does not wait for them and a
        missing model is found at startup. Safe to call again."""
        if self.loaded:
            return
        try:
            if self.preload:
                self._synth.preload()
        except Exception as exc:
            self.close()
            raise VoiceUnavailable(_reason(exc)) from None
        self.loaded = True

    def close(self):
        """Let go of the models and the memory they hold. Safe to call at any time."""
        self.loaded = False
        engines = getattr(self._synth, "_engines", None)
        if isinstance(engines, dict):
            for engine in engines.values():
                if hasattr(engine, "model"):
                    engine.model = None
            engines.clear()
        gc.collect()
        self._free_gpu()

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
        """Speak `text`. Nothing is returned until all of it has been made, so a
        failure part-way can never leave half a reply to be played.
        With `split`, each sentence is synthesized on its own so that its start
        time is measured, for placing gestures."""
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
                if len(pieces) > 1:
                    wave = self._tidied(wave, rate)
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
            if _out_of_memory(exc):
                gc.collect()
            self._free_gpu()
            raise VoiceUnavailable(_reason(exc)) from None
        if not pcm or len(pcm) % 2:
            raise VoiceUnavailable("the audio could not be converted for the robot's speaker")
        return Speech(pcm, gesture_pcm, seconds, words, len(pieces))

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
    def _spoken_span(wave: np.ndarray) -> tuple[int, int]:
        """First and last sample that is not silence."""
        loud = np.flatnonzero(np.abs(wave) >= QUIET * float(np.abs(wave).max()))
        return (int(loud[0]), int(loud[-1])) if loud.size else (0, len(wave) - 1)

    @classmethod
    def _tidied(cls, wave: np.ndarray, rate: int) -> np.ndarray:
        """One sentence ready to be joined to the next: long silence at either end
        cut back, and a few milliseconds of fade so the join cannot click.
        Only silence is removed; the speech itself is not touched."""
        first, last = cls._spoken_span(wave)
        start = max(0, first - round(LEAD_SILENCE_SECONDS * rate))
        stop = min(len(wave), last + 1 + round(TAIL_SILENCE_SECONDS * rate))
        wave = wave[start:stop].copy()
        fade = min(round(FADE_SECONDS * rate), len(wave) // 2)
        if fade > 0:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            wave[:fade] *= ramp
            wave[-fade:] *= ramp[::-1]
        return wave

    @classmethod
    def _word_times(cls, piece: str, wave: np.ndarray, rate: int, start: float) -> list:
        """ESTIMATED start time of each word of one sentence: the spoken part of
        its audio (silence at either end left out), shared out by position in the text."""
        first, last = cls._spoken_span(wave)
        begin, length = first / rate, max(0.0, (last - first) / rate)
        span = max(1, len(piece))
        return [(round(start + begin + length * match.start() / span, 3), match.group(0))
                for match in _WORD.finditer(piece)]
