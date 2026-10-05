#!/usr/bin/env python3
"""
test_name.py  -  offline check that the robot is, and calls itself, Yotie

Runs the real scripts/sb01_conversation.py with the same stand-ins as
tests/test_language.py: no robot, no Claude, no TTS, no network.

Claude is a stand-in here, so these tests cover what the script guarantees on
its own: what Claude is told, how a mis-transcribed name is flagged, and that a
reply never goes out with another name for the robot. They do not show how the
real Claude phrases an answer.

  python3 -m unittest tests.test_name -v      (from the project root)
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests import test_language as base     # loads the real script once, with stand-ins

script = base.script
NAME = "Yotie"
NAME_NOTE = "how your name, Yotie, was transcribed"

# What people say -> what a careless model might answer (wrong name on purpose).
CASES = [
    ("What is your name?",            "My name is sb01."),
    ("Hey Yodee",                     "Hey! Yodee here, what's up?"),
    ("Are you You-Tee?",              "Yes, I am You-Tee!"),
    ("Tell me about yourself, Yoti.", "I'm Yoti, a humanoid robot at Cal State San Bernardino."),
]
OTHER_NAMES = re.compile(r"(?<![A-Za-z0-9])(yodee|yodie|you-?tee|yo-?tee|yoti|yotee|sb-?01)(?![A-Za-z0-9])", re.I)


def _converse(loop, said, model_answer):
    """One phrase through the real _ask_claude. Returns (what Claude was sent, the reply to be spoken)."""
    base.reply_text["value"] = model_answer
    reply = loop._ask_claude(said, "")[0]
    return base.claude_calls[-1]["messages"][-1]["content"], reply


class RobotIsCalledYotie(unittest.TestCase):
    def tearDown(self):
        base.reply_text["value"] = "ok"

    # ── what the script itself uses ──────────────────────────────────────────

    def test_robot_name_setting(self):
        self.assertEqual(script.ROBOT_NAME, NAME)

    def test_system_prompt_introduces_yotie_and_explains_the_variants(self):
        prompt = script.BASE_SYSTEM_PROMPT
        self.assertTrue(prompt.startswith("You are Yotie,"))
        self.assertIn("Your name is Yotie.", prompt)
        for variant in ("Yodee", "You-Tee", "Yoti"):
            self.assertIn(variant, prompt)
        self.assertIn("they mean you", prompt)
        self.assertIn("Always call yourself Yotie", prompt)
        self.assertIn("never use any other name for yourself", prompt)
        self.assertIn("Do not point out or correct how someone says or spells your name", prompt)
        self.assertNotIn("sb01", prompt.lower())

    def test_greetings_and_terminal_labels_use_the_name_setting(self):
        with open(base.SCRIPT, encoding="utf-8") as handle:
            source = handle.read()
        # The five greetings for someone the robot does not know are its
        # self-introductions (greetings for a recognized person use their name).
        block = source[source.index("            greeting = random.choice([", source.index("        else:\n            greeting")):]
        introductions = re.findall(r'f"([^"]*)"', block[:block.index("])")])
        self.assertEqual(len(introductions), 5)
        for greeting in introductions:
            self.assertIn("{ROBOT_NAME}", greeting)
            self.assertIn(NAME, greeting.replace("{ROBOT_NAME}", script.ROBOT_NAME))
        self.assertNotIn("[sb01]", source)                      # labels are printed as [{ROBOT_NAME}]
        self.assertGreater(source.count("[{ROBOT_NAME}]"), 10)

    def test_technical_identifiers_keep_sb01(self):
        with open(base.SCRIPT, encoding="utf-8") as handle:
            source = handle.read()
        self.assertTrue(base.SCRIPT.endswith("sb01_conversation.py"))
        self.assertIn('PlayStream("sb01"', source)             # audio stream label, not speech

    # ── the four phrases ─────────────────────────────────────────────────────

    def test_each_phrase_reaches_claude_and_the_reply_says_yotie(self):
        for said, model_answer in CASES:
            with self.subTest(said=said):
                loop = base._new_loop()
                sent, reply = _converse(loop, said, model_answer)
                self.assertIn(said, sent)                                   # not dropped, not rewritten
                self.assertIn("Your name is Yotie.", base.claude_calls[-1]["system"])
                self.assertIn(NAME, reply)                                  # identifies itself as Yotie
                self.assertIsNone(OTHER_NAMES.search(reply), reply)         # and as nothing else
                self.assertEqual(loop.history[-1], {"role": "assistant", "content": reply})

    def test_variants_are_flagged_as_the_robots_name(self):
        for said in ("Hey Yodee", "Are you You-Tee?", "Tell me about yourself, Yoti."):
            with self.subTest(said=said):
                self.assertTrue(script.heard_own_name(said))
                sent, _ = _converse(base._new_loop(), said, "ok")
                self.assertIn(NAME_NOTE, sent)
                self.assertIn("the person is talking to you", sent)

    def test_what_is_your_name_needs_no_flag(self):
        self.assertFalse(script.heard_own_name("What is your name?"))
        sent, reply = _converse(base._new_loop(), "What is your name?", "I'm Yotie!")
        self.assertNotIn(NAME_NOTE, sent)
        self.assertEqual(reply, "I'm Yotie!")

    def test_the_correct_name_is_not_flagged_as_a_mistake(self):
        self.assertFalse(script.heard_own_name("Hi Yotie, how are you?"))

    # ── variants and look-alikes ─────────────────────────────────────────────

    def test_other_likely_variants(self):
        for said in ("yodie", "Yody", "Yotee", "Yottie", "yo tee", "Yo-Tee", "you tee", "YouTee", "YODEE!", "嗨Yoti"):
            self.assertTrue(script.heard_own_name(said), said)

    def test_ordinary_words_are_not_mistaken_for_the_name(self):
        for said in ("Can you tie my shoe", "I like to yodel", "Do you know Yoda", "you teach me math",
                     "youtube is fun", "my friend Jodie", "what is your name", "yo, what's up"):
            self.assertFalse(script.heard_own_name(said), said)

    def test_replies_are_spoken_with_the_right_name_in_every_language(self):
        self.assertEqual(script.say_own_name("Soy Yodie, mucho gusto."), "Soy Yotie, mucho gusto.")
        self.assertEqual(script.say_own_name("我是Yoti。"), "我是Yotie。")
        self.assertEqual(script.say_own_name("I am SB-01."), "I am Yotie.")
        self.assertEqual(script.say_own_name("I'm Yotie!"), "I'm Yotie!")

    def test_replies_are_otherwise_left_alone(self):
        for reply in ("I think you tee off first in golf.", "You tie the knot like this.",
                      "Cal State San Bernardino has about 17,900 students.", "Yoda is from Star Wars."):
            self.assertEqual(script.say_own_name(reply), reply)

    def test_the_name_handling_does_not_disturb_the_language_note(self):
        loop = base._new_loop()
        sent, _ = _converse(loop, "Hola Yodee, como estas hoy", "Hola, soy Yodee")
        self.assertTrue(sent.startswith("[Reply in Spanish.]"))
        self.assertIn(NAME_NOTE, sent)
        self.assertEqual(loop.history[-1]["content"], "Hola, soy Yotie")


if __name__ == "__main__":
    unittest.main()
