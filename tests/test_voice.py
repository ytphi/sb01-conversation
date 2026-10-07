#!/usr/bin/env python3
"""
test_voice.py  -  offline check of the optional Chatterbox voice

The real conversation program and the real teleop/voice_chatterbox.py, with a
stand-in voice engine in place of the Chatterbox models (no model, GPU, robot,
Claude or network needed). Same stand-ins as tests/test_language.py otherwise.

  python3 -m unittest tests.test_voice -v      (from the project root)
"""

import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests import test_language as base     # loads the real script once, with stand-ins
from teleop import voice_chatterbox as vc

script = base.script
RATE = 24000                                # what the stand-in engine produces
SECONDS_PER_LETTER = 0.05
EDGE = 0.1                                  # silence the stand-in puts at each end


class _Engine:
    """Stands in for the speech framework's SpeechSynth: a tone as long as the text."""

    def __init__(self):
        self.preloads, self.said, self.fail, self.bad = 0, [], None, None

    def preload(self):
        self.preloads += 1
        if isinstance(self.fail, Exception):
            raise self.fail

    def synthesize(self, text, language):
        self.said.append((text, language))
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.bad is not None:
            return self.bad
        tone = 0.5 * np.sin(np.arange(round(len(text) * SECONDS_PER_LETTER * RATE)) * 0.05, dtype=np.float32)
        quiet = np.zeros(round(EDGE * RATE), dtype=np.float32)
        return np.concatenate((quiet, tone, quiet)), RATE


def _to_pcm16(wave, rate, target):
    index = np.arange(0, len(wave), rate / target).astype(int)
    return (np.clip(wave[index], -1, 1) * 32767).astype(np.int16).tobytes()


def _voice(engine=None):
    return vc.ChatterboxVoice(synth=engine or _Engine(), to_pcm16=_to_pcm16)


class _Robot:
    """Robot audio and gesture client in one, recording the order of everything."""

    def __init__(self):
        self.events, self.played, self.starts = [], [], []

    def LedControl(self, *rgb):
        pass

    def PlayStream(self, app, stream, pcm):
        self.events.append("audio")
        self.played.append(bytes(pcm))

    def start(self, pcm, **kwargs):
        self.events.append("gesture-start")
        self.starts.append((pcm, kwargs))
        return True

    def begin(self, delay=0.0):
        self.events.append("gesture-begin")

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _loop(engine=None, gestures=True):
    loop = base._new_loop()
    robot = _Robot()
    loop.audio = robot
    loop.gestures = robot if gestures else None
    loop.tts_done.wait = lambda timeout=None: True
    if engine is not False:
        loop.voice = _voice(engine)
        loop.voice.load()
    return loop, robot


def _speak(loop, text, language=None):
    out = io.StringIO()
    with mock.patch.object(script.time, "sleep", lambda seconds: None), contextlib.redirect_stdout(out):
        loop._speak(text, language)
    return out.getvalue()


