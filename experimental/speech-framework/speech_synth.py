"""
speech_synth.py  -  speak text in the selected voice (see voices.py for the registry)

A voice routes each language to an engine + asset:
  nano          Chatterbox Nano (English, [laugh]-style tags)
  multilingual  Chatterbox Multilingual v3 (23 languages, clip cloning)
  utau          UTAU voicebank rendered with WORLD (utilities/tts/utau)

Languages the voice lacks go to its fallback voice; an engine that can't speak
the language falls back to multilingual with the built-in voice.
"""

import numpy as np

from tts_common import Prosody, TTSEngine
from voices import Route, Voice, VoiceRegistry


def _make_engine(kind: str, cfg: dict) -> TTSEngine:
    device = cfg.get("device", "auto")
    if kind == "nano":
        from chatterbox_nano import ChatterboxNano
        return ChatterboxNano(device=device)
    if kind == "multilingual":
        from chatterbox_ml3 import ChatterboxMultilingual
        return ChatterboxMultilingual(device=device, t3_model=cfg.get("multilingual_t3_model"))
    if kind == "utau":
        from utau_engine import UTAUEngine
        return UTAUEngine(**(cfg.get("utau") or {}))
    raise ValueError(f"unknown TTS engine {kind!r} (expected 'nano', 'multilingual' or 'utau')")


class SpeechSynth:
    def __init__(self, tts_cfg: dict, voice: str | None = None):
        self.cfg      = tts_cfg
        self.registry = VoiceRegistry(tts_cfg)
        self.voice: Voice = self.registry.get(voice)
        self._engines: dict[str, TTSEngine] = {}
        for problem in self.registry.check():
            if problem.startswith(self.voice.name + "/") or problem.startswith(self.voice.name + ":"):
                print(f"[tts] warning: {problem}")
        print(f"[tts] voice {self.voice.name!r}: "
              + ", ".join(f"{lang}={r.engine}" for lang, r in self.voice.languages.items())
              + (f" (else {self.voice.fallback})" if self.voice.fallback else ""))

    @property
    def wants_tone_tags(self) -> bool:
        return self.voice.tone_tags

    def set_voice(self, name: str):
        self.voice = self.registry.get(name)

    def _engine(self, kind: str) -> TTSEngine:
        if kind not in self._engines:
            self._engines[kind] = _make_engine(kind, self.cfg)
        return self._engines[kind]

    def _routes(self) -> list[Route]:
        routes, seen, v = [], set(), self.voice
        while v and v.name not in seen:
            seen.add(v.name)
            routes += v.languages.values()
            v = self.registry.voices.get(v.fallback) if v.fallback else None
        return routes

    def preload(self):
        for route in self._routes():
            engine = self._engine(route.engine)
            engine.load()
            if hasattr(engine, "warm"):
                engine.warm(route.asset)

    def route(self, lang: str) -> tuple[TTSEngine, Route]:
        found = self.registry.route(self.voice, lang)
        route = found[1] if found else Route("multilingual")
        engine = self._engine(route.engine)
        if not engine.supports(lang, route.asset):
            print(f"[tts] {engine.name} can't speak {lang!r}; using multilingual")
            route, engine = Route("multilingual"), self._engine("multilingual")
        return engine, route

    def synthesize(self, text: str, lang: str, prosody: Prosody | None = None,
                   tts_text: str | None = None) -> tuple[np.ndarray, int]:
        engine, route = self.route(lang)
        say = tts_text if (tts_text and engine.sound_tags) else text
        return engine.synthesize(say, lang, voice=route.asset, prosody=prosody, **route.params)
