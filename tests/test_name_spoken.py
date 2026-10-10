#!/usr/bin/env python3
"""
test_name_spoken.py  -  offline check of how the name is handed to the English Chatterbox voice

The English Chatterbox voice reads "Yotie" as "yah-dee". The text given to that
voice therefore spells the name "Yohdee", which it says as "yoh-dee". Only what
the voice reads changes: the name everywhere else stays "Yotie".

The voice is a stand-in here, so this shows what text it is given, not how it sounds.

  python3 -m unittest tests.test_name_spoken -v      (from the project root)
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests import test_voice as tv

script = tv.script


def _given_to_the_voice(text, language=None, gestures=True):
    loop, _ = tv._loop(gestures=gestures)
    tv._speak(loop, text, language)
    return [said for said, _ in loop.voice._synth.said]


class TheNameForTheVoice(unittest.TestCase):
    def test_the_english_voice_is_given_the_respelled_name(self):
        said = _given_to_the_voice("Hi! I am Yotie. Ask me anything.", "en")
        self.assertEqual(said, ["Hi! I am Yohdee. Ask me anything."])

    def test_only_the_name_itself_is_respelled(self):
        self.assertEqual(script.name_for_the_voice("Yotie's notes, said by yotie. Not Yotiesque."),
                         "Yohdee's notes, said by Yohdee. Not Yotiesque.")

    def test_the_written_name_is_unchanged(self):
        self.assertEqual(script.ROBOT_NAME, "Yotie")
        self.assertIn("You are Yotie,", script.BASE_SYSTEM_PROMPT)

    def test_other_languages_are_given_the_name_as_written(self):
        said = _given_to_the_voice("¡Hola! Soy Yotie. ¿Cómo estás?", "es")
        self.assertEqual(said, ["¡Hola! Soy Yotie. ¿Cómo estás?"])

    def test_the_spelling_can_be_set_or_switched_off(self):
        with mock.patch.object(script, "NAME_SPOKEN_AS", "Yoh-tee"):
            self.assertEqual(script.name_for_the_voice("I am Yotie."), "I am Yoh-tee.")
        with mock.patch.object(script, "NAME_SPOKEN_AS", ""):
            self.assertEqual(script.name_for_the_voice("I am Yotie."), "I am Yotie.")


if __name__ == "__main__":
    unittest.main()
