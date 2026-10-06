"""
monitor.py  -  operational + security telemetry for sb01

VALIDATION STATUS: simulation only. Not yet run on the physical G1.
Change VALIDATION below once a real-robot session has been checked.

Where data lives
  During a session everything is written to a private temporary folder OUTSIDE
  the repository:  <system temp>/sb01-monitor-<uid>/<session>/
      events.jsonl   one redacted JSON object per event (flushed per line)
      status.json    live status, rewritten every few seconds
      owner.json     pid of the running session (for crash recovery)
  Nothing is saved permanently unless the operator chooses to at shutdown
  (see teleop/monitor_reports.py). A session that crashes leaves its folder
  behind; the next startup offers to save it and then deletes it.

What is recorded
  timings, errors (component, type, redacted message, endpoint), service
  checks, outbound network contacts (DNS, TCP/UDP connect, HTTP method/URL/
  status - all redacted), face-recognition outcome metadata, robot mode
  changes seen by the gesture client, gesture start/fault events, file and
  config access, process start/stop, what the person said and what the robot
  said (redacted text; exported only in the separate transcript report), and
  the commands sent to the robot's audio and LED service.

Never recorded
  API keys, tokens, cookies, passwords, authorization headers, URL query
  values, audio, images, face encodings, person names (enrolled names are
  written as "(user)" in speech and "[person]" elsewhere), emotions,
  home-directory paths.

Network hooks are passive: every wrapper calls the original function, returns
its result unchanged and re-raises its exception unchanged. A failure inside
the monitor itself is swallowed and can never reach the caller.
"""

import getpass
import importlib
import ipaddress
import json
import os
import platform
import queue
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime

VALIDATION = "simulation only - real G1 untested"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HEARTBEAT_SECONDS = 1.0           # status + read-only robot/gesture state poll
ASR_SILENCE_WARN = 300.0          # no ASR message for this long -> status "degraded"
MAX_MESSAGE_CHARS = 300
MAX_SPEECH_CHARS = 4000           # one spoken phrase or reply in the transcript
MAX_EVENTS = 200_000              # after this, successful network events are no longer written
LEFTOVER_MAX_AGE_DAYS = 7

# Destinations the program is designed to contact. Anything else is "unexpected".
EXPECTED_HOSTS = {
    "www.csusb.edu":            "CSUSB context preload",
    "csusb.edu":                "CSUSB context preload",
    "wttr.in":                  "weather lookup",
    "api.anthropic.com":        "Claude API",
    "speech.platform.bing.com": "Microsoft Edge TTS",
}
# Hosts whose URL path is built from what a student said (e.g. a city name).
USER_DERIVED_PATH_HOSTS = {"wttr.in"}


def _uid() -> str:
    if hasattr(os, "getuid"):
        return str(os.getuid())
    return re.sub(r"\W", "", getpass.getuser()) or "user"


def _inside(path, root) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:            # different drives on Windows
        return False


def _resolve_runtime_base():
    """Temporary storage is never allowed inside the project folder."""
    default = os.path.abspath(os.path.join(tempfile.gettempdir(), f"sb01-monitor-{_uid()}"))
    chosen = os.environ.get("SB01_RUNTIME_DIR", "").strip()
    if chosen:
        chosen = os.path.abspath(os.path.expanduser(chosen))
        if not _inside(chosen, REPO_ROOT):
            return chosen, None
        warning = "[monitor] SB01_RUNTIME_DIR points inside the project folder; ignoring it."
    else:
        warning = None
    if _inside(default, REPO_ROOT):
        return None, "[monitor] DISABLED: the system temp folder is inside the project folder."
    return default, warning


RUNTIME_BASE, RUNTIME_WARNING = _resolve_runtime_base()

# ── redaction ────────────────────────────────────────────────────────────────

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]+"),
    re.compile(r"(?i)\b(x-api-key|api[_-]?key|authorization|proxy-authorization|cookie|set-cookie"
               r"|password|passwd|pwd|secret|token|trustedclienttoken|sec-ms-gec)"
               r"(['\"]?\s*[:=]\s*['\"]?)[^\s'\",;}&]+"),
]
_URL_PATTERN = re.compile(r"\b(?:https?|wss?)://[^\s'\"<>]+")
# memory/profiles/<name>.json, memory/sessions/<name>/..., memory/faces/<name>.jpg
_MEMORY_PATH = re.compile(r"(memory[/\\](?:profiles|sessions|faces)[/\\])[^/\\\s'\"]+")
_MEDIA_FILE = re.compile(r"[^\s'\"/\\<>]*\.(wav|mp3|pcm|ogg|flac|jpg|jpeg|png|npy)\b", re.I)
_PERSON_TERMS: list = []      # compiled patterns for enrolled names; held in memory, never written


