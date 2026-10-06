#!/usr/bin/env python3
"""
test_monitor.py  -  offline check of monitoring and the three reports

Runs the real scripts/sb01_conversation.py with the real teleop/monitor.py and
teleop/monitor_reports.py. Everything outside them is a stand-in (same ones as
tests/test_language.py): no robot, no DDS, no Claude, no Edge TTS, no camera,
no network. Monitoring data goes to a throw-away temporary folder.

  python3 -m unittest tests.test_monitor -v      (from the project root)
"""

import contextlib
import csv
import io
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests import test_language as base     # loads the real script once, with stand-ins
from teleop import monitor, monitor_reports

script = base.script
ROOT = base.ROOT
FAKE_KEY = "sk-ant-" + "test-0123456789abcdef"     # made up; not a real key
NAME = "Maria"                               # an enrolled person

SAID = [
    "Hola, ¿cómo estás?",
    f"My name is {NAME} and my password is hunter2",
    f"use api_key={FAKE_KEY} and Authorization: Bearer abcdef0123456789 please",
    "x" * 900,
]
REPLIES = [f"¡Hola {NAME}! Estoy bien.", f"Nice to meet you, {NAME}.", "I will not repeat that.", "That was long."]


class _Audio:
    """Stand-in robot audio service: every call is counted and answers 0 (OK)."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append(name)
            return 0
        return call


class _Gestures:
    """Only what monitoring reads from the gesture client. Sends nothing."""
    _state = ([0.0] * 29, 0, 5, 0.0)
    _fault = None

    def _fsm_id(self):
        return 802


class _AuthError(Exception):
    status_code = 401


def _unzip(path):
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name).decode("utf-8") for name in z.namelist()}


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


class _Session(unittest.TestCase):
    """One simulated conversation, recorded once and exported as all three reports."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="sb01-monitor-test-")
        cls._saved = (monitor.RUNTIME_BASE, list(monitor._PERSON_TERMS), os.environ.get("ANTHROPIC_API_KEY"))
        monitor.RUNTIME_BASE = os.path.join(cls.work, "runtime")
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        monitor.register_person_terms([NAME])

        with contextlib.redirect_stdout(io.StringIO()):
            mon = monitor.Monitor(script.ROBOT_NAME, interface="eno0", gesture_enabled=True)
            loop = script.SB01ConversationLoop("eno0", "", mon)
            cls.audio = loop.audio = _Audio()
            loop.tts_done.wait = lambda timeout=None: True
            replies = iter(REPLIES)
            loop.claude = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **kw: types.SimpleNamespace(
                content=[types.SimpleNamespace(text=next(replies))])))
            for text in SAID:
                loop.asr_queue.put((text, "", script.time.time()))
            take = loop.asr_queue.get

            def get(timeout=None):
                try:
                    return take(timeout=0.01)
                except queue.Empty:
                    raise KeyboardInterrupt          # nothing left to say: the operator stops the script

            loop.asr_queue.get = get
            with mock.patch.object(script.time, "sleep", lambda seconds: None):
                loop.setup()
                loop.run()

            # what the rest of the system would report during a session
            mon.robot_fsm(_Gestures())
            mon.gesture("started")
            mon.gesture("utterance_done", track_error_max=0.12, blocks=3)
            mon.event("network", layer="dns", host="api.anthropic.com", port=443, ip="192.0.2.10", outcome="ok",
                      expected=True, purpose="Claude API", source="program")
            mon.event("network", layer="connect", protocol="tcp", host="example.org", ip="192.0.2.99", port=443,
                      outcome="failed", error="ConnectionRefusedError: refused", expected=False,
                      purpose="not in expected destination list", source="program")
            mon.event("network", layer="http", client="httpx", method="POST", scheme="https",
                      host="api.anthropic.com", port=443, url="https://api.anthropic.com/v1/messages", status=401,
                      outcome="error_status", expected=True, purpose="Claude API", source="program")
            mon.error("claude", _AuthError(f"invalid x-api-key: {FAKE_KEY}"), endpoint="api.anthropic.com")
            mon.event("asr_dropped", chars=9)
            mon.speech("user", "wait wait", status="not answered")
            mon.close("stopped by operator", "clean")

        cls.mon = mon
        with open(mon.events_path, encoding="utf-8") as handle:
            cls.raw = handle.read()
        cls.events = monitor_reports.load_events(mon.events_path)
        cls.speech = [e for e in cls.events if e["event"] == "speech"]
        cls.commands = [e for e in cls.events if e["event"] == "robot_command"]
        cls.reports, cls.export_log = {}, []
        for audience in monitor_reports.AUDIENCES:
            dest = os.path.join(cls.work, f"{audience}.zip")
            if monitor_reports.export(mon.events_path, mon.session, audience, dest, out=cls.export_log.append):
                cls.reports[audience] = _unzip(dest)

    @classmethod
    def tearDownClass(cls):
        monitor.RUNTIME_BASE, terms, key = cls._saved
        monitor._PERSON_TERMS[:] = terms
        if key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = key
        shutil.rmtree(cls.work, ignore_errors=True)


