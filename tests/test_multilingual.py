"""
Conversation in every language Chatterbox Multilingual speaks (teleop/languages.py).

Run:  python3 -m unittest tests.test_multilingual

Claude, Edge TTS, Chatterbox and Whisper are stand-ins: these tests check which
language is chosen, what Claude is told, and which language and voice the reply
is spoken in.
"""

import asyncio
import types
import unittest
from unittest import mock

from tests import test_language as base     # loads the real script once, with stand-ins

script = base.script
languages = script.languages


def _ask(loop, text, whisper=None):
    """Send one phrase through _ask_claude; returns the language note Claude was given."""
    loop._ask_claude(text, "", whisper)
    return base.claude_calls[-1]["messages"][-1]["content"].split("]")[0] + "]"


class _Chatterbox:
    """Records the language each reply is synthesized in."""

    def __init__(self):
        self.languages = []

    def synthesize(self, text, language, split=False, gesture_audio=False):
        self.languages.append(language)
        return types.SimpleNamespace(pcm=b"\0\0" * 160, gesture_pcm=None, scale=1.0, words=[])


class TheLanguageList(unittest.TestCase):
    def test_it_is_chatterbox_multilinguals_list(self):
        self.assertEqual(set(languages.NAMES), {
            "ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it", "ja",
            "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv", "sw", "tr", "zh"})

    def test_every_language_has_an_edge_voice_of_its_own(self):
        self.assertEqual(set(languages.EDGE_VOICES), set(languages.NAMES))
        self.assertEqual(len(set(languages.EDGE_VOICES.values())), len(languages.NAMES))

    def test_the_original_three_voices_are_unchanged(self):
        self.assertEqual(languages.EDGE_VOICES["en"], script.VOICE_EN)
        self.assertEqual(languages.EDGE_VOICES["es"], script.VOICE_ES)
        self.assertEqual(languages.EDGE_VOICES["zh"], script.VOICE_ZH)


class WritingSystems(unittest.TestCase):
    def test_the_writing_system_shows_the_language(self):
        cases = {
            "こんにちは、元気ですか": "ja",      # kana, even beside Chinese characters
            "今日は天気がいいですね": "ja",
            "你好，今天怎么样": "zh",
            "안녕하세요 반갑습니다": "ko",
            "Привет, как дела": "ru",
            "مرحبا كيف حالك": "ar",
            "שלום מה שלומך": "he",
            "Γεια σου τι κάνεις": "el",
            "नमस्ते आप कैसे हैं": "hi",
        }
        for text, language in cases.items():
            with self.subTest(text=text):
                self.assertEqual(languages.script_language(text), language)
                self.assertEqual(script.detect_language(text), language)

    def test_latin_script_is_left_to_the_conversation(self):
        self.assertIsNone(languages.script_language("Bonjour, comment ça va"))
        self.assertIsNone(languages.script_language("Hello there"))


class WhisperDecidesTheLanguage(unittest.TestCase):
    def test_a_clear_sentence_in_any_language_switches(self):
        loop = base._new_loop()
        cases = [
            ("Bonjour, comment allez-vous aujourd'hui", "fr", "French"),
            ("Wie geht es dir heute", "de", "German"),
            ("Come stai oggi amico mio", "it", "Italian"),
            ("今日はいい天気ですね", "ja", "Japanese"),
            ("Hello, how are you today", "en", "English"),
        ]
        for text, code, name in cases:
            with self.subTest(code=code):
                self.assertEqual(_ask(loop, text, (code, 0.95)), f"[Reply in {name}.]")

    def test_a_greeting_switches(self):
        loop = base._new_loop()
        self.assertEqual(_ask(loop, "Bonjour", ("fr", 0.9)), "[Reply in French.]")

    def test_a_short_phrase_does_not_switch_even_when_whisper_is_sure(self):
        loop = base._new_loop()
        self.assertEqual(_ask(loop, "oui oui", ("fr", 0.99)), "[Reply in English.]")

    def test_an_unsure_guess_does_not_switch(self):
        loop = base._new_loop()
        self.assertEqual(_ask(loop, "Bonjour, comment allez-vous aujourd'hui", ("fr", 0.4)),
                         "[Reply in English.]")

    def test_a_language_the_voice_cannot_speak_does_not_switch(self):
        loop = base._new_loop()
        _ask(loop, "Hola, ¿cómo estás hoy?", ("es", 0.9))
        self.assertEqual(_ask(loop, "สวัสดีครับ วันนี้เป็นอย่างไรบ้าง", ("th", 0.99)), "[Reply in Spanish.]")

    def test_whisper_outranks_the_text_guess(self):
        loop = base._new_loop()
        # Spanish-looking words, but Whisper heard Portuguese
        self.assertEqual(_ask(loop, "como você está hoje", ("pt", 0.9)), "[Reply in Portuguese.]")

    def test_whispers_other_names_for_a_language_are_understood(self):
        self.assertEqual(languages.from_whisper("yue"), "zh")
        self.assertEqual(languages.from_whisper("nn"), "no")
        self.assertIsNone(languages.from_whisper("th"))
        self.assertIsNone(languages.from_whisper(None))