def register_person_terms(names):
    """Redact these names wherever they appear. The names themselves are never stored."""
    for name in names or ():
        name = str(name).strip()
        if len(name) >= 2:
            pattern = re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])")
            if all(p.pattern != pattern.pattern for p in _PERSON_TERMS):
                _PERSON_TERMS.append(pattern)


def person_terms_from_memory():
    """Names of enrolled faces and saved profiles (file names only, contents never read)."""
    names = set()
    memory = os.path.join(REPO_ROOT, "memory")
    for sub_dir in ("faces", "profiles", "sessions"):
        try:
            for entry in os.listdir(os.path.join(memory, sub_dir)):
                names.add(os.path.splitext(entry)[0])
        except OSError:
            pass
    return names


def _home_patterns():
    # Only absolute paths (start of text, or after a space, quote, = or :) - never
    # a relative path such as memory/sessions/[person].
    lead = r"(?:(?<=^)|(?<=[\s'\"=:(]))"
    pats = [re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s'\"]+"),
            re.compile(lead + r"/(?:home|Users)/[^/\s'\"]+"),
            re.compile(lead + r"/sessions/[^/\s'\"]+"),
            re.compile(lead + r"/root\b")]
    home = os.path.expanduser("~")
    if len(home) > 1:
        pats.insert(0, re.compile(re.escape(home)))
    return pats


_HOME_PATTERNS = _home_patterns()


def redact_url(url) -> str:
    """Keep scheme, host, port and path; drop credentials, redact every query
    value and any path built from student speech."""
    try:
        parts = urllib.parse.urlsplit(str(url))
        host = parts.hostname or ""
        netloc = host + (f":{parts.port}" if parts.port else "")
        path = parts.path
        if host in USER_DERIVED_PATH_HOSTS and path not in ("", "/"):
            path = "/[from-speech]"
        query = "&".join(f"{k}=[REDACTED]" for k, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))
    except Exception:
        return "[unparseable-url]"


def _redact_url_match(m) -> str:
    url = m.group(0)
    tail = ""
    while url and url[-1] in ".,;:)]}>":          # sentence punctuation is not part of the URL
        tail = url[-1] + tail
        url = url[:-1]
    return redact_url(url) + tail


def sanitize(text, person: str = "[person]", limit: int = MAX_MESSAGE_CHARS) -> str:
    """Remove secrets, URL query values, speech-derived URL paths and home paths;
    keep the endpoint and event context; cap the length."""
    text = str(text)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if len(key) >= 8:
        text = text.replace(key, "[REDACTED_KEY]")
    text = _URL_PATTERN.sub(_redact_url_match, text)
    text = _MEMORY_PATH.sub(lambda m: m.group(1) + "[person]", text)
    text = _MEDIA_FILE.sub(lambda m: f"[{m.group(1).lower()}-file]", text)
    for pattern in _PERSON_TERMS:
        text = pattern.sub(person, text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + m.group(2) if m.lastindex else "") + "[REDACTED]", text)
    text = text.replace(REPO_ROOT, "<repo>")
    for pattern in _HOME_PATTERNS:
        text = pattern.sub("~", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


# A secret said aloud ("my password is ..."). Only the word right after it is removed.
_SPOKEN_SECRET = re.compile(
    r"(?i)(\b(?:password|passcode|passphrase|pin number|pin code|pin|api key|access code|token"
    r"|contraseña|clave)\s+(?:is|was|es)\s+|密码是\s*)\S+")


def sanitize_speech(text) -> str:
    """What was said, for the transcript: same redaction as sanitize(), enrolled
    names become "(user)", and the text is not cut at the short message length."""
    text = _SPOKEN_SECRET.sub(lambda m: m.group(1) + "[REDACTED]", str(text))
    return sanitize(text, person="(user)", limit=MAX_SPEECH_CHARS)


def repo_relative(path) -> str:
    try:
        rel = os.path.relpath(os.path.abspath(path), REPO_ROOT)
        return rel if not rel.startswith("..") else sanitize(path)
    except ValueError:
        return sanitize(path)


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))], 1)


