"""
chatterbox_ml3.py  -  Chatterbox Multilingual (t3 "v3" checkpoint, 23 languages)

pip install chatterbox-tts
Used for zh / ja / es (and en, if you prefer it over Nano).
A Prosody plan is mapped onto exaggeration / cfg_weight (see ChatterboxEngine.apply_prosody).
"""

import numpy as np

from tts_common import ChatterboxEngine, pause

SUPPORTED = frozenset({
    "ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it", "ja",
    "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv", "sw", "tr", "zh",
})


class ChatterboxMultilingual(ChatterboxEngine):
    name = "chatterbox-multilingual"
    languages = SUPPORTED
    supports_style_knobs = True

    def __init__(self, device: str = "auto", t3_model: str | None = "v3"):
        super().__init__(device)
        self.t3_model = t3_model

    def _load_model(self, device: str):
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        kwargs = {"t3_model": self.t3_model} if self.t3_model else {}
        return ChatterboxMultilingualTTS.from_pretrained(device=device, **kwargs)

    def synthesize(self, text, lang="en", voice=None, prosody=None, **params):
        self.load()
        self._use_voice(voice)
        params = self.apply_prosody(params, prosody)
        wav, sr = self._to_numpy(self.model.generate(text, language_id=lang, **params))
        if prosody and prosody.pause_ms:
            wav = np.concatenate([wav, pause(prosody.pause_ms / 1000, sr)])
        return wav, sr
