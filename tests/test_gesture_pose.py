#!/usr/bin/env python3
"""
test_gesture_pose.py  -  offline check of fixed poses ("thinking") in the gesture client

No robot, no DDS, no network, no sidecar and no Unitree SDK: the SDK is the
stand-in from tests/test_gesture_fsm.py, the sidecar's reply is canned, and the
"robot" simply reports the arm angles it was last sent.

  python3 -m unittest tests.test_gesture_pose -v      (from the project root)
"""

import io
import json
import os
import sys
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests import test_gesture_fsm as base

POSE = np.array([0.0] * 7 + [-0.19, -0.36, 0.39, -0.62, 0.0, -0.2, 0.0], dtype=np.float32)
MOVE_FRAMES, STAY_FRAMES = 30, 60


def _smooth(count):
    ratio = np.linspace(0.0, 1.0, count, dtype=np.float32)
    return ratio * ratio * (3.0 - 2.0 * ratio)


def _pose_stream(stay=STAY_FRAMES, sway=0.0):
    """What the sidecar sends for a pose: hold, the way there + stay, done.
    With `sway`, the shoulders rock by that many rad while staying, and the
    first line says how many frames are the way there."""
    rest = np.zeros(14, dtype=np.float32)
    there = rest + _smooth(MOVE_FRAMES)[:, None] * (POSE - rest)
    staying = np.repeat(POSE[None], stay, axis=0)
    first = {"hold": rest.tolist(), "fps": 30}
    if sway:
        rock = sway * np.sin(2.0 * np.pi * 0.4 * np.arange(stay) / 30.0)
        staying[:, 0] += rock          # left shoulder pitch
        staying[:, 7] -= rock          # right shoulder pitch, the other way
        first["path"] = MOVE_FRAMES
    arms = np.concatenate((there, staying))
    frames = np.column_stack((arms, np.ones(len(arms), dtype=np.float32)))
    lines = [first, {"frames": frames.tolist()}, {"done": True}]
    return io.BytesIO("".join(json.dumps(line) + "\n" for line in lines).encode())