class ThreeReports(_Session):
    def test_there_are_exactly_three_reports_and_all_export(self):
        self.assertEqual(list(monitor_reports.AUDIENCES), ["team1", "security", "transcript"])
        self.assertEqual(sorted(self.reports), ["security", "team1", "transcript"], self.export_log)

    def test_team1_report_is_unchanged_and_has_no_speech_or_hosts(self):
        team1 = self.reports["team1"]
        self.assertEqual(sorted(team1), ["errors_by_component.csv", "service_status.csv", "summary.md", "turns.csv"])
        text = "".join(team1.values())
        for words in ("cómo estás", "Estoy bien", "wait wait", "xxxxxxxx", "api.anthropic.com", "example.org"):
            self.assertNotIn(words, text)

    def test_monitoring_data_is_kept_outside_the_project_folder(self):
        self.assertFalse(monitor._inside(self.mon.dir, ROOT))


class Transcript(_Session):
    def test_holds_only_the_transcript(self):
        self.assertEqual(sorted(self.reports["transcript"]), ["transcript.csv", "transcript.md"])

    def test_what_the_person_said(self):
        said = [e["text"] for e in self.speech if e["speaker"] == "user"]
        self.assertEqual(len(said), len(SAID) + 1)
        self.assertEqual(said[0], SAID[0])                         # word for word
        self.assertIn("**User:** Hola, ¿cómo estás?", self.reports["transcript"]["transcript.md"])

    def test_what_yotie_said(self):
        said = [e["text"] for e in self.speech if e["speaker"] == "robot"]
        self.assertEqual(len(said), len(REPLIES) + 2)              # greeting + replies + goodbye
        self.assertIn(script.ROBOT_NAME, said[0])
        self.assertEqual(said[3], REPLIES[2])
        self.assertEqual(said[-1], "Goodbye! It was great talking with you.")
        self.assertIn("**Yotie:** I will not repeat that.", self.reports["transcript"]["transcript.md"])

    def test_timestamps_and_speaker_labels(self):
        rows = _rows(self.reports["transcript"]["transcript.csv"])
        self.assertEqual(len(rows), len(self.speech))
        self.assertEqual({r["speaker"] for r in rows}, {"User", "Yotie"})
        stamps = [r["ts"] for r in rows]
        self.assertEqual(stamps, sorted(stamps))
        for stamp in stamps:
            self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}$")
        self.assertEqual([r["speaker"] for r in rows][:9], ["Yotie"] + ["User", "Yotie"] * 4)

    def test_reply_language_is_noted(self):
        rows = _rows(self.reports["transcript"]["transcript.csv"])
        self.assertEqual(rows[2]["language"], "es")

    def test_names_are_replaced_with_exactly_user(self):
        text = "".join(self.reports["transcript"].values())
        self.assertNotIn(NAME, self.raw)
        self.assertNotIn(NAME, text)
        self.assertNotIn("[person]", text)
        self.assertIn("My name is (user) and", text)
        self.assertIn("¡Hola (user)! Estoy bien.", text)
        self.assertIn("Nice to meet you, (user).", text)

    def test_the_robots_own_name_is_not_redacted(self):
        self.assertIn(script.ROBOT_NAME, self.reports["transcript"]["transcript.md"])

    def test_long_speech_is_not_cut_short(self):
        self.assertIn("x" * 900, self.reports["transcript"]["transcript.md"])

    def test_speech_that_was_not_answered_is_marked(self):
        self.assertIn("**User (not answered):** wait wait", self.reports["transcript"]["transcript.md"])

    def test_speech_is_in_no_other_report(self):
        for audience in ("team1", "security"):
            text = "".join(self.reports[audience].values())
            for words in ("cómo estás", "Estoy bien", "wait wait", "xxxxxxxx", "I will not repeat"):
                self.assertNotIn(words, text, audience)


