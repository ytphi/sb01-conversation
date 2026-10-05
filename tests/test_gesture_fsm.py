#!/usr/bin/env python3
"""
test_gesture_fsm.py  -  offline check of the gesture client's robot-state gate

No robot, no DDS, no network and no Unitree SDK needed: the SDK modules are
replaced by stand-ins that only record what would have been published.

  python3 -m unittest tests.test_gesture_fsm -v      (from the project root)
"""

import importlib
import io
import os
import sys
import time
import types
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _Motor:
    def __init__(self):
        self.q = self.dq = self.tau = self.kp = self.kd = 0.0


class _LowCmd:
    def __init__(self):
        self.mode_pr = self.mode_machine = self.crc = 0
        self.motor_cmd = [_Motor() for _ in range(35)]


class _Robot:
    """What the stand-in SDK reports and records."""
    fsm, fsm_code = 802, 0
    published = []
    lowstate_callback = None


class _Publisher:
    def __init__(self, *args):
        pass

    def Init(self):
        pass

    def Close(self):
        pass

    def Write(self, cmd):
        _Robot.published.append(cmd.motor_cmd[29].q)
        return True


class _Subscriber:
    def __init__(self, *args):
        pass

    def Init(self, callback, depth):
        _Robot.lowstate_callback = callback

    def Close(self):
        _Robot.lowstate_callback = None


class _LocoClient:
    def SetTimeout(self, seconds):
        pass

    def Init(self):
        pass

    def GetFsmId(self):
        return _Robot.fsm_code, (_Robot.fsm if _Robot.fsm_code == 0 else None)


class _CRC:
    def Crc(self, cmd):
        return 1


def _install_stand_in_sdk():
    def module(name, **attributes):
        mod = types.ModuleType(name)
        mod.__dict__.update(attributes)
        sys.modules[name] = mod

    for package in ("unitree_sdk2py", "unitree_sdk2py.core", "unitree_sdk2py.g1", "unitree_sdk2py.g1.loco",
                    "unitree_sdk2py.idl", "unitree_sdk2py.idl.unitree_hg", "unitree_sdk2py.idl.unitree_hg.msg",
                    "unitree_sdk2py.utils"):
        module(package, __path__=[])
    module("unitree_sdk2py.core.channel", ChannelPublisher=_Publisher, ChannelSubscriber=_Subscriber)
    module("unitree_sdk2py.g1.loco.g1_loco_client", LocoClient=_LocoClient)
    module("unitree_sdk2py.idl.default", unitree_hg_msg_dds__LowCmd_=_LowCmd)
    module("unitree_sdk2py.idl.unitree_hg.msg.dds_", LowCmd_=_LowCmd, LowState_=object)
    module("unitree_sdk2py.utils.crc", CRC=_CRC)


def _load_client(fsm_ids=None):
    """Import teleop.gesture_client afresh with SB01_GESTURE_FSM_IDS set or unset."""
    _install_stand_in_sdk()
    if fsm_ids is None:
        os.environ.pop("SB01_GESTURE_FSM_IDS", None)
    else:
        os.environ["SB01_GESTURE_FSM_IDS"] = fsm_ids
    sys.modules.pop("teleop.gesture_client", None)
    return importlib.import_module("teleop.gesture_client")


def _lowstate(mode_machine=5):
    return types.SimpleNamespace(motor_state=[types.SimpleNamespace(q=0.0) for _ in range(35)],
                                 mode_pr=0, mode_machine=mode_machine)


