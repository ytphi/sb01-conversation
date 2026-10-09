"""
Speech recognition on the PC (teleop/stt_whisper.py) inside the conversation script.

Run:  python3 -m unittest tests.test_stt_whisper

Whisper and the robot are stand-ins: these tests check the wiring (which
recognizer is used, what reaches the conversation, the fallback, the timings),
not recognition quality.
"""

import json
import os
import queue
import threading
import time
import types
import unittest
from unittest import mock

from tests import test_language as base     # loads the real script once, with stand-ins

script = base.script
stt_whisper = script.stt_whisper


class _StandInASR:
    """Behaves like the framework's LocalASRInput: listen() blocks for the next utterance."""

    def __init__(self):
        self.utterances: queue.Queue = queue.Queue()

    def listen(self):
        return self.utterances.get()


def _loop_with(source: str, start=None):
    """A conversation loop whose setup ran _start_stt with SB01_STT=source."""
    loop = base._new_loop()
    with mock.patch.dict(os.environ, {"SB01_STT": source}), \
         mock.patch.object(stt_whisper, "start_local_asr", start or (lambda *a, **k: _StandInASR())):
        loop._start_stt()
    return loop


def _onboard(loop, payload: dict):
    loop._asr_callback(types.SimpleNamespace(data=json.dumps(payload)))


def _next(loop, timeout=2.0):
    return loop.asr_queue.get(timeout=timeout)


class WhisperIsUsed(unittest.TestCase):
    def test_a_transcript_reaches_the_conversation_without_an_emotion(self):
        loop = _loop_with("g1-mic")
        self.assertEqual(loop.stt_source, "g1-mic")
        loop.local_asr.utterances.put("How do robots see")
        self.assertEqual(_next(loop)[:2], ("How do robots see", ""))

    def test_the_onboard_asr_text_is_ignored_while_whisper_listens(self):
        loop = _loop_with("g1-mic")
        _onboard(loop, {"text": "something the robot heard", "is_final": True, "emotion": "<|HAPPY|>"})
        self.assertTrue(loop.asr_queue.empty())

    def test_play_state_still_ends_each_reply(self):
        loop = _loop_with("g1-mic")
        loop.tts_done.clear()
        _onboard(loop, {"play_state": 0})
        self.assertTrue(loop.tts_done.is_set())

    def test_what_is_heard_while_the_robot_speaks_is_dropped(self):
        loop = _loop_with("g1-mic")
        loop.speaking.set()
        loop.local_asr.utterances.put("its own voice")
        time.sleep(0.1)
        loop.speaking.clear()
        loop.local_asr.utterances.put("a real question")
        self.assertEqual(_next(loop)[:2], ("a real question", ""))

    def test_noise_is_dropped(self):
        loop = _loop_with("g1-mic")
        loop.local_asr.utterances.put(".")
        loop.local_asr.utterances.put("a")
        loop.local_asr.utterances.put("Hi")
        self.assertEqual(_next(loop)[:2], ("Hi", ""))

    def test_the_speaking_flag_is_shared_so_its_own_voice_is_discarded_at_the_source(self):
        seen = {}
        def start(source, speaking, config):
            seen["speaking"] = speaking
            return _StandInASR()
        loop = _loop_with("pc-mic", start)
        self.assertIs(seen["speaking"], loop.speaking)


class OnboardASR(unittest.TestCase):
    def test_sb01_stt_g1_asr_keeps_the_old_path(self):
        started = []
        loop = _loop_with("g1-asr", lambda *a, **k: started.append(1))
        self.assertEqual(started, [])
        self.assertIsNone(loop.local_asr)
        base._hear(loop, "Hello there")
        self.assertEqual(_next(loop)[0], "Hello there")

    def test_when_whisper_cannot_start_the_onboard_asr_is_used(self):
        def fail(*a, **k):
            raise stt_whisper.STTUnavailable("a Python package is missing (faster_whisper)")
        loop = _loop_with("g1-mic", fail)
        self.assertIsNone(loop.local_asr)
        self.assertEqual(loop.stt_source, "g1-asr")
        base._hear(loop, "Hello there")
        self.assertEqual(_next(loop)[0], "Hello there")

    def test_an_unknown_source_uses_the_onboard_asr(self):
        loop = _loop_with("microphone-please")
        self.assertIsNone(loop.local_asr)

    def test_partials_wait_the_framework_debounce(self):
        loop = _loop_with("g1-asr")
        made = []
        real_timer = threading.Timer
        def timer(interval, fn):
            made.append(interval)
            return real_timer(interval, fn)
        with mock.patch.object(script.threading, "Timer", timer):
            base._hear(loop, "half a sentence", final=False)
        loop._asr_timer.cancel()
        self.assertEqual(made, [loop.timings["debounce_s"]])


class Timings(unittest.TestCase):
    def test_config_values_are_used(self):
        config = {"robot": {"playback_grace_s": 0.7, "echo_tail_s": 0.2},
                  "stt": {"g1_asr": {"debounce_s": 0.6}}}
        self.assertEqual(stt_whisper.timings(config),
                         {"debounce_s": 0.6, "playback_grace_s": 0.7, "echo_tail_s": 0.2})

    def test_without_a_config_the_framework_defaults_are_used(self):
        self.assertEqual(stt_whisper.timings({}), {"debounce_s": 0.8, "playback_grace_s": 0.5, "echo_tail_s": 0.3})

    def test_a_reply_waits_its_length_plus_the_grace_then_the_echo_tail(self):
        loop = base._new_loop()
        loop.timings = {"debounce_s": 0.8, "playback_grace_s": 0.5, "echo_tail_s": 0.3}
        waits, sleeps = [], []
        loop.tts_done.wait = lambda timeout=None: waits.append(timeout) or True
        pcm = b"\0" * 32000                                  # one second of 16 kHz 16-bit audio
        async def to_pcm(text, language):
            return pcm, None
        loop._text_to_pcm = to_pcm
        with mock.patch.object(script.time, "sleep", sleeps.append):
            loop._speak("Hello")
        self.assertAlmostEqual(waits[0], 1.5, delta=0.1)
        self.assertEqual(sleeps[-1], 0.3)


class Overrides(unittest.TestCase):
    def test_model_and_language_can_be_set_from_the_environment(self):
        config = {"stt": {"whisper": {"model": "small", "language": None}}}
        with mock.patch.dict(os.environ, {"SB01_WHISPER_MODEL": "large-v3-turbo", "SB01_WHISPER_LANGUAGE": "en"}):
            whisper = stt_whisper.stt_settings(config)["whisper"]
        self.assertEqual((whisper["model"], whisper["language"]), ("large-v3-turbo", "en"))

    def test_language_auto_means_detect(self):
        config = {"stt": {"whisper": {"language": "en"}}}
        with mock.patch.dict(os.environ, {"SB01_WHISPER_LANGUAGE": "auto"}):
            self.assertIsNone(stt_whisper.stt_settings(config)["whisper"]["language"])

    def test_the_source_comes_from_the_config_unless_sb01_stt_is_set(self):
        config = {"stt": {"source": "pc-mic"}}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SB01_STT", None)
            self.assertEqual(stt_whisper.chosen_source(config), "pc-mic")
        with mock.patch.dict(os.environ, {"SB01_STT": "g1-asr"}):
            self.assertEqual(stt_whisper.chosen_source(config), "g1-asr")


if __name__ == "__main__":
    unittest.main()
