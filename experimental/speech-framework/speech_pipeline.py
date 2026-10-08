"""
speech_pipeline.py  -  reply text → sentences → TTS → player, as overlapping stages

  LLM thread       pulls text deltas, splits sentences (first clause flushed early)
  TTS (this one)   parses each sentence's tags into phrases and synthesizes them
  player thread    plays audio while the next sentence is synthesized

  SpeechPipeline.speak(text, lang)          whole reply (greeting, goodbye, --say)
  SpeechPipeline.speak_stream(sentences)    an iterator of sentences, e.g. Conversation.respond_stream

A player is anything with begin() / play(wav, sr) / finish(): the G1 speaker
(run_robot.py) or the PC speakers (run_local.py).
"""

import queue
import threading
import time
from collections.abc import Iterable
from typing import Protocol

import numpy as np

from lang_detect import detect_language
from reply_tags import Phrase, parse_sentence, split_sentences, strip_tags
from timing_log import TurnTimer

_DONE = object()


def _merge(phrases: list[Phrase]) -> list[Phrase]:
    """One phrase per sentence, for engines that can't vary tone mid-sentence."""
    spoken = [p for p in phrases if p.text]
    if len(spoken) <= 1:
        return phrases
    first = spoken[0]
    first.prosody.pause_ms = phrases[-1].prosody.pause_ms
    return [Phrase(" ".join(p.text for p in spoken), " ".join(p.tts_text for p in spoken),
                   first.prosody, [], [], first.lang)]


class AudioPlayer(Protocol):
    def begin(self, timer: TurnTimer | None = None) -> None: ...
    def play(self, wav: np.ndarray, sr: int) -> None: ...
    def finish(self) -> None: ...


class SpeechPipeline:
    def __init__(self, synth, player: AudioPlayer, default_language: str = "en",
                 on_sentence=None):
        self.synth    = synth
        self.player   = player
        self.default_language = default_language
        self.on_sentence = on_sentence          # callback(sentence_text, lang) e.g. for printing

    def speak(self, text: str, lang: str | None = None, timer: TurnTimer | None = None):
        self.speak_stream(split_sentences(text), lang, timer)

    def speak_stream(self, sentences: Iterable[str], lang: str | None = None,
                     timer: TurnTimer | None = None):
        q: queue.Queue = queue.Queue()

        def produce():
            try:
                for s in sentences:
                    q.put(s)
            except Exception as exc:                 # the LLM side already logged / fell back
                print(f"[error] reply stream: {exc}")
            finally:
                q.put(_DONE)

        threading.Thread(target=produce, daemon=True, name="llm-stream").start()

        self.player.begin(timer)
        n_sentences, audio_s, tts_s = 0, 0.0, 0.0
        reply_lang = lang
        try:
            while True:
                sentence = q.get()
                if sentence is _DONE:
                    break
                spoken = strip_tags(sentence)
                if not spoken and "<pause" not in sentence:
                    continue
                s_lang = lang or detect_language(spoken, reply_lang or self.default_language)
                reply_lang = reply_lang or s_lang
                if self.on_sentence:
                    self.on_sentence(spoken, s_lang)
                phrases = parse_sentence(sentence, s_lang)
                if not getattr(self.synth, "wants_tone_tags", False):
                    phrases = _merge(phrases)        # Chatterbox: one call per sentence
                for phrase in phrases:
                    t0 = time.time()
                    try:
                        if phrase.text:
                            wav, sr = self.synth.synthesize(phrase.text, s_lang, phrase.prosody,
                                                            tts_text=phrase.tts_text)
                        else:
                            sr = 24000
                            wav = np.zeros(int(sr * (phrase.prosody.pause_ms or 0) / 1000), np.float32)
                    except Exception as exc:
                        print(f"[error] TTS: {exc}")
                        continue
                    tts_s += time.time() - t0
                    if timer:
                        timer.mark("first_audio_ready")
                    audio_s += len(wav) / sr
                    self.player.play(wav, sr)
                n_sentences += 1
        finally:
            self.player.finish()
            if timer:
                timer.mark("playback_end")
                timer.add(sentences=n_sentences, audio_s=round(audio_s, 2), tts_s=round(tts_s, 2))
