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

One engine on the card. With cuda asked for, SB01_CHATTERBOX_GPU_ENGINES can
name the engines that go on the graphics card (for example "nano", the English
engine); the others stay on the processor. Less memory is then required, so a
small card can be shared with the gesture server for the languages that engine
speaks. The audio is made by the same model either way.

Settings:
  SB01_CHATTERBOX_DEVICE        cpu (default) | cuda   overrides tts.device in config.yaml
  SB01_CHATTERBOX_GPU_ENGINES   with cuda: which engines use the card (default: all), e.g. nano
  SB01_CHATTERBOX_GPU_GB        free graphics memory required before cuda is used
                                (default 7 for both engines, 4 for nano, 5.5 for multilingual;
                                it cannot be set below what the engines were measured to use)
  SB01_CHATTERBOX_TONE          unset (default): the voice as the model makes it | warm

Volume. Chatterbox audio normally keeps exactly the amplitude the model
generated, in every language: nothing here turns it up, matches it to Edge TTS,
or lowers it as a rule. (Synthesis runs with PyTorch's gradient bookkeeping
off, see _no_gradients; that is a memory fix and does not change the waveform.)
What is done to the audio is only what playing it needs: conversion to 16 kHz
16-bit for the robot and to 24 kHz for the gesture model and, when sentences
are synthesized one by one, trimming silence and a 5 ms fade at each join.

The one exception is actual digital clipping. 16-bit audio cannot hold a
sample above full scale, and now and then the multilingual engine produces one
(or the conversion to 16 kHz raises a peak over it). Only then, that one reply
is multiplied by the largest number below 1 that brings its highest peak just
under full scale (see _clip_guard). The robot audio and the gesture copy get
the same number, Speech.scale records it, and the conversation program prints
it. A reply within range is never touched: its scale is exactly 1.0.