class SecurityReport(_Session):
    def test_files(self):
        self.assertEqual(sorted(self.reports["security"]), sorted([
            "README.md", "summary.md", "indicators.csv", "network_contacts.csv", "errors.csv",
            "errors_by_component.csv", "face_events.csv", "robot_state.csv", "gesture_events.csv",
            "robot_commands.csv", "process_events.csv", "file_access.csv", "turns.csv", "service_status.csv",
            "timeline.jsonl"]))

    def test_network_destinations_and_connection_attempts(self):
        rows = _rows(self.reports["security"]["network_contacts.csv"])
        self.assertEqual({(r["layer"], r["host"], r["outcome"]) for r in rows},
                         {("dns", "api.anthropic.com", "ok"), ("connect", "example.org", "failed"),
                          ("http", "api.anthropic.com", "error_status")})
        indicators = {r["indicator"]: r for r in _rows(self.reports["security"]["indicators.csv"])}
        self.assertEqual(indicators["unexpected_destination"]["severity"], "high")
        self.assertIn("connection_failure", indicators)

    def test_errors_and_authentication_failures(self):
        errors = _rows(self.reports["security"]["errors.csv"])
        self.assertEqual([(r["component"], r["error_type"], r["status"]) for r in errors], [("claude", "_AuthError", "401")])
        indicators = {r["indicator"]: r for r in _rows(self.reports["security"]["indicators.csv"])}
        self.assertEqual(indicators["api_auth_failure"]["severity"], "high")
        self.assertEqual(indicators["api_auth_failure"]["count"], "2")      # the HTTP 401 and the error

    def test_robot_status_and_fsm_id(self):
        rows = _rows(self.reports["security"]["robot_state.csv"])
        self.assertIn(("fsm_id", "802"), {(r["field"], r["new"]) for r in rows})
        status = {r["service"]: r for r in _rows(self.reports["security"]["service_status.csv"])}
        self.assertEqual(status["robot_audio"]["status"], "OK")

    def test_unreadable_fsm_id_is_recorded_as_unreadable(self):
        class Broken:
            def _fsm_id(self):
                raise RuntimeError("no reply")
        seen = []
        with mock.patch.object(monitor.Monitor, "event", lambda self, kind, **fields: seen.append(fields)):
            monitor.Monitor.robot_fsm(self.mon, Broken())
        self.assertEqual(seen[0]["new"], "unreadable")

    def test_audio_commands(self):
        rows = [r for r in _rows(self.reports["security"]["robot_commands.csv"]) if r["target"] == "audio"]
        self.assertEqual([r["action"] for r in rows].count("SetVolume"), 1)
        plays = [r for r in rows if r["action"] == "PlayStream"]
        self.assertEqual(len(plays), len(REPLIES) + 2)             # one per thing Yotie said
        for play in plays:
            self.assertEqual(play["code"], "0")
            self.assertGreater(int(play["bytes"]), 0)
            self.assertTrue(play["stream"])

    def test_led_commands(self):
        rows = [r for r in _rows(self.reports["security"]["robot_commands.csv"]) if r["target"] == "led"]
        self.assertEqual(len(rows), self.audio.calls.count("LedControl"))     # every one that was sent
        self.assertGreater(len(rows), 0)
        self.assertEqual((rows[0]["action"], rows[0]["value"], rows[0]["code"]), ("LedControl", "0,0,128", "0"))

    def test_gesture_summaries(self):
        rows = _rows(self.reports["security"]["gesture_events.csv"])
        self.assertEqual([r["action"] for r in rows], ["started", "utterance_done"])
        self.assertEqual((rows[1]["track_error_max"], rows[1]["blocks"]), ("0.12", "3"))

    def test_chronological_timeline(self):
        timeline = [json.loads(line) for line in self.reports["security"]["timeline.jsonl"].splitlines()]
        stamps = [r["ts"] for r in timeline]
        self.assertEqual(stamps, sorted(stamps))
        kinds = {r["event"] for r in timeline}
        for kind in ("session_start", "network", "error", "robot_state", "robot_command", "gesture", "turn",
                     "check", "session_end"):
            self.assertIn(kind, kinds)
        self.assertNotIn("speech", kinds)

    def test_readme_describes_the_commands_and_the_transcript(self):
        readme = self.reports["security"]["README.md"]
        self.assertIn("robot_commands.csv", readme)
        self.assertIn("separate transcript", readme)