class TheReplyIsSpokenInTheConversationLanguage(unittest.TestCase):
    def test_french_with_accents_is_not_mistaken_for_spanish(self):
        loop = base._new_loop()
        self.assertEqual(base._voice_for(loop, "Très bien, à bientôt et merci.", "fr"), "fr-FR-DeniseNeural")

    def test_chatterbox_is_given_the_conversation_language(self):
        loop = base._new_loop()
        loop.voice = _Chatterbox()
        for text, language in (("Très bien, merci.", "fr"), ("Sehr gut, danke.", "de"),
                               ("Muito bem, obrigado.", "pt"), ("Hello there.", "en"),
                               ("Muy bien, gracias.", "es")):
            asyncio.run(loop._text_to_pcm(text, language))
        self.assertEqual(loop.voice.languages, ["fr", "de", "pt", "en", "es"])

    def test_the_writing_system_wins_over_the_conversation_language(self):
        loop = base._new_loop()
        loop.voice = _Chatterbox()
        asyncio.run(loop._text_to_pcm("こんにちは", "en"))
        asyncio.run(loop._text_to_pcm("안녕하세요", "fr"))
        self.assertEqual(loop.voice.languages, ["ja", "ko"])

    def test_every_language_gets_its_own_edge_voice(self):
        loop = base._new_loop()
        for code, voice in languages.EDGE_VOICES.items():
            with self.subTest(code=code):
                self.assertEqual(base._voice_for(loop, "Some reply text.", code), voice)

    def test_an_edge_voice_that_fails_falls_back_to_the_english_voice(self):
        loop = base._new_loop()
        tried = []

        class Flaky:
            def __init__(self, text, voice):
                tried.append(voice)
                self.voice = voice

            async def stream(self):
                if self.voice != script.VOICE_EN:
                    raise RuntimeError("voice not found")
                yield {"type": "audio", "data": b"mp3"}

        with mock.patch.object(script.edge_tts, "Communicate", Flaky, create=True):
            pcm, _ = asyncio.run(loop._text_to_pcm("Bună ziua", "sw"))
        self.assertEqual(tried, ["sw-KE-ZuriNeural", script.VOICE_EN])
        self.assertTrue(pcm)


class WhisperLanguageIsRecorded(unittest.TestCase):
    def test_the_detected_language_is_kept_and_the_text_is_unchanged(self):
        info = types.SimpleNamespace(language="fr", language_probability=0.93)

        class Model:
            def transcribe(self, audio, **kwargs):
                return iter(["segment"]), info

        class Engine:
            model = None

            def load(self):
                self.model = self.model or Model()

        engine = Engine()
        script.stt_whisper._record_language(engine)
        engine.load()
        segments, got = engine.model.transcribe("audio")
        self.assertEqual(list(segments), ["segment"])
        self.assertIs(got, info)
        self.assertEqual(engine.last_language, ("fr", 0.93))
        engine.load()                                   # loading again does not wrap twice
        engine.model.transcribe("audio")
        self.assertEqual(script.stt_whisper.last_language(types.SimpleNamespace(engine=engine)), ("fr", 0.93))

    def test_transcripts_carry_whispers_language_to_the_conversation(self):
        import queue
        import os

        class ASR:
            def __init__(self):
                self.utterances = queue.Queue()
                self.engine = types.SimpleNamespace(last_language=("de", 0.88))

            def listen(self):
                return self.utterances.get()

        loop = base._new_loop()
        with mock.patch.dict(os.environ, {"SB01_STT": "g1-mic"}), \
             mock.patch.object(script.stt_whisper, "start_local_asr", lambda *a, **k: ASR()):
            loop._start_stt()
        loop.local_asr.utterances.put("Wie geht es dir heute")
        self.assertEqual(loop.asr_queue.get(timeout=2), ("Wie geht es dir heute", "", ("de", 0.88)))


if __name__ == "__main__":
    unittest.main()
