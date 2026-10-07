"""
chatterbox_nano.py  -  Chatterbox Nano (110M, English only, fast on CPU/edge)

Install with experimental/speech-framework/requirements.txt (Chatterbox pinned to one commit;
the 0.1.7 release on PyPI lacks options used here).
Supports paralinguistic tags in text, e.g. "[laugh]", "[chuckle]".
"""

from tts_common import ChatterboxEngine


class ChatterboxNano(ChatterboxEngine):
    name = "chatterbox-nano"
    languages = frozenset({"en"})

    def _load_model(self, device: str):
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        return ChatterboxTurboTTS.from_pretrained(device=device, nano=True)

    def synthesize(self, text, lang="en", voice=None, **params):
        self.load()
        self._use_voice(voice)
        wav = self.model.generate(text, **params)
        return self._to_numpy(wav)
