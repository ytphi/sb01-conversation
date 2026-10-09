#!/usr/bin/env python3
"""
test_gesture_waist.py  -  offline check of what the gesture client sends to the waist

By default the waist is held, with gains, where it was when the arms were taken.
SB01_GESTURE_WAIST=free is kept for experiments: nothing is sent to the waist
(zero gains). On the physical G1 that let the torso lean back (2026-10-09), so
the startup output carries a warning when it is set.

No robot, no DDS, no sidecar: the same stand-ins as tests/test_gesture_pose.py.
This shows what is commanded, not how the real robot balances.

  python3 -m unittest tests.test_gesture_waist -v      (from the project root)
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests import test_gesture_fsm as base
from tests import test_gesture_pose as pose

WAIST = (12, 13, 14)


class _WaistCase(pose.GesturePose):
    """A pose played once, recording the gains and targets sent to the waist."""
    MODE = None
    __test__ = False                        # only the subclasses below are run

    def setUp(self):
        self._waist_setting = os.environ.pop("SB01_GESTURE_WAIST", None)
        if self.MODE is not None:
            os.environ["SB01_GESTURE_WAIST"] = self.MODE
        super().setUp()
        self.waist = []
        recorded = base._Publisher.Write       # the write that test_gesture_pose installed
        test = self

        def write(publisher, cmd):
            test.waist.append([(cmd.motor_cmd[j].q, cmd.motor_cmd[j].kp, cmd.motor_cmd[j].kd) for j in WAIST])
            return recorded(publisher, cmd)

        base._Publisher.Write = write
        self._recorded = recorded

    def tearDown(self):
        base._Publisher.Write = self._recorded
        super().tearDown()
        os.environ.pop("SB01_GESTURE_WAIST", None)
        if self._waist_setting is not None:
            os.environ["SB01_GESTURE_WAIST"] = self._waist_setting

    def _held_throughout(self):
        accepted, _, printed = self._play(seconds=0.5)
        self.assertTrue(accepted, printed)
        self.assertGreater(len(self.waist), 30)
        gains = {(kp, kd) for frame in self.waist for _, kp, kd in frame}
        self.assertEqual(gains, {(self.module.WAIST_KP, self.module.WAIST_KD)})            # never zero gains
        self.assertEqual(len({tuple(q for q, _, _ in frame) for frame in self.waist}), 1)   # and it never moves

    def _startup(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.module.GestureClient("http://127.0.0.1:9").close()
        return out.getvalue()


# the inherited pose tests are not repeated here
for _name in [n for n in dir(pose.GesturePose) if n.startswith("test_")]:
    setattr(_WaistCase, _name, None)


class WaistIsHeld(_WaistCase):
    __test__ = True
    MODE = None

    def test_the_waist_is_held_with_gains_at_one_position(self):
        self._held_throughout()

    def test_startup_says_the_waist_is_held(self):
        printed = self._startup()
        self.assertIn("waist: held in place", printed)
        self.assertNotIn("WARNING", printed)


class FreeWaistForExperiments(_WaistCase):
    __test__ = True
    MODE = "free"

    def test_nothing_is_sent_to_the_waist(self):
        accepted, _, printed = self._play(seconds=0.5)
        self.assertTrue(accepted, printed)
        self.assertGreater(len(self.waist), 30)
        for frame in self.waist:
            self.assertEqual(frame, [(0.0, 0.0, 0.0)] * 3)

    def test_startup_warns_what_it_did_on_the_robot(self):
        printed = self._startup()
        self.assertIn("WARNING: SB01_GESTURE_WAIST=free", printed)
        self.assertIn("lean back", printed)
        self.assertNotIn("waist: held in place", printed)


class WaistSetting(unittest.TestCase):
    def test_holding_is_the_default(self):
        saved = os.environ.pop("SB01_GESTURE_WAIST", None)
        try:
            self.assertTrue(base._load_client().WAIST_HOLD)
        finally:
            if saved is not None:
                os.environ["SB01_GESTURE_WAIST"] = saved

    def test_an_unknown_value_is_refused(self):
        saved = os.environ.get("SB01_GESTURE_WAIST")
        os.environ["SB01_GESTURE_WAIST"] = "wobbly"
        try:
            with self.assertRaises(ValueError) as refused:
                base._load_client()
            self.assertIn("SB01_GESTURE_WAIST", str(refused.exception))
        finally:
            os.environ.pop("SB01_GESTURE_WAIST", None)
            if saved is not None:
                os.environ["SB01_GESTURE_WAIST"] = saved


if __name__ == "__main__":
    unittest.main()