class GestureFsmGate(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("SB01_GESTURE_FSM_IDS")
        _Robot.fsm, _Robot.fsm_code = 802, 0
        _Robot.published = []
        self.client = None

    def tearDown(self):
        if self.client is not None:
            with redirect_stdout(io.StringIO()):
                self.client.close()
        if self._saved is None:
            os.environ.pop("SB01_GESTURE_FSM_IDS", None)
        else:
            os.environ["SB01_GESTURE_FSM_IDS"] = self._saved

    def _client(self, fsm_ids=None):
        module = _load_client(fsm_ids)
        with redirect_stdout(io.StringIO()):
            self.client = module.GestureClient("http://127.0.0.1:9")   # no sidecar is ever contacted
            _Robot.lowstate_callback(_lowstate())
        return module, self.client

    # ── the default ──────────────────────────────────────────────────────────

    def test_default_is_802_only(self):
        module, _ = self._client()
        self.assertEqual(module.DEFAULT_FSM_IDS, "802")
        self.assertEqual(module.FSM_ALLOWED, (802,))

    def test_fsm_802_is_accepted(self):
        module, client = self._client()
        _Robot.fsm = 802
        q = client._require_supported_state()          # returns the joint angles when the gate passes
        self.assertEqual(len(q), 29)

    def test_other_states_are_rejected_and_named(self):
        module, client = self._client()
        for fsm in (501, 0, 1, 4, 500, 801):
            _Robot.fsm = fsm
            with self.assertRaises(module.GestureFault) as raised:
                client._require_supported_state()
            message = str(raised.exception)
            self.assertIn(f"FSM={fsm}", message)
            self.assertIn("mode_pr=0", message)
            self.assertIn("mode_machine=5", message)
            self.assertIn("(802,)", message)

    def test_unreadable_fsm_is_rejected(self):
        module, client = self._client()
        _Robot.fsm_code = 3104
        with self.assertRaises(module.GestureFault) as raised:
            client._require_supported_state()
        self.assertIn("FSM=unreadable", str(raised.exception))

    def test_start_refuses_and_publishes_nothing_in_an_unsupported_state(self):
        module, client = self._client()
        _Robot.fsm = 501
        out = io.StringIO()
        with redirect_stdout(out):
            started = client.start(b"\0\0" * 2400)
        self.assertFalse(started)
        self.assertEqual(_Robot.published, [])
        self.assertIn("not gesturing", out.getvalue())
        self.assertIn("FSM=501", out.getvalue())

    # ── the override ─────────────────────────────────────────────────────────

    def test_environment_variable_replaces_the_default(self):
        module, client = self._client("501")
        self.assertEqual(module.FSM_ALLOWED, (501,))
        _Robot.fsm = 501
        client._require_supported_state()
        _Robot.fsm = 802
        with self.assertRaises(module.GestureFault):
            client._require_supported_state()

    def test_environment_variable_accepts_a_list(self):
        module, client = self._client(" 802, 501 ")
        self.assertEqual(module.FSM_ALLOWED, (802, 501))
        for fsm in (802, 501):
            _Robot.fsm = fsm
            client._require_supported_state()
        _Robot.fsm = 4
        with self.assertRaises(module.GestureFault):
            client._require_supported_state()

    # ── the other gates still apply in FSM 802 ───────────────────────────────

    def test_stale_lowstate_is_rejected_even_in_802(self):
        module, client = self._client()
        q, mode_pr, mode_machine, stamp = client._state
        client._state = (q, mode_pr, mode_machine, stamp - 10 * module.STATE_MAX_AGE)
        with self.assertRaises(module.GestureFault) as raised:
            client._require_supported_state()
        self.assertIn("stale", str(raised.exception))

    def test_missing_lowstate_is_rejected_even_in_802(self):
        module, client = self._client()
        client._state = None
        with self.assertRaises(module.GestureFault):
            client._require_supported_state()

    def test_changed_mode_machine_is_rejected_even_in_802(self):
        module, client = self._client()
        with redirect_stdout(io.StringIO()):
            _Robot.lowstate_callback(_lowstate(mode_machine=4))
        time.sleep(0.01)
        with self.assertRaises(module.GestureFault) as raised:
            client._require_supported_state()
        self.assertIn("mode_machine changed", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
