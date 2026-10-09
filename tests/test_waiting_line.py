#!/usr/bin/env python3
"""
test_waiting_line.py  -  offline check of which prepared line is said while a reply is made

With SB01_FILLER=1 and the Chatterbox voice, a short line is said as soon as a
phrase is heard. These tests cover the choice only: none before a short phrase,
a question line after a question, and the language the reply will be in,
including when Whisper's language detection is what decides it.

No robot, no voice model, no Claude: the lines are stand-ins.

  python3 -m unittest tests.test_waiting_line -v      (from the project root)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests import test_language as base     # loads the real script once, with stand-ins

script = base.script


def _loop(language="en"):
    loop = base._new_loop()
    loop.voice = object()                    # stands for a loaded Chatterbox voice
    loop.language = language
    loop._fillers = {code: {kind: [f"{code}/{kind}/{n}" for n in range(len(lines))] for kind, lines in kinds.items()}
                     for code, kinds in script.FILLERS.items()}
    return loop


class WhichLineIsSaid(unittest.TestCase):
    def test_nothing_before_a_short_phrase(self):
        loop = _loop()
        self.assertIsNone(loop._pick_filler("Hi"))
        self.assertIsNone(loop._pick_filler("thank you Yotie"))

    def test_a_question_gets_a_question_line_and_a_statement_the_other_kind(self):
        loop = _loop()
        self.assertTrue(loop._pick_filler("Can you tell me what DDS is").startswith("en/question/"))
        self.assertTrue(loop._pick_filler("Tell me about the robot please").startswith("en/other/"))

    def test_lines_take_turns(self):
        loop = _loop()
        first = loop._pick_filler("What is the grading for this class")
        second = loop._pick_filler("What is the grading for this class")
        self.assertNotEqual(first, second)

    def test_the_line_is_in_the_language_the_reply_will_switch_to(self):
        loop = _loop("en")
        self.assertTrue(loop._pick_filler("¿Puedes explicarme cómo funciona este circuito?").startswith("es/question/"))
        self.assertEqual(loop.language, "en")          # choosing the line does not itself switch the conversation

    def test_whisper_decides_the_language_when_it_is_sure(self):
        loop = _loop("en")
        said = "puedes explicarme como funciona el circuito"
        self.assertTrue(loop._pick_filler(said, ("es", 0.95)).startswith("es/question/"))
        self.assertTrue(loop._pick_filler(said, ("es", 0.30)).startswith("en/question/"))   # not sure: stays in English

    def test_no_line_in_a_language_that_has_none_prepared(self):
        loop = _loop("en")
        self.assertIsNone(loop._pick_filler("pouvez-vous m'expliquer le circuit s'il vous plaît", ("fr", 0.97)))

    def test_no_line_without_the_chatterbox_voice(self):
        loop = _loop()
        loop.voice = None
        self.assertIsNone(loop._pick_filler("Can you tell me what DDS is"))


if __name__ == "__main__":
    unittest.main()
