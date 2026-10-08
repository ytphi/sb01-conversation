"""
tts_common.py  -  shared pieces for the local TTS engines

  Prosody          - per-phrase tone plan (pitch, contour, speed, emphasis, pause) from the LLM
  TTSEngine        - interface every engine implements:
                     synthesize(text, lang, voice, prosody=None) → (wav, sr)
  ChatterboxEngine - lazy model loading + per-voice conditioning cache for Chatterbox models
  resolve_device   - "auto" → cuda / mps / cpu
  to_pcm16         - float waveform → 16 kHz mono int16 bytes (what the G1 PlayStream wants)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np

CONTOURS = ("fall", "rise", "flat", "question")


@dataclass
class Prosody:
    """How one phrase should sound. Every field is optional: None = engine default.

    pitch     - semitones above (+) or below (-) the voice's normal speaking pitch
    range     - multiplier on the voice's pitch movement (0 = monotone, 1 = normal)
    contour   - "fall" (statement), "rise", "flat" or "question" (rise at the end)
    speed     - speaking rate multiplier (1.0 = normal)
    emphasis  - words to stress
    pause_ms  - silence to add after the phrase

    Engines use what they can: the UTAU engine uses all of it; Chatterbox maps
    pitch / contour / speed onto exaggeration and cfg_weight and ignores the rest.
    """
    pitch:    float | None = None
    range:    float | None = None
    contour:  str | None = None
    speed:    float | None = None
    emphasis: list[str] = field(default_factory=list)
    pause_ms: int | None = None

    def is_empty(self) -> bool:
        return (self.pitch is None and self.range is None and self.contour is None
                and self.speed is None and not self.emphasis and self.pause_ms is None)

    def merged(self, other: "Prosody | None") -> "Prosody":
        """self with any fields set in other taking priority."""
        if other is None:
            return self
        return Prosody(
            pitch=other.pitch if other.pitch is not None else self.pitch,
            range=other.range if other.range is not None else self.range,
            contour=other.contour or self.contour,
            speed=other.speed if other.speed is not None else self.speed,
            emphasis=other.emphasis or self.emphasis,
            pause_ms=other.pause_ms if other.pause_ms is not None else self.pause_ms,
        )


def resolve_device(device: str = "auto") -> str:
    if device != "auto":
        return device
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def to_pcm16(wav: np.ndarray, sr: int, target_sr: int = 16000) -> bytes:
    if sr != target_sr:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(sr, target_sr)
        wav = resample_poly(np.asarray(wav, dtype=np.float32), target_sr // g, sr // g)
    wav = np.clip(wav, -1.0, 1.0)
    return (wav * 32767).astype(np.int16).tobytes()


class TTSEngine(ABC):
    name = "base"
    languages: frozenset[str] = frozenset()
    sound_tags = False           # True if the model speaks [laugh]-style tags itself

    def supports(self, lang: str, asset: str | None = None) -> bool:
        """Can this engine speak lang (with that clip / voicebank)?"""
        return lang in self.languages

    @abstractmethod
    def load(self):
        """Load model weights (called lazily on first use, or eagerly to preload)."""

    @abstractmethod
    def synthesize(self, text: str, lang: str, voice: str | None = None,
                   prosody: Prosody | None = None, **params) -> tuple[np.ndarray, int]:
        """Return (mono float32 waveform in [-1, 1], sample_rate).

        voice is engine specific: a reference clip for Chatterbox, a voicebank
        folder for UTAU, None for the engine's built-in voice.
        """


class ChatterboxEngine(TTSEngine):
    """Base for Chatterbox models: caches the conditioning for the current voice.

    voice=None uses the model's built-in default voice (conds.pt shipped with
    the checkpoint). A path to a ~10 s reference .wav clones that voice; the
    conditioning is only recomputed when the voice changes.
    """

    def __init__(self, device: str = "auto"):
        self.device = device
        self.model = None
        self._builtin_conds = None
        self._voice: str | None = None

    @abstractmethod
    def _load_model(self, device: str):
        ...

    def load(self):
        if self.model is None:
            device = resolve_device(self.device)
            print(f"[tts] loading {self.name} on {device}...")
            self.model = self._load_model(device)
            self._builtin_conds = self.model.conds
            self._voice = None

    def _use_voice(self, voice: str | None):
        if voice == self._voice:
            return
        if voice is None:
            if self._builtin_conds is None:
                raise RuntimeError(f"{self.name}: no built-in voice; set a voice reference clip")
            self.model.conds = self._builtin_conds
        else:
            self._prepare(voice)
        self._voice = voice

    def _prepare(self, clip: str):
        self.model.prepare_conditionals(clip)

    def _to_numpy(self, wav) -> tuple[np.ndarray, int]:
        return wav.squeeze(0).detach().cpu().numpy().astype(np.float32), self.model.sr

    # Chatterbox has no pitch or speed control. These knobs only exist on the
    # Multilingual model (Turbo / Nano ignores them): higher exaggeration sounds
    # livelier, lower cfg_weight speaks a little faster.
    supports_style_knobs = False

    def apply_prosody(self, params: dict, prosody: Prosody | None) -> dict:
        if not self.supports_style_knobs or prosody is None or prosody.is_empty():
            return params
        params = dict(params)
        exaggeration = float(params.get("exaggeration", 0.5))
        cfg_weight   = float(params.get("cfg_weight", 0.5))
        if prosody.pitch is not None:
            exaggeration += 0.04 * prosody.pitch
        if prosody.range is not None:
            exaggeration += 0.2 * (prosody.range - 1.0)
        if prosody.contour in ("rise", "question"):
            exaggeration += 0.1
        elif prosody.contour == "flat":
            exaggeration -= 0.1
        if prosody.speed is not None:
            cfg_weight -= 0.5 * (prosody.speed - 1.0)
        params["exaggeration"] = float(np.clip(exaggeration, 0.25, 1.0))
        params["cfg_weight"]   = float(np.clip(cfg_weight, 0.2, 0.8))
        return params


def pause(seconds: float, sr: int) -> np.ndarray:
    return np.zeros(int(max(seconds, 0.0) * sr), dtype=np.float32)
