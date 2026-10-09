#!/usr/bin/env python3
"""
test_gesture_waist.py  -  offline check of what the gesture client sends to the waist

The waist is always held, with gains, where it was when the arms were taken.
Sending nothing to it ("free") was tried on the physical G1 on 2026-10-09 and
the torso bent far backward, so SB01_GESTURE_WAIST=free is refused: the waist
is held all the same and the startup output says so.

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
        self.assertNotIn("NOT used", printed)


class AskingForAFreeWaistIsRefused(_WaistCase):
    __test__ = True
    MODE = "free"

    def test_the_waist_is_held_all_the_same(self):
        self._held_throughout()

    def test_startup_says_the_setting_is_not_used_and_why(self):
        printed = self._startup()
        self.assertIn("SB01_GESTURE_WAIST=free is NOT used", printed)
        self.assertIn("bend far backward", printed)
        self.assertIn("waist: held in place", printed)


if __name__ == "__main__":
    unittest.main()