Tone, only when asked for. SB01_CHATTERBOX_TONE=warm changes how every reply
sounds, on purpose: more body in the low part of the voice, slightly less edge
at the top, and about 5 dB louder, with the loudest moments held under a
ceiling so the lift cannot clip (see warm_tone). Timing and length are not
changed. Without the setting none of this runs and the paragraphs above hold.
"""

import contextlib
import gc
import importlib
import os
import re
import subprocess
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
# One engine alone on the card. Measured at its peak (weights, a long reply and
# the CUDA runtime): nano 2.5 GB, multilingual 4.0 GB, both 6.0 GB. Required
# free before loading: that plus 1.5 GB, which is the gesture server (1.3 GB)
# starting afterwards, or the same room for anything else. On a 6 GB card with
# the gesture server already running (4.7 GB free) nano is allowed and leaves
# about 2.6 GB; multilingual is not.
GPU_GB_USED = {"nano": 2.5, "multilingual": 4.0}
GPU_GB_USED_BY_ALL = 6.0
GPU_GB_RESERVE = 1.5
GPU_GB_PER_ENGINE = {name: used + GPU_GB_RESERVE for name, used in GPU_GB_USED.items()}

# Sentences synthesized one by one are joined with a pause. Each piece keeps at
# most this much of its own silence and is faded at both ends, so a join can
# neither click nor leave a long gap.
SENTENCE_GAP_SECONDS = 0.12
LEAD_SILENCE_SECONDS = 0.08
TAIL_SILENCE_SECONDS = 0.18
FADE_SECONDS = 0.005
QUIET = 0.02                # of the loudest sample: below this is silence
# Only for a reply that would otherwise exceed full scale: where its highest
# peak is put. 0.998 is 0.02 dB under full scale, enough that rounding to 16
# bits cannot bring a sample back up to it.
SAFE_PEAK = 0.998
# SB01_CHATTERBOX_TONE=warm (chosen by ear from samples on 2026-10-09):
TONES = ("warm",)
WARM_LOW_DB = 3.5           # more body: this much more below about 300 Hz
WARM_HIGH_DB = -2.0         # less edge: this much less above about 5 kHz
WARM_LOUDER_DB = 5.0        # everything this much louder ...
WARM_CEILING = 0.94         # ... except that no peak goes above this (of full scale)
WARM_BLOCK_SECONDS = 0.005  # how finely the loudest moments are followed
WARM_RELEASE_SECONDS = 0.06

_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+|[。！？]+")
_CJK = re.compile(r"[㐀-鿿]")
_WORD = re.compile(r"[㐀-鿿]|[^\W_]+(?:['’][^\W_]+)*")


class VoiceUnavailable(Exception):
    """Chatterbox cannot be used right now; the message says why in plain words."""


def chosen_tone() -> str:
    """Empty for the voice as the model makes it, or the tone SB01_CHATTERBOX_TONE names."""
    tone = os.environ.get("SB01_CHATTERBOX_TONE", "").strip().lower()
    if tone and tone not in TONES:
        raise VoiceUnavailable(f"SB01_CHATTERBOX_TONE is {tone!r}; use {' or '.join(TONES)}, or leave it unset")
    return tone


def warm_tone(wave: np.ndarray, rate: int) -> np.ndarray:
    """The same speech, warmer and louder. Same length and timing.

    Tone: a smooth lift below about 300 Hz and a smooth cut above about 5 kHz,
    applied to the whole reply at once, so nothing is shifted in time.
    Level: everything up by WARM_LOUDER_DB; where that would pass WARM_CEILING
    the level is eased down just before the peak and let back up after it."""
    wave = np.asarray(wave, dtype=np.float32)
    spectrum = np.fft.rfft(wave)
    frequency = np.fft.rfftfreq(len(wave), 1.0 / rate)
    low = 1.0 / (1.0 + (frequency / 300.0) ** 2)
    high = 1.0 - 1.0 / (1.0 + (frequency / 5000.0) ** 2)
    change_db = WARM_LOW_DB * low + WARM_HIGH_DB * high
    change_db[frequency < 60.0] = 0.0                # no lift of rumble below the voice
    shaped = np.fft.irfft(spectrum * 10.0 ** (change_db / 20.0), n=len(wave)) * 10.0 ** (WARM_LOUDER_DB / 20.0)
    # The loudest sample of each short block, and of the blocks beside it, says
    # how far that stretch must come down; in between the level moves smoothly.
    block = max(1, round(WARM_BLOCK_SECONDS * rate))
    count = -(-len(shaped) // block)
    padded = np.zeros(count * block)
    padded[:len(shaped)] = np.abs(shaped)
    peaks = padded.reshape(count, block).max(axis=1)
    beside = np.pad(peaks, 1, mode="edge")
    peaks = np.maximum(np.maximum(beside[:-2], beside[1:-1]), beside[2:])
    level = np.minimum(1.0, WARM_CEILING / np.maximum(peaks, 1e-9))
    span = max(1, round(WARM_RELEASE_SECONDS / WARM_BLOCK_SECONDS))
    eased = np.convolve(np.pad(level, span, mode="edge"), np.ones(2 * span + 1) / (2 * span + 1), mode="valid")
    level = np.minimum(level, eased)
    centres = (np.arange(count) + 0.5) * block
    return (shaped * np.interp(np.arange(len(shaped)), centres, level)).astype(np.float32)


@dataclass
class Speech:
    pcm: bytes                          # 16 kHz mono 16-bit, for the robot
    gesture_pcm: bytes | None           # 24 kHz mono 16-bit, for the gesture model
    seconds: float
    words: list = field(default_factory=list)   # [(start seconds, word)]: ESTIMATES
    pieces: int = 1                     # how many sentences were synthesized separately
    scale: float = 1.0                  # 1.0 unless the reply had to be turned down so it does not clip


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


def _torch_free_gb() -> float | None:
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        return torch.cuda.mem_get_info()[0] / 2 ** 30
    except Exception:
        return None


def _smi_free_gb() -> float | None:
    """Free memory of the first card as the driver's own tool counts it (every
    program included), or None if the tool is not there."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        total, used = (float(value) for value in out.strip().splitlines()[0].split(","))
        return max(0.0, total - used) / 1024
    except Exception:
        return None


def gpu_free_gb() -> float | None:
    """Free graphics memory in GB, or None if there is no usable CUDA card.
    The lower of two readings: under Windows/WSL PyTorch's figure was seen not
    to count what other programs hold, while nvidia-smi's does."""
    free = _torch_free_gb()
    if free is None:
        return None
    other = _smi_free_gb()
    return free if other is None else min(free, other)


def gpu_engines() -> tuple:
    """The engines SB01_CHATTERBOX_GPU_ENGINES names, or () for all of them."""
    names = tuple(dict.fromkeys(
        name.strip().lower() for name in os.environ.get("SB01_CHATTERBOX_GPU_ENGINES", "").split(",") if name.strip()))
    for name in names:
        if name not in ENGINES:
            raise VoiceUnavailable(f"SB01_CHATTERBOX_GPU_ENGINES names {name!r}; use {' or '.join(ENGINES)}")
    return () if set(names) == set(ENGINES) else names