# ── crash-recovery helpers ───────────────────────────────────────────────────

def _pid_alive(pid) -> bool:
    """Does process `pid` still exist? Never signals or terminates anything.
    (On Windows os.kill(pid, 0) TERMINATES the process, so it is not used there.)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == 259             # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)          # POSIX: signal 0 only checks existence
        return True
    except PermissionError:
        return True              # exists, owned by another user
    except OSError:
        return False


def list_sessions(base: str = None) -> list[dict]:
    base = base or RUNTIME_BASE
    out = []
    if not base or not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        events = os.path.join(path, "events.jsonl")
        if not os.path.isfile(events):
            continue
        try:
            owner = json.load(open(os.path.join(path, "owner.json")))
        except Exception:
            owner = {}
        try:
            exit_type = open(os.path.join(path, "closed")).read().strip()
        except OSError:
            exit_type = None
        out.append({"session": name, "path": path, "events": events,
                    "alive": owner.get("pid") != os.getpid() and _pid_alive(owner.get("pid")),
                    "closed": exit_type is not None, "exit_type": exit_type,
                    "age_days": (time.time() - os.path.getmtime(events)) / 86400})
    return out


def discard_session(path: str):
    if path and RUNTIME_BASE and os.path.abspath(path).startswith(RUNTIME_BASE + os.sep):
        shutil.rmtree(path, ignore_errors=True)


def cleanup_old_sessions(base: str = None) -> int:
    removed = 0
    for s in list_sessions(base):
        if not s["alive"] and s["age_days"] > LEFTOVER_MAX_AGE_DAYS:
            discard_session(s["path"])
            removed += 1
    return removed


# ── read-only access to the gesture client ───────────────────────────────────

_STOP = object()     # writer-thread sentinel


def read_gesture_client_state(client) -> dict | None:
    """The ONLY place monitoring touches GestureClient internals.

    teleop/gesture_client.py has no public read-only API and must not be
    modified, so this reads two attributes it already maintains:
      _state = (q, mode_pr, mode_machine, monotonic time)  from rt/lowstate
      _fault = text of a fault raised by its FSM/state guard thread
    Plain attribute reads: nothing is called, locked or sent to the robot.
    Returns None if the attributes are missing or have an unexpected shape
    (e.g. after a gesture_client.py change), so the caller can record that
    the observation is unavailable instead of guessing.
    Not visible here: tracking, write and stream faults, which the client
    only prints.
    """
    if client is None or not hasattr(client, "_state") or not hasattr(client, "_fault"):
        return None
    try:
        state = getattr(client, "_state")
        fault = getattr(client, "_fault")
        modes = None
        if state is not None:
            if len(state) < 3:
                return None
            modes = (int(state[1]), int(state[2]))
        return {"modes": modes, "fault": str(fault) if fault else None}
    except Exception:
        return None


# ── monitor ──────────────────────────────────────────────────────────────────

class Monitor:
    METRICS = ("claude_ms", "tts_ms", "response_ms", "turn_ms")

    def __init__(self, service: str = "sb01", **process_fields):
        """Never raises. If temporary storage cannot be created, monitoring is
        disabled (enabled=False) and every method becomes a no-op."""
        self.service = service
        self.simulated = os.environ.get("SB01_SIMULATED") == "1"
        self.started = time.time()
        # Unique per process, so two sessions started in the same second never share a folder.
        self.session = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
        self.enabled = False
        self.dir = self.events_path = self.status_path = None
        self._file = None
        self._lock = threading.RLock()
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._write_failed = False
        self._count = 0
        register_person_terms(person_terms_from_memory())
        try:
            if RUNTIME_WARNING:
                print(RUNTIME_WARNING)
            if not RUNTIME_BASE:
                raise RuntimeError("no safe temporary folder")
            os.makedirs(RUNTIME_BASE, mode=0o700, exist_ok=True)
            try:
                os.chmod(RUNTIME_BASE, 0o700)
            except OSError:
                pass
            base_id, n = self.session, 1
            while True:
                self.dir = os.path.join(RUNTIME_BASE, self.session)
                try:
                    os.makedirs(self.dir, mode=0o700)
                    break
                except FileExistsError:
                    n += 1
                    self.session = f"{base_id}_{n}"
            with open(os.path.join(self.dir, "owner.json"), "w") as f:
                json.dump({"pid": os.getpid(), "started": self.started}, f)
            self.events_path = os.path.join(self.dir, "events.jsonl")
            self.status_path = os.path.join(self.dir, "status.json")
            self._file = open(self.events_path, "a", encoding="utf-8")
            try:
                os.chmod(self.events_path, 0o600)
            except OSError:
                pass
            self.enabled = True
        except Exception as exc:
            print(f"[monitor] DISABLED - could not create temporary storage "
                  f"({sanitize(f'{type(exc).__name__}: {exc}')}). The conversation continues without monitoring.")

        self.state = "starting"
        self.turns = 0
        self.errors: Counter = Counter()
        self.last_error: dict | None = None
        self.last_asr: float | None = None
        self.checks: dict = {}
        self.latency: dict[str, deque] = {k: deque(maxlen=500) for k in self.METRICS}
        self.exit_reason: str | None = None
        self._gestures = None
        self._robot_seen: tuple | None = None
        self._gesture_fault_seen: str | None = None
        self._once: set = set()

        self._stop = threading.Event()
        self._closed = False
        if not self.enabled:
            return
        self._writer = threading.Thread(target=self._write_loop, name="sb01-monitor-writer", daemon=True)
        self._writer.start()
        self._heartbeat = threading.Thread(target=self._beat, name="sb01-monitor-heartbeat", daemon=True)
        self.event("session_start", validation=VALIDATION, simulated=self.simulated)
        self.event("process", action="start", pid=os.getpid(),
                   python=platform.python_version(), platform=platform.system(),
                   **{k: sanitize(v) if isinstance(v, str) else v for k, v in process_fields.items()})
        self._heartbeat.start()

    # ── writing ──────────────────────────────────────────────────────────────
    # event() only timestamps the record and hands it to a queue: no file I/O,
    # no exception can reach the caller. A background thread does the writing.

    def event(self, kind: str, **fields):
        if not self.enabled or self._closed:
            return
        try:
            with self._lock:
                self._count += 1
                if self._count > MAX_EVENTS and kind == "network" and fields.get("outcome") == "ok":
                    if "cap" not in self._once:
                        self._once.add("cap")
                        kind, fields = "event_cap_reached", {"max_events": MAX_EVENTS}
                    else:
                        return
            self._queue.put({"ts": datetime.now().isoformat(timespec="milliseconds"),
                             "session": self.session, "event": kind, **fields})
        except Exception:
            pass

    def _write_loop(self):
        while True:
            record = self._queue.get()
            batch = [record]
            try:                                   # take whatever else is already waiting
                while len(batch) < 500:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            stop = any(r is _STOP for r in batch)
            if not self._write_failed:
                try:
                    for r in batch:
                        if r is not _STOP:
                            self._file.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
                    self._file.flush()
                except Exception as exc:
                    self._write_failed = True
                    try:
                        print(f"[monitor] stopped recording ({sanitize(f'{type(exc).__name__}: {exc}')}); "
                              "the conversation continues without monitoring")
                    except Exception:
                        pass
            if stop:
                return

    def once(self, key) -> bool:
        """True the first time it is called with this key."""
        if key in self._once:
            return False
        self._once.add(key)
        return True

    # ── typed events ─────────────────────────────────────────────────────────

    def error(self, component: str, exc, endpoint: str | None = None):
        if not self.enabled:
            return
        error_type = type(exc).__name__ if isinstance(exc, BaseException) else "message"
        raw = f"{error_type}: {exc}" if isinstance(exc, BaseException) else str(exc)
        status = getattr(exc, "status_code", None)
        message = sanitize(raw)
        self.errors[component] += 1
        self.last_error = {"component": component, "error_type": error_type, "message": message,
                           "ts": datetime.now().isoformat(timespec="seconds")}
        self.event("error", component=component, error_type=error_type, message=message,
                   endpoint=endpoint, status=status if isinstance(status, int) else None)
        print(f"[monitor] {component} error: {message}")

    def check(self, name: str, ok, detail: str = ""):
        """ok: True, False, or None when the result cannot be confirmed."""
        if not self.enabled:
            return
        ok = None if ok is None else bool(ok)
        detail = sanitize(detail)
        self.checks[name] = {"ok": ok, "detail": detail, "ts": datetime.now().isoformat(timespec="seconds")}
        self.event("check", service=name, ok=ok, detail=detail)
        label = {True: "OK  ", False: "FAIL", None: "N/A "}[ok]
        print(f"[monitor] check {name:<14} {label} {detail}")

    def observe(self, name: str, seen: bool, note: str = ""):
        """An informational signal whose meaning is not confirmed; never affects health."""
        self.event("observation", name=name, seen=bool(seen), note=sanitize(note))

    def file_access(self, op: str, target: str, ok: bool = True, error=None, **extra):
        self.event("file_access", op=op, target=target, ok=bool(ok),
                   error=sanitize(f"{type(error).__name__}: {error}") if isinstance(error, BaseException)
                   else (sanitize(error) if error else None), **extra)

    def face(self, result: dict | None):
        result = result or {"outcome": "unknown"}
        allowed = ("outcome", "confidence", "enrolled", "faces_seen", "frames")
        self.event("face", **{k: result.get(k) for k in allowed})

    def gesture(self, action: str, **fields):
        self.event("gesture", action=action, **{k: sanitize(v) if isinstance(v, str) else v
                                                for k, v in fields.items()})

    def speech(self, speaker: str, text: str, language: str | None = None, status: str | None = None):
        """What was said. speaker: "user" or "robot". Redacted before it is stored."""
        self.event("speech", speaker=speaker, text=sanitize_speech(text), language=language, status=status)

    def command(self, target: str, action: str, **fields):
        """A command the program sent to the robot (already sent; this only records it)."""
        self.event("robot_command", target=target, action=action,
                   **{k: sanitize(v) if isinstance(v, str) else v for k, v in fields.items()})

    def robot_fsm(self, client):
        """Record the robot's FSM id once, at startup, using the gesture client's
        own read-only query (the one it runs before every gesture). No gesture is
        playing yet, so this cannot delay its safety checks. Never raises."""
        try:
            fsm = client._fsm_id()
            fsm = fsm if isinstance(fsm, int) and not isinstance(fsm, bool) else None
        except Exception:
            fsm = None
        self.event("robot_state", field="fsm_id", old=None, new="unreadable" if fsm is None else fsm,
                   change="initial", expected=fsm is not None)

    def turn(self, **fields):
        self.turns += 1
        record = {k: fields.get(k) for k in self.METRICS}
        record.update(error=fields.get("error"), user_chars=fields.get("user_chars"),
                      reply_chars=fields.get("reply_chars"))
        for key in ("response_ms", "turn_ms"):
            if record[key] is not None:
                self.latency[key].append(record[key])
        self.event("turn", n=self.turns, **record)
        parts = [f"{k}={record[k]:.0f}ms" for k in ("claude_ms", "tts_ms", "response_ms")
                 if isinstance(record.get(k), (int, float))]
        print(f"[monitor] turn {self.turns}: " + " ".join(parts))

    def set_state(self, state: str):
        self.state = state

    def asr_seen(self):
        self.last_asr = time.time()

    @contextmanager
    def timed(self, metric: str, out: dict):
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            out[metric] = round(elapsed, 1)
            self.latency[metric].append(elapsed)

    # ── service checks ───────────────────────────────────────────────────────

    def startup_checks(self, gesture_url: str = ""):
        if not self.enabled:
            return
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.check("api_key", has_key, "present" if has_key else "missing - see .env")
        with network_source("monitor_check"):
            for name, host in (("claude_api", "api.anthropic.com"), ("edge_tts_net", "speech.platform.bing.com")):
                started = time.perf_counter()
                try:
                    socket.create_connection((host, 443), timeout=3).close()
                    self.check(name, True, f"{host}:443 reachable in {(time.perf_counter()-started)*1000:.0f}ms")
                except OSError as exc:
                    self.check(name, False, f"{host}:443 unreachable: {type(exc).__name__}: {exc}")
            if gesture_url:
                try:
                    with urllib.request.urlopen(gesture_url.rstrip("/") + "/health", timeout=3) as resp:
                        body = json.loads(resp.read() or b"{}")
                    ok = body.get("status") == "ok"
                    self.check("gesture_server", ok, gesture_url)
                    self.gesture("server_health", ok=ok, endpoint=gesture_url)
                except Exception as exc:
                    self.check("gesture_server", False, f"{gesture_url}: {type(exc).__name__}: {exc}")
                    self.gesture("server_health", ok=False, endpoint=gesture_url,
                                 error=f"{type(exc).__name__}: {exc}")

    # ── read-only robot / gesture observation ────────────────────────────────

    def watch_gestures(self, gestures):
        """Poll attributes the gesture client already keeps. Read-only: nothing
        is called on the client and nothing is sent to the robot."""
        self._gestures = gestures

    def _poll_gestures(self):
        g = self._gestures
        if g is None:
            return
        observed = read_gesture_client_state(g)
        if observed is None:
            if self.once("gesture_state_unavailable"):
                self.observe("gesture_client_state", False,
                             "GestureClient._state/_fault missing or changed shape; robot mode and "
                             "guard faults are not being recorded")
            return
        if observed["modes"] is not None:
            seen = observed["modes"]                     # (mode_pr, mode_machine)
            if self._robot_seen is None:
                self.event("robot_state", field="mode_pr/mode_machine", old=None,
                           new=f"{seen[0]}/{seen[1]}", change="initial", expected=True)
            elif seen != self._robot_seen:
                for i, fieldname in enumerate(("mode_pr", "mode_machine")):
                    if seen[i] != self._robot_seen[i]:
                        self.event("robot_state", field=fieldname, old=self._robot_seen[i], new=seen[i],
                                   change="changed", expected=False)
            self._robot_seen = seen
        fault = observed["fault"]
        if fault and fault != self._gesture_fault_seen:
            self.gesture("fault", detail=str(fault))
        self._gesture_fault_seen = fault

    # ── status / shutdown ────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        asr_age = None if self.last_asr is None else round(time.time() - self.last_asr, 1)
        failing = [name for name, c in self.checks.items() if c["ok"] is False]
        health = ("stopped" if self.state == "stopped" else
                  "degraded" if failing or (asr_age is not None and asr_age > ASR_SILENCE_WARN) else "ok")
        return {
            "service": self.service, "session": self.session, "validation": VALIDATION,
            "simulated": self.simulated, "health": health, "state": self.state,
            "exit_reason": self.exit_reason, "uptime_s": round(time.time() - self.started),
            "turns": self.turns, "seconds_since_last_asr": asr_age, "errors": dict(self.errors),
            "last_error": self.last_error, "checks": self.checks,
            "latency_ms": {k: {"p50": _percentile(v, 50), "p95": _percentile(v, 95),
                               "max": _percentile(v, 100), "n": len(v)} for k, v in self.latency.items()},
            "updated": datetime.now().isoformat(timespec="seconds"),
        }

    def _write_status(self):
        if not self.enabled or self._write_failed:
            return
        tmp = self.status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.snapshot(), f, indent=2)
        os.replace(tmp, self.status_path)

    def _beat(self):
        last_status = 0.0
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                self._poll_gestures()
                if time.time() - last_status >= 5.0:
                    self._write_status()
                    last_status = time.time()
            except Exception:
                pass

    def close(self, exit_reason: str = "stopped by operator", exit_type: str = "clean"):
        """Record the shutdown and flush. Never raises.
        exit_type: clean | signal | interrupted | exit | crashed"""
        try:
            uninstall_hooks()
        except Exception:
            pass
        if not self.enabled or self._closed:
            return
        try:
            self._stop.set()
            try:
                self._poll_gestures()
            except Exception:
                pass
            self.state = "stopped"
            self.exit_reason = sanitize(exit_reason)
            self.event("process", action="stop", exit_type=exit_type, exit_reason=self.exit_reason)
            self.event("session_end", exit_reason=self.exit_reason, exit_type=exit_type,
                       turns=self.turns, errors=dict(self.errors))
            self._closed = True
            self._queue.put(_STOP)
            self._writer.join(timeout=5.0)
            try:
                self._write_status()
                with open(os.path.join(self.dir, "closed"), "w") as f:
                    f.write(exit_type)
            except Exception:
                pass
            lat = self.snapshot()["latency_ms"]["response_ms"]
            print(f"[monitor] session {self.session} ended ({self.exit_reason}): {self.turns} turns, "
                  f"{sum(self.errors.values())} errors, response p50={lat['p50']}ms p95={lat['p95']}ms")
            if self._write_failed:
                print("[monitor] note: recording stopped early because the temporary folder could not be written")
        except Exception:
            pass
        finally:
            try:
                with self._lock:
                    self._file.close()
            except Exception:
                pass


# ── passive network hooks ────────────────────────────────────────────────────

_MON: Monitor | None = None
_EXTRA_EXPECTED: dict[str, str] = {}      # "host:port" -> purpose (gesture server)
_DNS_CACHE: dict[str, str] = {}           # ip -> hostname
_ORIG: dict = {}
_TLS = threading.local()


@contextmanager
def network_source(name: str):
    previous = getattr(_TLS, "source", None)
    _TLS.source = name
    try:
        yield
    finally:
        _TLS.source = previous


def _is_ip(host) -> bool:
    try:
        ipaddress.ip_address(str(host))
        return True
    except ValueError:
        return False


def _expectation(host, port) -> tuple[bool, str]:
    host = (host or "").lower().rstrip(".")
    if f"{host}:{port}" in _EXTRA_EXPECTED:
        return True, _EXTRA_EXPECTED[f"{host}:{port}"]
    if host in EXPECTED_HOSTS:
        return True, EXPECTED_HOSTS[host]
    return False, "not in expected destination list"


def _record(**fields):
    mon = _MON
    if mon is None:
        return
    try:
        host = fields.get("host")
        expected, purpose = _expectation(host, fields.get("port"))
        fields.setdefault("expected", expected)
        fields.setdefault("purpose", purpose)
        if fields.get("error"):
            fields["error"] = sanitize(fields["error"])
        fields["source"] = getattr(_TLS, "source", None) or "program"
        mon.event("network", **fields)
    except Exception:
        pass


def _ms(started):
    return round((time.perf_counter() - started) * 1000.0, 1)


def _hook_getaddrinfo(host, port, *args, **kwargs):
    started = time.perf_counter()
    try:
        result = _ORIG["getaddrinfo"](host, port, *args, **kwargs)
    except Exception as exc:
        try:
            if host and not _is_ip(host):
                _record(layer="dns", host=str(host), port=port, outcome="failed",
                        error=f"{type(exc).__name__}: {exc}", duration_ms=_ms(started))
        except Exception:
            pass
        raise
    try:
        if host and not _is_ip(host) and str(host) != "localhost":
            ips = sorted({r[4][0] for r in result})
            for ip in ips:
                _DNS_CACHE[ip] = str(host)
            _record(layer="dns", host=str(host), port=port, ip=",".join(ips[:4]), outcome="ok",
                    duration_ms=_ms(started))
    except Exception:
        pass
    return result


def _hook_connect(self, address):
    try:
        watch = self.family in (socket.AF_INET, socket.AF_INET6)
    except Exception:
        watch = False
    if not watch:
        return _ORIG["connect"](self, address)
    started = time.perf_counter()
    try:
        result = _ORIG["connect"](self, address)
    except BlockingIOError:
        _connect_record(self, address, "initiated", None, started)    # non-blocking (asyncio)
        raise
    except Exception as exc:
        _connect_record(self, address, "failed", exc, started)
        raise
    _connect_record(self, address, "ok", None, started)
    return result


def _connect_record(sock, address, outcome, exc, started):
    try:
        ip, port = address[0], address[1]
        proto = "udp" if sock.type == socket.SOCK_DGRAM else "tcp"
        host = _DNS_CACHE.get(ip, ip)
        _record(layer="connect", protocol=proto, host=host, ip=ip, port=port, outcome=outcome,
                error=f"{type(exc).__name__}: {exc}" if exc else None, duration_ms=_ms(started))
    except Exception:
        pass


def _http_record(client, method, url, status, exc, started):
    try:
        parts = urllib.parse.urlsplit(str(url))
        port = parts.port or {"https": 443, "wss": 443, "http": 80, "ws": 80}.get(parts.scheme)
        outcome = ("failed" if exc is not None and status is None
                   else "error_status" if isinstance(status, int) and status >= 400 else "ok")
        _record(layer="http", client=client, method=str(method).upper(), scheme=parts.scheme,
                host=parts.hostname, port=port, url=redact_url(url), status=status, outcome=outcome,
                error=f"{type(exc).__name__}: {exc}" if exc else None, duration_ms=_ms(started))
    except Exception:
        pass


def _hook_urlopen(url, *args, **kwargs):
    started = time.perf_counter()
    try:
        if isinstance(url, urllib.request.Request):
            method, full = url.get_method(), url.full_url
        else:
            data = kwargs.get("data", args[0] if args else None)
            method, full = ("POST" if data is not None else "GET"), url
    except Exception:
        method, full = "?", url
    try:
        resp = _ORIG["urlopen"](url, *args, **kwargs)
    except Exception as exc:
        _http_record("urllib", method, full, getattr(exc, "code", None), exc, started)
        raise
    _http_record("urllib", method, full, getattr(resp, "status", None), None, started)
    return resp


def _make_send_hook(module_name):
    """Client.send wrapper for httpx (Anthropic SDK 0.x) or httpx2 (Anthropic SDK 1.x)."""
    def _hook_send(self, request, *args, **kwargs):
        started = time.perf_counter()
        try:
            resp = _ORIG[module_name](self, request, *args, **kwargs)
        except Exception as exc:
            _http_record(module_name, getattr(request, "method", "?"), getattr(request, "url", ""), None, exc, started)
            raise
        _http_record(module_name, request.method, request.url, getattr(resp, "status_code", None), None, started)
        return resp
    return _hook_send


_HTTP_CLIENT_MODULES = ("httpx", "httpx2")


async def _hook_aiohttp_request(self, method, str_or_url, *args, **kwargs):
    started = time.perf_counter()
    try:
        resp = await _ORIG["aiohttp_request"](self, method, str_or_url, *args, **kwargs)
    except Exception as exc:
        _http_record("aiohttp", method, str_or_url, getattr(exc, "status", None), exc, started)
        raise
    _http_record("aiohttp", method, str_or_url, getattr(resp, "status", None), None, started)
    return resp


def install_hooks(mon: Monitor, gesture_url: str = "") -> list[str]:
    """Attach passive recorders. Returns the list of layers instrumented."""
    global _MON
    _MON = mon
    if gesture_url:
        p = urllib.parse.urlsplit(gesture_url)
        if p.hostname:
            _EXTRA_EXPECTED[f"{p.hostname}:{p.port or 80}"] = "gesture server"
    layers = []
    if "getaddrinfo" not in _ORIG:
        _ORIG["getaddrinfo"] = socket.getaddrinfo
        socket.getaddrinfo = _hook_getaddrinfo
        _ORIG["connect"] = socket.socket.connect
        socket.socket.connect = _hook_connect
        _ORIG["urlopen"] = urllib.request.urlopen
        urllib.request.urlopen = _hook_urlopen
        # Optional: these come with the anthropic / edge-tts packages that setup.sh
        # installs. If one is missing, that layer is skipped (DNS/connect still see
        # the traffic) and the "instrumentation" event records which layers ran.
        for module_name in _HTTP_CLIENT_MODULES:
            try:
                module = importlib.import_module(module_name)
                _ORIG[module_name] = module.Client.send
                module.Client.send = _make_send_hook(module_name)
            except Exception:
                pass
        try:
            import aiohttp
            _ORIG["aiohttp_request"] = aiohttp.ClientSession._request
            aiohttp.ClientSession._request = _hook_aiohttp_request
        except Exception:
            pass
    layers = ["dns", "connect", "urllib"] + [n for n in _HTTP_CLIENT_MODULES if n in _ORIG]
    layers += ["aiohttp"] if "aiohttp_request" in _ORIG else []
    mon.event("instrumentation", layers=layers)
    return layers


def uninstall_hooks():
    global _MON
    _MON = None
    if "getaddrinfo" in _ORIG:
        socket.getaddrinfo = _ORIG.pop("getaddrinfo")
        socket.socket.connect = _ORIG.pop("connect")
        urllib.request.urlopen = _ORIG.pop("urlopen")
    for module_name in _HTTP_CLIENT_MODULES:
        if module_name in _ORIG:
            importlib.import_module(module_name).Client.send = _ORIG.pop(module_name)
    if "aiohttp_request" in _ORIG:
        import aiohttp
        aiohttp.ClientSession._request = _ORIG.pop("aiohttp_request")
