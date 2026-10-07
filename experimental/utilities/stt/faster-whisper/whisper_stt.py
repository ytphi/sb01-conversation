"""
whisper_stt.py  -  faster-whisper (Whisper on CTranslate2), speech-to-text on the PC

pip install faster-whisper
Models download from Hugging Face on first use. Rough guide:
  base / small      fast on CPU, decent English
  distil-large-v3   English only, close to large-v3 accuracy, fast on GPU
  large-v3-turbo    multilingual, best accuracy/speed trade-off on GPU
"""

from stt_common import STTEngine


class FasterWhisper(STTEngine):
    name = "faster-whisper"

    def __init__(self, model: str = "small", device: str = "auto", compute_type: str = "default",
                 language: str | None = None, beam_size: int = 1,
                 no_speech_threshold: float = 0.6, **options):
        self.model_name   = model
        self.device       = device              # auto | cuda | cpu
        self.compute_type = compute_type        # default | float16 | int8_float16 | int8
        self.language     = language            # None = auto-detect per utterance
        self.beam_size    = beam_size
        self.no_speech_threshold = no_speech_threshold
        self.options      = options             # passed straight to transcribe()
        self.model = None

    def load(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            print(f"[stt] loading {self.name} {self.model_name} on {self.device}...")
            self.model = WhisperModel(self.model_name, device=self.device,
                                      compute_type=self.compute_type)

    def transcribe(self, audio) -> str:
        self.load()
        segments, _info = self.model.transcribe(
            audio, language=self.language, beam_size=self.beam_size,
            condition_on_previous_text=False, **self.options)
        # Drop segments Whisper itself thinks are noise ("Thank you." hallucinations)
        return " ".join(s.text.strip() for s in segments
                        if s.no_speech_prob < self.no_speech_threshold).strip()