def choose_device(asked: str | None, engines: tuple = ()) -> tuple[str, str]:
    """(device to use, why) for what was asked for. Only an explicit "cuda"
    can put Chatterbox on the graphics card, and only if enough of it is free.
    `engines` are the ones that would use the card; () means all of them."""
    asked = (asked or "").strip().lower()
    if asked in ("", "auto", "cpu"):
        why = "" if asked == "cpu" else "the processor is the default, so the graphics card is left to the gesture server"
        return "cpu", why
    if asked == "mps":
        return "mps", ""
    if not asked.startswith("cuda"):
        raise VoiceUnavailable(f"SB01_CHATTERBOX_DEVICE / tts.device is {asked!r}; use cpu or cuda")
    usual = sum(GPU_GB_PER_ENGINE[name] for name in engines) if engines else GPU_FREE_GB_NEEDED
    least = sum(GPU_GB_USED[name] for name in engines) if engines else GPU_GB_USED_BY_ALL
    try:
        needed = float(os.environ.get("SB01_CHATTERBOX_GPU_GB", "") or usual)
    except ValueError:
        needed = usual
    needed = max(needed, least)      # never less than the engines themselves take
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
        self.tone = chosen_tone()
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
        some = gpu_engines()
        self.device, self.device_note = choose_device(asked, some)
        on_card = some if self.device.startswith("cuda") else ()
        # never "auto": that would pick the graphics card. With only some
        # engines on the card, the rest are made for the processor.
        config["device"] = "cpu" if on_card else self.device
        try:
            synth = SpeechSynth(config)
            for name in on_card:
                synth._engine(name).device = self.device       # made, not loaded: load() reads this
        except Exception as exc:
            raise VoiceUnavailable(_reason(exc)) from None
        if on_card:
            fast = sorted(language for language, entry in config["languages"].items()
                          if (entry or {}).get("engine", "multilingual") in on_card)
            self.device_note = (f"only the {' and '.join(on_card)} engine is on the graphics card ({self.device_note}): "
                                f"{', '.join(fast) or 'no language'} is fast, other languages are synthesized "
                                f"on the processor and are slow")
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
            try:
                clip = resolve_path(entry.get("voice"))
            except Exception:
                raise VoiceUnavailable(f"tts.languages.{language}.voice in the speech framework's config "
                                       f"is not a file path ({entry.get('voice')!r})") from None
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
                with self._no_gradients():
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
            # The amplitude is the model's own: no gain, no matching to Edge TTS,
            # unless a tone was asked for (SB01_CHATTERBOX_TONE).
            if self.tone == "warm":
                whole = warm_tone(whole, rate)
            pcm = self._to_pcm16(whole, rate, PLAYBACK_RATE)
            gesture_pcm = self._to_pcm16(whole, rate, GESTURE_RATE) if gesture_audio else None
            # Unless a sample would exceed full scale: then this reply alone is
            # turned down just enough, the same for both copies.
            scale = self._clip_guard(whole, rate, pcm, gesture_pcm)
            if scale < 1.0:
                whole = whole * np.float32(scale)
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
        return Speech(pcm, gesture_pcm, seconds, words, len(pieces), scale)

    def _clip_guard(self, wave: np.ndarray, rate: int, pcm: bytes, gesture_pcm: bytes | None) -> float:
        """1.0 if this reply fits in 16-bit audio as it is, which is the normal
        case and leaves it untouched. Otherwise the largest scale that puts its
        highest peak at SAFE_PEAK, counting the peak after conversion to each
        rate that will be used (conversion can raise a peak)."""
        made = [(PLAYBACK_RATE, pcm)] + ([(GESTURE_RATE, gesture_pcm)] if gesture_pcm else [])
        if not any(self._at_full_scale(audio) for _, audio in made):
            return 1.0                   # nothing reached full scale, so nothing can have been cut
        # Something reached full scale: measure the real peaks, which the 16-bit
        # audio no longer shows, by converting a copy scaled well inside range.
        highest = float(np.abs(wave).max())
        probe = 0.5 / highest
        quiet = wave * np.float32(probe)
        for target, _ in made:
            converted = np.frombuffer(self._to_pcm16(quiet, rate, target), dtype="<i2").astype(np.int32)
            highest = max(highest, float(np.abs(converted).max()) / 32767.0 / probe)
        return 1.0 if highest <= 1.0 else SAFE_PEAK / highest

    @staticmethod
    def _at_full_scale(pcm: bytes) -> bool:
        return bool(pcm) and int(np.abs(np.frombuffer(pcm, dtype="<i2").astype(np.int32)).max()) >= 32767

    @staticmethod
    def _no_gradients():
        """Synthesis with PyTorch's gradient bookkeeping off. A memory fix: the
        waveform the model generates is the same sample for sample. Without this
        the English engine's watermarking step kept about 5 MB of ordinary
        memory with every sentence, for good.
        Where PyTorch is not installed (the offline tests) this does nothing."""
        try:
            import torch
            return torch.inference_mode()
        except Exception:
            return contextlib.nullcontext()

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
