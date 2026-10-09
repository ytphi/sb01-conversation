#!/usr/bin/env python3
"""
test_class_material.py  -  offline check of the course material Yotie is given

The files in reference/class are read at startup and sent to Claude with every
question. These tests cover the loading (order, the per-file limit, a missing
folder) and what is in the repository's own files: nothing that should not be
in a public repository, and a size the prompt can carry.

Claude is not called, so they do not show how well a question gets answered.

  python3 -m unittest tests.test_class_material -v      (from the project root)
"""

import contextlib
import io
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests import test_language as base     # loads the real script once, with stand-ins

script = base.script
CLASS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "reference", "class"))


def _load(folder):
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        parts = script.load_class_material(folder)
    return parts, printed.getvalue()


class LoadingCourseMaterial(unittest.TestCase):
    def test_every_text_file_is_loaded_in_name_order_after_the_note(self):
        with tempfile.TemporaryDirectory() as folder:
            for name, text in (("week2.md", "second"), ("week1.md", "first"), ("notes.txt", "third"), ("slides.pdf", "ignored")):
                with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                    f.write(text)
            parts, printed = _load(folder)
        self.assertIn("Never guess a date, a grade or a policy.", parts[0])
        self.assertEqual([p.splitlines()[-1] for p in parts[1:]], ["third", "first", "second"])
        self.assertNotIn("ignored", "".join(parts))
        self.assertEqual(printed.count("course material:"), 3)

    def test_a_long_file_is_cut_at_the_limit_and_the_startup_line_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "long.md"), "w", encoding="utf-8") as f:
                f.write("x" * (script.CLASS_FILE_MAX_CHARS + 250))
            parts, printed = _load(folder)
        self.assertEqual(parts[1].count("x"), script.CLASS_FILE_MAX_CHARS)
        self.assertIn("the last 250 left out", printed)

    def test_accented_and_chinese_text_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "a.md"), "w", encoding="utf-8") as f:
                f.write("señal y 机器人")
            parts, _ = _load(folder)
        self.assertIn("señal y 机器人", parts[1])

    def test_no_folder_or_an_empty_one_means_no_course_material(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(_load(folder)[0], [])
            self.assertEqual(_load(os.path.join(folder, "missing"))[0], [])

    def test_the_material_reaches_what_claude_is_given(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "week1.md"), "w", encoding="utf-8") as f:
                f.write("Lectures are on Monday and Wednesday.")
            saved, script.CLASS_DIR = script.CLASS_DIR, folder
            saved_urls, saved_files = script.PRELOAD_URLS, script.PRELOAD_FILES
            script.PRELOAD_URLS, script.PRELOAD_FILES = [], []
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    context = script.load_context()
            finally:
                script.CLASS_DIR, script.PRELOAD_URLS, script.PRELOAD_FILES = saved, saved_urls, saved_files
        self.assertIn("Lectures are on Monday and Wednesday.", context)
        self.assertIn("Course material for the class you assist with", context)


class TheCourseMaterialInTheRepository(unittest.TestCase):
    def setUp(self):
        self.files = {}
        for name in sorted(os.listdir(CLASS_DIR)):
            if name.endswith((".md", ".txt")):
                with open(os.path.join(CLASS_DIR, name), encoding="utf-8") as f:
                    self.files[name] = f.read()

    def test_the_syllabus_and_six_weeks_are_there(self):
        self.assertIn("00_syllabus.md", self.files)
        self.assertEqual(sum(name.startswith("week") for name in self.files), 6)

    def test_every_file_fits_under_the_limit_so_nothing_is_cut(self):
        for name, text in self.files.items():
            with self.subTest(name=name):
                self.assertLessEqual(len(text.strip()), script.CLASS_FILE_MAX_CHARS)

    def test_the_whole_set_stays_a_size_the_prompt_can_carry(self):
        self.assertLess(sum(len(text) for text in self.files.values()), 45000)

    def test_no_email_addresses_or_phone_numbers(self):
        for name, text in self.files.items():
            with self.subTest(name=name):
                self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", text))
                self.assertIsNone(re.search(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b", text))

    def test_teams_are_named_by_role_not_by_student(self):
        text = "\n".join(self.files.values())
        self.assertNotRegex(text, r"(?i)\b(researchers|officers|engineers)\s*:")   # the slides' rosters start this way
        self.assertNotRegex(text, r"\(lead\)")


if __name__ == "__main__":
    unittest.main()
