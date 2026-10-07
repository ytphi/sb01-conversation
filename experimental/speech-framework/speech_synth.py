"""
speech_synth.py  -  route text to the right Chatterbox engine + voice per language

Default routing (config.yaml → tts.languages):
  en → chatterbox-nano          (fast, English only)
  zh / ja / es → chatterbox-multilingual (v3)

An engine that can't speak the requested language falls back to multilingual.
"""

import numpy as np

from settings import resolve_path
from tts_common import TTSEngine


def _make_engine(kind: str, cfg: dict) -> TTSEngine:
    device = cfg.get("device", "auto")
    if kind == "nano":
        from chatterbox_nano import ChatterboxNano
        return ChatterboxNano(device=device)
    if kind == "multilingual":
        from chatterbox_ml3 import ChatterboxMultilingual
        return ChatterboxMultilingual(device=device, t3_model=cfg.get("multilingual_t3_model"))
    raise ValueError(f"unknown TTS engine {kind!r} (expected 'nano' or 'multilingual')")


class SpeechSynth:
    def __init__(self, tts_cfg: dict):
        self.cfg = tts_cfg
        self.languages: dict[str, dict] = tts_cfg.get("languages", {})
        self._engines: dict[str, TTSEngine] = {}

    def _engine(self, kind: str) -> TTSEngine:
        if kind not in self._engines:
            self._engines[kind] = _make_engine(kind, self.cfg)
        return self._engines[kind]

    def preload(self):
        for kind in {lc.get("engine", "multilingual") for lc in self.languages.values()}:
            self._engine(kind).load()

    def synthesize(self, text: str, lang: str) -> tuple[np.ndarray, int]:
        lang_cfg = dict(self.languages.get(lang) or self.languages.get("en") or {})
        kind  = lang_cfg.pop("engine", "multilingual")
        voice = resolve_path(lang_cfg.pop("voice", None))
        engine = self._engine(kind)
        if not engine.supports(lang):
            print(f"[tts] {engine.name} can't speak {lang!r}; using multilingual")
            engine = self._engine("multilingual")
        return engine.synthesize(text, lang, voice=voice, **lang_cfg)
