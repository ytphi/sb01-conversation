"""
chatterbox_nano.py  -  Chatterbox Nano (110M, English only, fast on CPU/edge)

pip install chatterbox-tts
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