class ChoosingTheVoice(unittest.TestCase):
    def test_edge_tts_is_the_default_and_nothing_is_loaded(self):
        self.assertEqual(script.VOICE_BACKEND, "edge")
        loop, robot = _loop(engine=False)
        with mock.patch.object(script, "ChatterboxVoice", side_effect=AssertionError("must not be created")):
            loop._load_voice()
        self.assertIsNone(loop.voice)
        base.tts_calls.clear()
        _speak(loop, "Hello there, nice to meet you.")
        self.assertEqual(len(base.tts_calls), 1)                         # Edge TTS spoke
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)

    def test_the_setting_is_read_from_the_environment(self):
        with open(base.SCRIPT, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('VOICE_BACKEND = os.environ.get("SB01_VOICE", "").strip().lower() or "edge"', source)

    def test_an_unknown_voice_name_is_reported_and_edge_is_used(self):
        loop, _ = _loop(engine=False)
        out = io.StringIO()
        with mock.patch.object(script, "VOICE_BACKEND", "robotvoice"), contextlib.redirect_stdout(out):
            loop._load_voice()
        self.assertIsNone(loop.voice)
        self.assertIn("is not a voice", out.getvalue())
        self.assertIn("using Edge TTS", out.getvalue())

    def test_chatterbox_is_loaded_when_asked_for(self):
        loop, _ = _loop(engine=False)
        engine = _Engine()
        out = io.StringIO()
        with mock.patch.object(script, "VOICE_BACKEND", "chatterbox"), \
                mock.patch.object(script, "ChatterboxVoice", lambda: _voice(engine)), contextlib.redirect_stdout(out):
            loop._load_voice()
        self.assertIsNotNone(loop.voice)
        self.assertEqual(engine.preloads, 1)
        self.assertIn("Chatterbox ready", out.getvalue())
        self.assertIn("Edge TTS is the fallback", out.getvalue())


class WhenChatterboxIsNotAvailable(unittest.TestCase):
    def _load(self, **patches):
        loop, robot = _loop(engine=False)
        out = io.StringIO()
        with mock.patch.object(script, "VOICE_BACKEND", "chatterbox"), contextlib.redirect_stdout(out):
            with contextlib.ExitStack() as stack:
                for target, value in patches.items():
                    stack.enter_context(mock.patch.object(script, target, value))
                loop._load_voice()
        return loop, robot, out.getvalue()

    def _assert_edge_still_speaks(self, loop, robot):
        base.tts_calls.clear()
        _speak(loop, "Hello there, nice to meet you.")
        self.assertEqual(len(base.tts_calls), 1)
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"])     # gestures still run

    def test_a_missing_python_package_is_named_with_how_to_install_it(self):
        framework_modules = {name: None for name in ("yaml",)}
        forgotten = {name: sys.modules.pop(name) for name in ("settings", "speech_synth", "_paths", "tts_common")
                     if name in sys.modules}
        try:
            with mock.patch.dict(sys.modules, framework_modules):
                loop, robot, printed = self._load()
        finally:
            sys.modules.update(forgotten)
        self.assertIsNone(loop.voice)
        self.assertIn("Chatterbox is NOT in use", printed)
        self.assertIn("a Python package is missing (yaml)", printed)
        self.assertIn("pip install -r experimental/speech-framework/requirements.txt", printed)
        self.assertIn("using Edge TTS instead", printed)
        self._assert_edge_still_speaks(loop, robot)

    def test_a_missing_speech_framework_folder_is_reported(self):
        with mock.patch.object(vc, "FRAMEWORK_DIR", os.path.join(base.ROOT, "no-such-folder")):
            loop, robot, printed = self._load()
        self.assertIn("experimental/speech-framework is not in this copy", printed)
        self._assert_edge_still_speaks(loop, robot)

    def test_missing_model_files_are_reported(self):
        engine = _Engine()
        engine.fail = OSError("snapshot_download failed: model files for ResembleAI/chatterbox not found")
        loop, robot, printed = self._load(ChatterboxVoice=lambda: _voice(engine))
        self.assertIsNone(loop.voice)
        self.assertIn("a model or voice file could not be read or downloaded", printed)
        self._assert_edge_still_speaks(loop, robot)

    def test_a_full_graphics_card_is_reported_with_what_to_do(self):
        engine = _Engine()
        engine.fail = RuntimeError("CUDA out of memory. Tried to allocate 512.00 MiB")
        loop, robot, printed = self._load(ChatterboxVoice=lambda: _voice(engine))
        self.assertIsNone(loop.voice)
        self.assertIn("ran out of memory", printed)
        self.assertIn("SB01_CHATTERBOX_DEVICE=cpu", printed)
        self._assert_edge_still_speaks(loop, robot)

    def test_a_package_version_that_does_not_match_is_reported(self):
        engine = _Engine()
        engine.fail = TypeError("from_pretrained() got an unexpected keyword argument 'nano'")
        _, _, printed = self._load(ChatterboxVoice=lambda: _voice(engine))
        self.assertIn("does not match the speech framework", printed)


class SpeakingWithChatterbox(unittest.TestCase):
    def test_the_robot_plays_chatterbox_audio_and_edge_is_not_used(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        base.tts_calls.clear()
        printed = _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertEqual(printed, "")
        self.assertEqual(base.tts_calls, [])
        self.assertEqual(engine.said, [("Hello there, nice to meet you.", "en")])
        expected = loop.voice.synthesize("Hello there, nice to meet you.", "en")
        self.assertEqual(b"".join(robot.played), expected.pcm)
        self.assertEqual(len(expected.pcm) % 2, 0)

    def test_the_gesture_model_gets_the_same_speech_at_its_own_sample_rate(self):
        loop, robot = _loop()
        _speak(loop, "Hello there, nice to meet you.", "en")
        gesture_pcm, kwargs = robot.starts[0]
        self.assertEqual(kwargs, {})                                      # asked exactly as with Edge TTS
        self.assertAlmostEqual(len(gesture_pcm) / len(b"".join(robot.played)), 24000 / 16000, places=2)

    def test_audio_is_ready_before_the_gesture_and_the_gesture_begins_with_the_audio(self):
        loop, robot = _loop()
        _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"])
        self.assertFalse(loop.speaking.is_set())

    def test_two_replies_never_overlap(self):
        loop, robot = _loop()
        _speak(loop, "This is the first reply to you.", "en")
        _speak(loop, "And this is the second reply.", "en")
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"] * 2)

    def test_without_gestures_no_gesture_audio_is_made_and_speech_still_plays(self):
        loop, robot = _loop(gestures=False)
        with mock.patch.object(loop.voice, "synthesize", wraps=loop.voice.synthesize) as made:
            _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertFalse(made.call_args.kwargs["gesture_audio"])
        self.assertEqual(robot.events, ["audio"])

    def test_the_models_are_loaded_once_not_per_sentence(self):
        engine = _Engine()
        loop, _ = _loop(engine)
        for text in ("First thing to say to you.", "Second thing to say.", "Third thing to say."):
            _speak(loop, text, "en")
            loop.voice.load()
        self.assertEqual(engine.preloads, 1)
        self.assertEqual(len(engine.said), 3)

    def test_each_language_goes_to_the_matching_voice(self):
        engine = _Engine()
        loop, _ = _loop(engine)
        _speak(loop, "Hello there, nice to meet you.", "en")
        _speak(loop, "¡Hola! ¿Cómo estás hoy?", "es")
        _speak(loop, "Claro, con mucho gusto te ayudo", "es")                # no accents: the language decides
        _speak(loop, "你好，很高兴见到你。", "zh")
        _speak(loop, "Goodbye! It was great talking with you.", None)        # fixed English phrase
        self.assertEqual([language for _, language in engine.said], ["en", "es", "es", "zh", "en"])

    def test_the_name_and_campus_wording_reach_chatterbox_as_they_reach_edge(self):
        engine = _Engine()
        loop, _ = _loop(engine)
        _speak(loop, "Welcome to CSUSB, I am Yotie.", "en")
        self.assertEqual(engine.said[0][0], "Welcome to Cal State San Bernardino, I am Yotie.")


class FallingBackToEdge(unittest.TestCase):
    def test_one_failed_reply_is_spoken_with_edge_and_chatterbox_stays_on(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        engine.fail = RuntimeError("CUDA out of memory")
        base.tts_calls.clear()
        printed = _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertIn("Chatterbox could not say this", printed)
        self.assertIn("using Edge TTS for it", printed)
        self.assertEqual(len(base.tts_calls), 1)
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)   # the reply was still spoken, once
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"])
        self.assertIsNotNone(loop.voice)
        engine.fail = None
        robot.played.clear()
        _speak(loop, "Now it works again, thank you.", "en")
        self.assertEqual(len(base.tts_calls), 1)                           # back on Chatterbox
        self.assertEqual(loop._voice_failures, 0)

    def test_three_failures_in_a_row_leave_it_off_for_the_session(self):
        engine = _Engine()
        loop, _ = _loop(engine)
        engine.fail = RuntimeError("boom")
        printed = "".join(_speak(loop, f"Reply number {i} for you.", "en") for i in range(3))
        self.assertIn("failed 3 times in a row", printed)
        self.assertIsNone(loop.voice)
        said = len(engine.said)
        _speak(loop, "One more reply for you.", "en")
        self.assertEqual(len(engine.said), said)                           # not tried again

    def test_unusable_audio_is_refused_with_a_reason(self):
        tone = np.full(RATE, 0.3, dtype=np.float32)
        cases = {
            "no audio": (np.zeros(0, dtype=np.float32), RATE),
            "damaged audio": (np.array([0.1, np.nan, 0.2], dtype=np.float32), RATE),
            "silence": (np.zeros(RATE, dtype=np.float32), RATE),
            "impossible sample rate": (tone, 0),
            "which cannot be right": (np.full(RATE * 61, 0.3, dtype=np.float32), RATE),
            "not audio": ("oops", RATE),
        }
        for reason, bad in cases.items():
            with self.subTest(reason=reason):
                engine = _Engine()
                engine.bad = bad
                loop, robot = _loop(engine)
                base.tts_calls.clear()
                printed = _speak(loop, "Hello there, nice to meet you.", "en")
                self.assertIn(reason, printed)
                self.assertEqual(len(base.tts_calls), 1)                   # Edge spoke it
                self.assertEqual(b"".join(robot.played), base._Segment.raw_data)

    def test_nothing_to_say_is_refused(self):
        with self.assertRaises(vc.VoiceUnavailable):
            _voice().synthesize("   ", "en")


class TeachingGesturesWithChatterbox(unittest.TestCase):
    REPLY = "That is a very good answer today. Now look at the [point] diagram on the board."

    def test_sentences_are_measured_and_the_gesture_lands_inside_its_sentence(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        with mock.patch.object(script, "GESTURE_CUES", True):
            printed = _speak(loop, self.REPLY, "en")
        first, second = "That is a very good answer today.", "Now look at the diagram on the board."
        self.assertEqual([text for text, _ in engine.said], [first, second])       # one by one, marks removed
        _, kwargs = robot.starts[0]
        self.assertEqual([cue["name"] for cue in kwargs["cues"]], ["point"])
        first_len = 2 * EDGE + len(first) * SECONDS_PER_LETTER
        second_start = first_len + vc.SENTENCE_GAP_SECONDS
        second_end = second_start + 2 * EDGE + len(second) * SECONDS_PER_LETTER
        when = kwargs["cues"][0]["time"]
        self.assertGreater(when, second_start)
        self.assertLess(when, second_end)
        expected = second_start + EDGE + len(second) * SECONDS_PER_LETTER * second.index("diagram") / len(second)
        self.assertAlmostEqual(when, expected, delta=0.05)
        self.assertIn("placed by estimate", printed)                              # it says so
        self.assertIn("no word times", printed)

    def test_the_estimate_notice_is_printed_once(self):
        loop, _ = _loop()
        with mock.patch.object(script, "GESTURE_CUES", True):
            first = _speak(loop, self.REPLY, "en")
            second = _speak(loop, self.REPLY, "en")
        self.assertIn("placed by estimate", first)
        self.assertNotIn("placed by estimate", second)

    def test_with_teaching_gestures_off_the_reply_is_spoken_in_one_piece(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        _speak(loop, "That is a very good answer today. Now look at the diagram.", "en")
        self.assertEqual(len(engine.said), 1)
        self.assertEqual(robot.starts[0][1], {})

    def test_word_times_run_forward_and_stay_inside_the_audio(self):
        speech = _voice().synthesize("One two three four. Five six seven eight nine ten.", "en", split=True)
        times = [when for when, _ in speech.words]
        self.assertEqual([word for _, word in speech.words],
                         ["One", "two", "three", "four", "Five", "six", "seven", "eight", "nine", "ten"])
        self.assertEqual(times, sorted(times))
        self.assertGreaterEqual(times[0], EDGE - 0.01)                             # after the leading silence
        self.assertLess(times[-1], speech.seconds)

    def test_chinese_is_timed_character_by_character(self):
        speech = _voice().synthesize("现在请看黑板。", "zh", split=True)
        self.assertEqual("".join(word for _, word in speech.words), "现在请看黑板")

    def test_edge_tts_word_times_are_not_called_estimates(self):
        loop, _ = _loop(engine=False)
        with mock.patch.object(script, "GESTURE_CUES", True):
            printed = _speak(loop, self.REPLY, "en")
        self.assertNotIn("estimate", printed)
        self.assertFalse(loop._words_estimated)


class TheWholeProgram(unittest.TestCase):
    """setup() and run() end to end with stand-ins: greeting, one reply, goodbye."""

    def _run(self, backend, engine=None):
        import queue
        loop, robot = _loop(engine=False)
        loop.asr_queue.put(("How do robots move", ""))
        take = loop.asr_queue.get

        def get(timeout=None):
            try:
                return take(timeout=0.01)
            except queue.Empty:
                raise KeyboardInterrupt          # nothing left to say: the operator stops the program

        loop.asr_queue.get = get
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(script, "VOICE_BACKEND", backend))
            stack.enter_context(mock.patch.object(script, "ChatterboxVoice", lambda: _voice(engine)))
            stack.enter_context(mock.patch.object(script.time, "sleep", lambda seconds: None))
            stack.enter_context(contextlib.redirect_stdout(out))
            loop.setup()
            loop.audio, loop.gestures = robot, robot          # setup() made its own stand-ins; watch these
            loop.run()
        return loop, robot, out.getvalue()

    def test_it_starts_and_runs_with_edge_tts(self):
        base.tts_calls.clear()
        loop, robot, printed = self._run("edge")
        self.assertIsNone(loop.voice)
        self.assertNotIn("[voice]", printed)
        self.assertEqual(len(base.tts_calls), 3)                          # greeting, reply, goodbye
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"] * 3)

    def test_it_starts_and_runs_with_chatterbox(self):
        engine = _Engine()
        base.tts_calls.clear()
        loop, robot, printed = self._run("chatterbox", engine)
        self.assertIn("Chatterbox ready", printed)
        self.assertEqual(engine.preloads, 1)
        self.assertEqual(len(engine.said), 3)                             # greeting, reply, goodbye
        self.assertEqual(base.tts_calls, [])                              # Edge TTS never needed
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"] * 3)
        self.assertIn("Yotie", engine.said[0][0])                         # the greeting uses the name

    def test_it_starts_and_runs_when_chatterbox_cannot_load(self):
        engine = _Engine()
        engine.fail = OSError("model files not found")
        base.tts_calls.clear()
        loop, robot, printed = self._run("chatterbox", engine)
        self.assertIn("Chatterbox is NOT in use", printed)
        self.assertIsNone(loop.voice)
        self.assertEqual(len(base.tts_calls), 3)                          # everything was still said
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"] * 3)


class Sentences(unittest.TestCase):
    def test_nothing_is_lost_or_repeated(self):
        for text in ("One sentence only", "First one here. Second one here! Third one?", "Yes. That is right.",
                     "回答得很好。对，完全正确。", "Ask Dr. Smith about the schedule. He knows."):
            pieces = vc.sentences(text)
            self.assertEqual("".join("".join(pieces).split()), "".join(text.split()), text)

    def test_a_very_short_sentence_stays_with_the_next(self):
        self.assertEqual(vc.sentences("Yes. That is exactly right."), ["Yes. That is exactly right."])


class NothingSensitiveIsTracked(unittest.TestCase):
    """No model weights, voice clips, generated audio or secrets belong in git."""

    FORBIDDEN = (".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".bin", ".gguf", ".wav", ".mp3", ".flac",
                 ".ogg", ".m4a", ".npy", ".npz", ".pem", ".key")

    def test_tracked_files(self):
        try:
            listed = subprocess.run(["git", "ls-files"], cwd=base.ROOT, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git is not available")
        if listed.returncode != 0:
            self.skipTest("not a git checkout")
        files = listed.stdout.split("\n")
        self.assertGreater(len(files), 20)
        bad = [name for name in files if name.lower().endswith(self.FORBIDDEN)
               or os.path.basename(name) in (".env", "env", "config.local.yaml")]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