class NoCredentials(_Session):
    SECRETS = (FAKE_KEY, "sk-ant-", "hunter2", "abcdef0123456789")

    def test_never_written_to_temporary_storage(self):
        for secret in self.SECRETS:
            self.assertNotIn(secret, self.raw)

    def test_never_in_any_report(self):
        for audience, files in self.reports.items():
            for secret in self.SECRETS:
                self.assertNotIn(secret, "".join(files.values()), audience)

    def test_redaction_keeps_the_rest_of_the_sentence(self):
        said = [e["text"] for e in self.speech if e["speaker"] == "user"]
        self.assertEqual(said[1], "My name is (user) and my password is [REDACTED]")
        self.assertEqual(said[2], "use api_key=[REDACTED] and Authorization: [REDACTED] please")

    def test_spoken_secrets_in_spanish_and_chinese(self):
        self.assertEqual(monitor.sanitize_speech("mi contraseña es gato123"), "mi contraseña es [REDACTED]")
        self.assertEqual(monitor.sanitize_speech("我的密码是abc123"), "我的密码是[REDACTED]")

    def test_export_is_refused_if_a_credential_is_found(self):
        events = self.events + [{"ts": self.events[-1]["ts"], "event": "speech", "speaker": "user", "text": "ok"}]
        path = os.path.join(self.work, "tampered.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")
        dest, log = os.path.join(self.work, "refused.zip"), []
        with mock.patch.object(monitor_reports, "sanitize_speech", lambda text: "token: abc123" if text == "ok" else text):
            self.assertFalse(monitor_reports.export(path, "s", "transcript", dest, out=log.append))
        self.assertFalse(os.path.exists(dest))
        self.assertIn("REFUSED", log[0])


