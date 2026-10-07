#!/usr/bin/env python3
"""
test_gesture_cues.py  -  offline check of teaching-gesture marks in replies

The marks ("look at the [point] diagram") are taken out of what is spoken and
turned into (gesture, time) pairs for the sidecar. No robot, no Claude, no
network: same stand-ins as tests/test_language.py.

  python3 -m unittest tests.test_gesture_cues -v      (from the project root)
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests import test_language as base     # loads the real script once, with stand-ins
from teleop import gesture_cues

script = base.script
REPLY = "Great answer. [yes] Yes, that is right. Look at the [point] diagram on the board."
SPOKEN = "Great answer. Yes, that is right. Look at the diagram on the board."
WORDS = [(0.10, "Great"), (0.50, "answer"), (1.20, "Yes"), (1.60, "that"), (1.80, "is"), (2.00, "right"),
         (2.60, "Look"), (2.80, "at"), (2.90, "the"), (3.10, "diagram"), (3.60, "on"), (3.70, "the"), (3.80, "board")]


class Marks(unittest.TestCase):
    def test_marks_are_taken_out_of_the_spoken_text(self):
        spoken, marks = gesture_cues.split(REPLY)
        self.assertEqual(spoken, SPOKEN)
        self.assertEqual([name for name, _ in marks], ["yes", "point"])
        self.assertEqual(spoken[marks[0][1]:].split()[0], "Yes,")           # each mark sits at its word
        self.assertEqual(spoken[marks[1][1]:].split()[0], "diagram")

    def test_text_without_marks_is_untouched(self):
        for text in ("Hello there!", "Use list[0] in Python.", "¿Cómo estás?", "你好"):
            self.assertEqual(gesture_cues.split(text), (text, []))

    def test_an_unknown_mark_is_removed_and_makes_no_gesture(self):
        spoken, marks = gesture_cues.split("That is [dance] wonderful and [YES] correct.")
        self.assertEqual(spoken, "That is wonderful and correct.")
        self.assertEqual([name for name, _ in marks], ["yes"])

    def test_every_cue_name_is_recognized(self):
        text = " ".join(f"[{name}] word" for name in gesture_cues.CUE_NAMES)
        self.assertEqual([name for name, _ in gesture_cues.split(text)[1]], list(gesture_cues.CUE_NAMES))

    def test_no_more_than_the_limit(self):
        spoken, marks = gesture_cues.split("[yes] ok " * 30)
        self.assertEqual(len(marks), gesture_cues.MAX_CUES)
        self.assertNotIn("[", spoken)

    def test_marks_work_in_spanish_and_chinese(self):
        spoken, marks = gesture_cues.split("¡[yes] Sí, correcto! 请看[point]黑板。")
        self.assertEqual(spoken, "¡Sí, correcto! 请看黑板。")
        self.assertEqual([name for name, _ in marks], ["yes", "point"])


class Timing(unittest.TestCase):
    def test_a_cue_gets_the_time_of_its_word(self):
        spoken, marks = gesture_cues.split(REPLY)
        self.assertEqual(gesture_cues.timed(marks, WORDS, spoken),
                         [{"name": "yes", "time": 1.2}, {"name": "point", "time": 3.1}])

    def test_a_repeated_word_is_matched_in_order(self):
        spoken, marks = gesture_cues.split("the cat and [point] the dog")
        words = [(0.0, "the"), (0.3, "cat"), (0.6, "and"), (0.9, "the"), (1.2, "dog")]
        self.assertEqual(gesture_cues.timed(marks, words, spoken), [{"name": "point", "time": 0.9}])

    def test_a_mark_at_the_very_end_uses_the_last_word(self):
        spoken, marks = gesture_cues.split("Any questions [ask]")
        self.assertEqual(gesture_cues.timed(marks, [(0.0, "Any"), (0.4, "questions")], spoken),
                         [{"name": "ask", "time": 0.4}])

    def test_without_word_times_there_are_no_cues(self):
        spoken, marks = gesture_cues.split(REPLY)
        self.assertEqual(gesture_cues.timed(marks, [], spoken), [])


class _Gestures:
    """Stands in for the gesture client: records what it is asked, moves nothing."""

    def __init__(self):
        self.starts = []

    def start(self, pcm, **kwargs):
        self.starts.append(kwargs)
        return False

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _SpeakingWords:
    """Edge TTS stand-in that also reports when each word starts."""
    asked = []

    def __init__(self, text, voice, **kwargs):
        _SpeakingWords.asked.append((text, kwargs))

    async def stream(self):
        for start, word in WORDS:
            yield {"type": "WordBoundary", "offset": int(start * 1e7), "duration": 1, "text": word}
        yield {"type": "audio", "data": b"mp3"}


class InTheConversation(unittest.TestCase):
    def setUp(self):
        _SpeakingWords.asked = []
        self.loop = base._new_loop()
        self.loop.tts_done.wait = lambda timeout=None: True
        self.loop.gestures = _Gestures()
        self._sleep = mock.patch.object(script.time, "sleep", lambda seconds: None)
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()

    def test_off_by_default(self):
        self.assertFalse(script.GESTURE_CUES)
        self.assertNotIn("[yes]", self.loop._build_system_prompt())
        self.loop._speak("Plain reply.")
        self.assertEqual(self.loop.gestures.starts, [{}])               # asked exactly as before
        self.assertEqual(base.tts_calls[-1][1], "Plain reply.")

    def test_when_on_claude_is_told_about_the_marks(self):
        with mock.patch.object(script, "GESTURE_CUES", True):
            prompt = self.loop._build_system_prompt()
        for name in ("yes", "point", "one_hand", "other_hand", "small", "big", "ask"):
            self.assertIn(f"[{name}]", prompt)
        self.assertIn("never spoken", prompt)

    def test_when_on_marks_are_not_spoken_and_cues_reach_the_gesture_client(self):
        with mock.patch.object(script, "GESTURE_CUES", True), \
                mock.patch.object(script.edge_tts, "Communicate", _SpeakingWords):
            self.loop._speak(REPLY)
        text, kwargs = _SpeakingWords.asked[-1]
        self.assertEqual(text, SPOKEN)                                   # no marks in the voice
        self.assertEqual(kwargs, {"boundary": "WordBoundary"})
        self.assertEqual(self.loop.gestures.starts,
                         [{"cues": [{"name": "yes", "time": 1.2}, {"name": "point", "time": 3.1}]}])

    def test_when_on_but_the_voice_gives_no_word_times_it_gestures_as_before(self):
        with mock.patch.object(script, "GESTURE_CUES", True):
            self.loop._speak(REPLY)                                      # stand-in voice: audio only
        self.assertEqual(base.tts_calls[-1][1], SPOKEN)                  # marks still not spoken
        self.assertEqual(self.loop.gestures.starts, [{}])                # plain speech gesture


if __name__ == "__main__":
    unittest.main()