class GesturePose(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("SB01_GESTURE_FSM_IDS")
        base._Robot.fsm, base._Robot.fsm_code = 802, 0
        base._Robot.published = []
        self.sent = []                       # every command: 14 arm angles + weight
        self.requests = []                   # (url, body) of every sidecar request
        self.arms = np.zeros(14)
        self.reply = _pose_stream
        self.client = None
        self.module = base._load_client()

        test = self

        def write(publisher, cmd):
            frame = [cmd.motor_cmd[joint].q for joint in range(15, 30)]
            test.sent.append(frame)
            test.arms = np.array(frame[:14])     # the stand-in robot follows at once
            return True

        def urlopen(request, timeout=None):
            test.requests.append((request.full_url, json.loads(request.data)))
            reply = test.reply()
            if isinstance(reply, Exception):
                raise reply
            return reply

        self._patches = [mock.patch.object(base._Publisher, "Write", write),
                         mock.patch.object(self.module.urllib.request, "urlopen", urlopen)]
        for patch in self._patches:
            patch.start()
        self._alive = True
        self._feeder = threading.Thread(target=self._feed, daemon=True)
        with redirect_stdout(io.StringIO()):
            self.client = self.module.GestureClient("http://127.0.0.1:9")
            self._feeder.start()
            time.sleep(0.05)                 # first rt/lowstate is in

    def _feed(self):
        """rt/lowstate, as the robot would send it: the arms are where they were told to be."""
        while self._alive:
            q = [0.0] * 15 + list(self.arms) + [0.0] * 6
            callback = base._Robot.lowstate_callback
            if callback is not None:
                callback(types.SimpleNamespace(motor_state=[types.SimpleNamespace(q=v) for v in q],
                                               mode_pr=0, mode_machine=5))
            time.sleep(0.005)

    def tearDown(self):
        with redirect_stdout(io.StringIO()):
            self.client.close()
        self._alive = False
        self._feeder.join()
        for patch in self._patches:
            patch.stop()
        if self._saved is None:
            os.environ.pop("SB01_GESTURE_FSM_IDS", None)
        else:
            os.environ["SB01_GESTURE_FSM_IDS"] = self._saved

    def _play(self, finish_after=None, seconds=2.0):
        out = io.StringIO()
        with redirect_stdout(out):
            started = time.monotonic()
            accepted = self.client.pose("thinking", seconds)
            if finish_after is not None:
                time.sleep(finish_after)
                self.client.finish_pose()
            self.client.wait()
        return accepted, time.monotonic() - started, out.getvalue()

    def _at_pose(self):
        return sum(1 for frame in self.sent if np.abs(np.array(frame[:14]) - POSE).max() < 1e-4)

    def _largest_step(self):
        return float(np.abs(np.diff(np.array(self.sent)[:, :14], axis=0)).max())

    # ── the request ──────────────────────────────────────────────────────────

    def test_pose_is_asked_for_by_name_without_audio(self):
        self._play(finish_after=0.0)
        url, body = self.requests[0]
        self.assertEqual(url, "http://127.0.0.1:9/pose")
        self.assertEqual((body["pose"], body["seconds"]), ("thinking", 2.0))
        self.assertNotIn("pcm", body)
        self.assertEqual((len(body["legs"]), len(body["waist"]), len(body["arms"])), (12, 3, 14))

    def test_speech_gestures_are_still_asked_for_the_same_way(self):
        self.reply = lambda: io.BytesIO(b'{"error": "stop here"}\n')
        with redirect_stdout(io.StringIO()):
            self.assertFalse(self.client.start(b"\0\0" * 2400))
        url, body = self.requests[0]
        self.assertEqual(url, "http://127.0.0.1:9/gesture")
        self.assertIn("pcm", body)
        self.assertNotIn("pose", body)
        self.assertEqual(self.sent, [])

    def test_teaching_cues_are_sent_with_the_speech_request(self):
        self.reply = lambda: io.BytesIO(b'{"error": "stop here"}\n')
        cues = [{"name": "yes", "time": 1.2}, {"name": "point", "time": 3.1}]
        with redirect_stdout(io.StringIO()):
            self.assertFalse(self.client.start(b"\0\0" * 2400, cues=cues))
            self.assertFalse(self.client.start(b"\0\0" * 2400, pose="thinking", cues=cues))
        self.assertEqual(self.requests[0][0], "http://127.0.0.1:9/gesture")
        self.assertEqual(self.requests[0][1]["cues"], cues)
        self.assertNotIn("cues", self.requests[1][1])                  # a fixed pose takes no cues
        self.assertEqual(self.sent, [])

    # ── playing it ───────────────────────────────────────────────────────────

    def test_pose_goes_there_stays_and_comes_back(self):
        accepted, _, printed = self._play()
        self.assertTrue(accepted)
        self.assertGreaterEqual(self._at_pose(), STAY_FRAMES)          # stayed the whole time
        self.assertLess(self.sent[0][14], 0.1)                         # arms are taken gently
        self.assertLess(self._assert_went_back_the_way_it_came(printed), 1e-4)

    def _assert_went_back_the_way_it_came(self, printed):
        self.assertEqual(printed, "")
        self.assertLessEqual(self._largest_step(), self.module.MAX_STEP_RAD + 1e-3)   # nothing jumped
        arms = np.round(np.array(self.sent)[:, :14], 5)
        turn = int(np.argmin(np.abs(arms - POSE).max(axis=1)))         # the frame closest to the pose
        played = {tuple(frame) for frame in arms[:turn + 1]}
        for frame in arms[turn:]:
            self.assertIn(tuple(frame), played)                        # only poses it had already been in
        self.assertEqual(self.sent[-1][14], 0.0)                       # fully released
        self.assertLess(np.abs(arms[-1]).max(), 1e-4)                  # where the arms were taken
        return float(np.abs(arms[turn] - POSE).max())

    def test_finish_on_the_way_there_turns_round_at_once(self):
        accepted, took, printed = self._play(finish_after=0.5)         # half-way through the move
        self.assertTrue(accepted)
        short_of_pose = self._assert_went_back_the_way_it_came(printed)
        self.assertGreater(short_of_pose, 0.1)                         # it did not carry on to the pose
        self.assertEqual(self._at_pose(), 0)
        self.assertLess(took, 0.5 + 0.5 + 0.7 + self.module.ABORT_RELEASE_SECONDS + 0.5)

    def test_finish_while_staying_goes_back_without_waiting(self):
        accepted, took, printed = self._play(finish_after=1.5)         # at the pose, 0.5 s into a 2 s stay
        self.assertTrue(accepted)
        short_of_pose = self._assert_went_back_the_way_it_came(printed)
        self.assertLess(short_of_pose, 1e-4)                           # it had arrived
        self.assertLess(self._at_pose(), STAY_FRAMES - 20)             # and did not wait out the stay

    def test_a_stream_that_breaks_at_the_pose_still_brings_the_arms_back(self):
        def cut_short():
            whole = _pose_stream().getvalue().splitlines(keepends=True)
            return io.BytesIO(b"".join(whole[:-1]))                    # no "done": the sidecar died
        self.reply = cut_short
        accepted, _, printed = self._play()
        self.assertTrue(accepted)
        self.assertIn("stream ended early", printed)
        self.assertLess(self._assert_went_back_the_way_it_came(""), 1e-4)

    def test_finish_after_the_pose_is_already_coming_back_changes_nothing(self):
        self.reply = lambda: _pose_stream(stay=5)
        with redirect_stdout(io.StringIO()):
            self.client.pose("thinking", 0.2)
            self.client.wait()
        natural, self.sent = np.array(self.sent), []
        accepted, _, printed = self._play(finish_after=1.0 + 5 / 30 + 0.5)   # half-way back
        self.assertTrue(accepted)
        self.assertEqual(printed, "")
        self.assertEqual(len(self.sent), len(natural))
        self.assertLess(np.abs(np.array(self.sent) - natural).max(), 1e-6)

    def test_finishing_does_not_carry_over_to_the_next_gesture(self):
        self._play(finish_after=0.0)
        self.assertTrue(self.client._wrap.is_set())
        self.reply = lambda: _pose_stream(stay=5)
        with redirect_stdout(io.StringIO()):
            self.client.pose("thinking", 0.2)
            self.assertFalse(self.client._wrap.is_set())
            self.client.wait()

    # ── swaying at the pose ──────────────────────────────────────────────────

    def _assert_came_back_from_a_sway(self, printed, sway):
        """After swaying, the arms step back onto the path (a few frames), then
        go back the way they came. They do not replay the sway backwards."""
        self.assertEqual(printed, "")
        self.assertLessEqual(self._largest_step(), self.module.MAX_STEP_RAD + 1e-3)   # nothing jumped
        sent = np.array(self.sent)
        arms = np.round(sent[:, :14], 5)
        at_pose = np.flatnonzero(np.abs(arms - POSE).max(axis=1) < 1e-4)
        self.assertGreater(len(at_pose), 0)                                # it passed through the pose
        on_the_way = {tuple(frame) for frame in arms[:at_pose[0] + 1]}
        back = arms[at_pose[-1]:]                                          # from the pose home
        for frame in back:
            self.assertIn(tuple(frame), on_the_way)
        self.assertLessEqual(len(back), MOVE_FRAMES + 2 + round(self.module.ABORT_RELEASE_SECONDS * 30))
        swayed = np.abs(arms[at_pose[0]:at_pose[-1], [0, 7]] - POSE[[0, 7]]).max()
        self.assertGreater(swayed, 0.5 * sway)                             # the sway was really played
        self.assertEqual(sent[-1, 14], 0.0)
        self.assertLess(np.abs(arms[-1]).max(), 1e-4)

    def test_sway_at_the_pose_is_played_and_the_arms_still_come_home(self):
        self.reply = lambda: _pose_stream(stay=70, sway=0.06)              # ends mid-sway, off the pose
        accepted, _, printed = self._play()
        self.assertTrue(accepted)
        self._assert_came_back_from_a_sway(printed, 0.06)

    def test_finish_during_a_sway_steps_back_onto_the_path(self):
        self.reply = lambda: _pose_stream(stay=200, sway=0.06)
        accepted, took, printed = self._play(finish_after=1.0 + 0.6)        # 0.6 s into the sway
        self.assertTrue(accepted)
        self._assert_came_back_from_a_sway(printed, 0.06)
        self.assertLess(took, 0.5 + 1.6 + 0.2 + 1.0 + self.module.ABORT_RELEASE_SECONDS + 0.6)   # did not wait it out

    def test_a_bad_path_count_is_ignored(self):
        def odd():
            whole = _pose_stream(stay=5).getvalue().splitlines(keepends=True)
            first = json.loads(whole[0])
            first["path"] = "many"
            return io.BytesIO(json.dumps(first).encode() + b"\n" + b"".join(whole[1:]))
        self.reply = odd
        accepted, _, printed = self._play()
        self.assertTrue(accepted)
        self.assertIsNone(self.client._path_frames)
        self.assertLess(self._assert_went_back_the_way_it_came(printed), 1e-4)

    # ── refusals ─────────────────────────────────────────────────────────────

    def test_pose_is_refused_outside_the_supported_robot_state(self):
        base._Robot.fsm = 501
        accepted, _, printed = self._play()
        self.assertFalse(accepted)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.requests, [])                            # the sidecar is not even asked
        self.assertIn("FSM=501", printed)

    def test_pose_the_sidecar_does_not_know_moves_nothing(self):
        self.reply = lambda: OSError("HTTP Error 400: Bad Request")
        accepted, _, printed = self._play()
        self.assertFalse(accepted)
        self.assertEqual(self.sent, [])
        self.assertIn("not gesturing", printed)

    def test_pose_that_jumps_is_rejected_like_any_other_block(self):
        def jumping():
            frames = np.column_stack((np.repeat(POSE[None], 10, axis=0), np.ones(10)))   # no path to the pose
            lines = [{"hold": [0.0] * 14, "fps": 30}, {"frames": frames.tolist()}, {"done": True}]
            return io.BytesIO("".join(json.dumps(line) + "\n" for line in lines).encode())

        self.reply = jumping
        accepted, _, printed = self._play()
        self.assertFalse(accepted)
        self.assertIn("stream rejected", printed)
        self.assertLess(np.abs(np.array(self.sent)[:, :14]).max(), 1e-6)    # arms never left rest
        self.assertEqual(self.sent[-1][14], 0.0)


if __name__ == "__main__":
    unittest.main()