class ExportIsLocalAndOperatorChosen(_Session):
    def test_building_and_saving_a_report_opens_no_connection(self):
        def refuse(*args, **kwargs):
            raise AssertionError("a report export tried to use the network")

        with mock.patch.object(socket, "getaddrinfo", refuse), mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(monitor_reports.subprocess, "run", refuse):
            for audience in monitor_reports.AUDIENCES:
                dest = os.path.join(self.work, f"offline_{audience}.zip")
                self.assertTrue(monitor_reports.export(self.mon.events_path, self.mon.session, audience, dest,
                                                       out=lambda line: None))

    def test_report_code_has_no_upload_path(self):
        for name in ("teleop/monitor_reports.py", "scripts/monitor_export.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
                source = handle.read()
            imported = {line.split()[1].split(".")[0] for line in source.splitlines()
                        if line.startswith(("import ", "from "))}
            self.assertFalse(imported & {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "smtplib",
                                         "ftplib", "ssl", "paramiko", "boto3"}, name)

    def _cli(self, *args, answers="y\n"):
        env = dict(os.environ, SB01_RUNTIME_DIR=monitor.RUNTIME_BASE, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "monitor_export.py"), *args],
                              input=answers, capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT)

    def test_export_command_saves_to_a_folder_the_operator_names(self):
        drive = os.path.join(self.work, "mounted-drive")           # stands in for a mounted encrypted drive
        os.mkdir(drive)
        for audience in monitor_reports.AUDIENCES:
            done = self._cli("--audience", audience, "--session", self.mon.session, "--output", drive)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(sorted(os.listdir(drive)), sorted(
            f"sb01_{audience}_{self.mon.session}.zip" for audience in monitor_reports.AUDIENCES))
        self.assertTrue(os.path.isfile(self.mon.events_path))      # kept: --discard was not given

    def test_export_command_asks_before_a_sensitive_report(self):
        drive = os.path.join(self.work, "declined")
        os.mkdir(drive)
        for audience, word in (("transcript", "CONVERSATION TRANSCRIPT"), ("security", "SECURITY REPORT")):
            done = self._cli("--audience", audience, "--session", self.mon.session, "--output", drive, answers="n\n")
            self.assertNotEqual(done.returncode, 0)
            self.assertIn(word, done.stdout)
        self.assertEqual(os.listdir(drive), [])

    def test_export_command_refuses_the_project_folder(self):
        done = self._cli("--audience", "team1", "--session", self.mon.session, "--output", ROOT)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Refusing to write inside the project folder", done.stderr)
        self.assertFalse(os.path.exists(os.path.join(ROOT, f"sb01_team1_{self.mon.session}.zip")))

    def test_shutdown_prompt_offers_all_three_and_deletes_temporary_data(self):
        session_dir = os.path.join(monitor.RUNTIME_BASE, "copy_for_prompt")
        shutil.copytree(self.mon.dir, session_dir)
        dest = os.path.join(self.work, "picked.zip")
        answers, shown = iter(["y", "3", "y", dest]), []

        def ask(question):
            shown.append(question)
            return next(answers)

        with mock.patch.object(monitor_reports, "_zenity", lambda *a: ("unavailable", None)):
            saved = monitor_reports.offer_save(session_dir, "copy_for_prompt", ask=ask, out=shown.append)
        self.assertEqual(saved, dest)
        self.assertEqual(sorted(_unzip(dest)), ["transcript.csv", "transcript.md"])
        self.assertTrue(any("[1] Team 1" in s and "[2] Security" in s and "[3] Conversation transcript" in s
                            for s in shown))
        self.assertTrue(any("never uploaded" in s for s in shown))
        self.assertFalse(os.path.exists(session_dir))

    def test_declining_at_the_prompt_saves_nothing(self):
        session_dir = os.path.join(monitor.RUNTIME_BASE, "copy_declined")
        shutil.copytree(self.mon.dir, session_dir)
        before = set(os.listdir(self.work))
        saved = monitor_reports.offer_save(session_dir, "copy_declined", ask=lambda q: "n", out=lambda s: None)
        self.assertIsNone(saved)
        self.assertEqual(set(os.listdir(self.work)), before)
        self.assertFalse(os.path.exists(session_dir))


if __name__ == "__main__":
    unittest.main()
