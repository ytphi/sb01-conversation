"""
chatterbox_nano.py  -  Chatterbox Nano (110M, English only, fast on CPU/edge)

pip install chatterbox-tts
Supports paralinguistic tags in text, e.g. "[laugh]", "[chuckle]".
Has no pitch / speed / emotion knobs, so a Prosody plan only adds its pause.
"""

import numpy as np

from tts_common import ChatterboxEngine, pause


class ChatterboxNano(ChatterboxEngine):
    name = "chatterbox-nano"
    languages = frozenset({"en"})
    sound_tags = True

    def _load_model(self, device: str):
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        return ChatterboxTurboTTS.from_pretrained(device=device, nano=True)

    def _prepare(self, clip: str):
        # chatterbox 0.1.7: Turbo's loudness normalisation returns float64 and the
        # model then fails with "expected Double but found Float"; skip it
        self.model.prepare_conditionals(clip, norm_loudness=False)

    def synthesize(self, text, lang="en", voice=None, prosody=None, **params):
        self.load()
        self._use_voice(voice)
        wav, sr = self._to_numpy(self.model.generate(text, **params))
        if prosody and prosody.pause_ms:
            wav = np.concatenate([wav, pause(prosody.pause_ms / 1000, sr)])
        return wav, sr
