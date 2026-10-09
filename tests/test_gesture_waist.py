#!/usr/bin/env python3
"""
test_gesture_waist.py  -  offline check of what the gesture client sends to the waist

By default the waist is held where it was when the arms were taken. With
SB01_GESTURE_WAIST=free nothing is sent to the waist at all (zero gains, as in
RoboGesture's own robot code), so the robot's balance controller keeps it.

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
ARMS = range(15, 29)


class _WaistCase(pose.GesturePose):
    """A pose played once, recording the gains and targets sent to the waist and arms."""
    MODE = None
    __test__ = False                        # only the two subclasses below are run

    def setUp(self):
        self._waist_setting = os.environ.pop("SB01_GESTURE_WAIST", None)
        if self.MODE is not None:
            os.environ["SB01_GESTURE_WAIST"] = self.MODE
        super().setUp()
        self.waist, self.arm_gains = [], []
        recorded = base._Publisher.Write       # the write that test_gesture_pose installed
        test = self

        def write(publisher, cmd):
            test.waist.append([(cmd.motor_cmd[j].q, cmd.motor_cmd[j].kp, cmd.motor_cmd[j].kd) for j in WAIST])
            test.arm_gains.append({(cmd.motor_cmd[j].kp, cmd.motor_cmd[j].kd) for j in ARMS})
            return recorded(publisher, cmd)

        base._Publisher.Write = write
        self._recorded = recorded

    def tearDown(self):
        base._Publisher.Write = self._recorded
        super().tearDown()
        os.environ.pop("SB01_GESTURE_WAIST", None)
        if self._waist_setting is not None:
            os.environ["SB01_GESTURE_WAIST"] = self._waist_setting

    def _one_pose(self):
        accepted, _, printed = self._play(seconds=0.5)
        self.assertTrue(accepted, printed)
        self.assertGreater(len(self.waist), 30)
        return printed


# the inherited pose tests are not repeated here
for _name in [n for n in dir(pose.GesturePose) if n.startswith("test_")]:
    setattr(_WaistCase, _name, None)


class WaistHeldByDefault(_WaistCase):
    __test__ = True
    MODE = None

    def test_the_waist_is_held_with_gains_at_one_position(self):
        self._one_pose()
        gains = {(kp, kd) for frame in self.waist for _, kp, kd in frame}
        self.assertEqual(gains, {(self.module.WAIST_KP, self.module.WAIST_KD)})
        self.assertEqual(len({tuple(q for q, _, _ in frame) for frame in self.waist}), 1)   # it never moves

    def test_startup_says_the_waist_is_held(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.module.GestureClient("http://127.0.0.1:9").close()
        self.assertIn("waist: held in place", out.getvalue())


class WaistLeftToTheRobot(_WaistCase):
    __test__ = True
    MODE = "free"

    def test_nothing_is_sent_to_the_waist(self):
        self._one_pose()
        for frame in self.waist:
            self.assertEqual(frame, [(0.0, 0.0, 0.0)] * 3)                  # no target, no stiffness, no damping

    def test_the_arms_are_commanded_exactly_as_before(self):
        self._one_pose()
        self.assertEqual(set().union(*self.arm_gains), {(self.module.ARM_KP, self.module.ARM_KD)})
        self.assertEqual(self.sent[-1][14], 0.0)                            # and released at the end
        self.assertLessEqual(self._largest_step(), self.module.MAX_STEP_RAD + 1e-6)

    def test_startup_says_only_the_arms_are_commanded(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.module.GestureClient("http://127.0.0.1:9").close()
        self.assertIn("only the arms are commanded", out.getvalue())


class WaistSetting(unittest.TestCase):
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
