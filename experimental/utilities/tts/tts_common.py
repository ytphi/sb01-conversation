"""
tts_common.py  -  shared pieces for the local TTS engines

  TTSEngine        - interface every engine implements: synthesize(text, lang, voice) → (wav, sr)
  ChatterboxEngine - lazy model loading + per-voice conditioning cache for Chatterbox models
  resolve_device   - "auto" → cuda / mps / cpu
  to_pcm16         - float waveform → 16 kHz mono int16 bytes (what the G1 PlayStream wants)
"""

from abc import ABC, abstractmethod

import numpy as np


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
        import torch
        import torchaudio.functional as AF
        wav = AF.resample(torch.from_numpy(wav), sr, target_sr).numpy()
    wav = np.clip(wav, -1.0, 1.0)
    return (wav * 32767).astype(np.int16).tobytes()


class TTSEngine(ABC):
    name = "base"
    languages: frozenset[str] = frozenset()

    def supports(self, lang: str) -> bool:
        return lang in self.languages

    @abstractmethod
    def load(self):
        """Load model weights (called lazily on first use, or eagerly to preload)."""

    @abstractmethod
    def synthesize(self, text: str, lang: str, voice: str | None = None,
                   **params) -> tuple[np.ndarray, int]:
        """Return (mono float32 waveform in [-1, 1], sample_rate)."""


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
            self.model.prepare_conditionals(voice)
        self._voice = voice

    def _to_numpy(self, wav) -> tuple[np.ndarray, int]:
        return wav.squeeze(0).detach().cpu().numpy().astype(np.float32), self.model.sr
