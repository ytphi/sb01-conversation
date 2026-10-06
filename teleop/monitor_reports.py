"""
monitor_reports.py  -  build Team 1 / Security / Transcript report packages and
save them only where the operator chooses.

  build(events, session, audience)  -> {filename: text}
  offer_save(session_dir, ...)      -> interactive "Save monitoring report? [y/N]" flow
  choose_destination(...)           -> zenity Save As dialog, or terminal fallback
  write_zip(files, dest)            -> atomic write to the chosen path only

Every field is copied from an allowlist and passed through sanitize() again,
then every file is scanned; if anything sensitive is found nothing is written.
"""

import csv
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from collections import Counter, defaultdict
from datetime import datetime

from teleop.monitor import (REPO_ROOT, VALIDATION, discard_session, person_terms_from_memory,
                            register_person_terms, sanitize, sanitize_speech)

AUDIENCES = {"team1": "Team 1 operational report", "security": "Security-study report",
             "transcript": "Conversation transcript"}
SENSITIVE = ("security", "transcript")       # saved owner-only, after a warning
SECURITY_WARNING = """
  WARNING - SECURITY REPORT
  This ZIP contains sensitive metadata: network destinations, IP addresses and
  ports, authentication failures, face-recognition outcomes, robot mode and
  gesture-safety events, and file-access records.
  It is NOT encrypted. It will only be written to the location you choose; it
  is never uploaded or shared automatically. Share it only with the security
  team and delete it when the study no longer needs it.
"""


TRANSCRIPT_WARNING = """
  WARNING - CONVERSATION TRANSCRIPT
  This ZIP contains the words the person said and the words the robot said,
  with timestamps. Enrolled names are written as (user), but people can still
  be identified by what they talk about.
  It is NOT encrypted. It will only be written to the location you choose; it
  is never uploaded or shared automatically. Delete it when it is no longer
  needed.
"""


def confirm_security(ask=input, out=print) -> bool:
    out(SECURITY_WARNING)
    return ask("Save the security report? [y/N]: ").strip().lower() in ("y", "yes")


def confirm_transcript(ask=input, out=print) -> bool:
    out(TRANSCRIPT_WARNING)
    return ask("Save the transcript? [y/N]: ").strip().lower() in ("y", "yes")


METRIC_LABELS = {
    "response_ms": "Heard -> robot starts speaking",
    "claude_ms":   "Claude API",
    "tts_ms":      "Edge TTS synthesis",
    "turn_ms":     "Heard -> reply finished",
}
REPEAT_WINDOW_S, REPEAT_COUNT = 300, 3
LONG_INPUT_CHARS = 800

FORBIDDEN = [
    (re.compile(r"sk-ant-"), "API key pattern"),
    (re.compile(r"(?i)\bbearer\s+(?!\[REDACTED)[A-Za-z0-9._~+/=\-]{8,}"), "bearer token"),
    (re.compile(r"(?i)(x-api-key|authorization|cookie|password|token)['\"]?\s*[:=]\s*['\"]?(?!\[REDACTED)[A-Za-z0-9]"),
     "credential value"),
    (re.compile(r"(?i)[A-Z]:\\Users\\"), "Windows home path"),
    (re.compile(r"/(home|Users)/[^/\s]"), "home path"),
    (re.compile(r"(?i)\.(wav|mp3|pcm|ogg|flac|jpg|jpeg|png|npy)\b"), "audio/image file reference"),
]


# ── helpers ──────────────────────────────────────────────────────────────────

def load_events(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass                      # half-written last line after a crash
    return events


def _s(value):
    """Sanitize anything that is not a plain number/bool/None."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize(value)


def _num(value):
    return round(float(value), 1) if isinstance(value, (int, float)) and not isinstance(value, bool) else ""


def _pct(values, p):
    if not values:
        return "-"
    v = sorted(values)
    return f"{v[min(len(v) - 1, round(p / 100 * (len(v) - 1)))]:.0f}"


def _csv(rows, fields) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else _s(r.get(k))) for k in fields})
    return buf.getvalue()


def _ts(e):
    try:
        return datetime.fromisoformat(e["ts"]).timestamp()
    except Exception:
        return 0.0


def _md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join("" if c is None else str(_s(c)) for c in row) + " |" for row in rows]
    return out if rows else out + ["| " + " | ".join(["-"] * len(header)) + " |"]


# ── analysis shared by both audiences ────────────────────────────────────────

def analyse(events):
    a = {"turns": [], "errors": [], "checks": {}, "network": [], "faces": [], "robot": [],
         "gesture": [], "process": [], "files": [], "observations": [], "dropped": 0,
         "speech": [], "commands": [],
         "start": None, "end": None, "exit_type": None,
         "simulated": False, "validation": VALIDATION, "exit_reason": None, "layers": []}
    for e in events:
        kind = e.get("event")
        a["start"] = a["start"] or e.get("ts")
        a["end"] = e.get("ts") or a["end"]
        if kind == "session_start":
            a["simulated"] = bool(e.get("simulated"))
            a["validation"] = _s(e.get("validation") or VALIDATION)
        elif kind == "turn":
            a["turns"].append(e)
        elif kind == "error":
            a["errors"].append(e)
        elif kind == "check":
            a["checks"][_s(e.get("service"))] = e
        elif kind == "network":
            a["network"].append(e)
        elif kind == "face":
            a["faces"].append(e)
        elif kind == "robot_state":
            a["robot"].append(e)
        elif kind == "gesture":
            a["gesture"].append(e)
        elif kind == "process":
            a["process"].append(e)
        elif kind == "file_access":
            a["files"].append(e)
        elif kind == "observation":
            a["observations"].append(e)
        elif kind == "asr_dropped":
            a["dropped"] += 1
        elif kind == "speech":
            a["speech"].append(e)
        elif kind == "robot_command":
            a["commands"].append(e)
        elif kind == "instrumentation":
            a["layers"] = e.get("layers") or []
        elif kind == "session_end":
            a["exit_reason"] = _s(e.get("exit_reason"))
            a["exit_type"] = _s(e.get("exit_type"))
    return a


def errors_by_component(errors, detailed=False):
    groups = {}
    for e in errors:
        key = (e.get("component"), e.get("error_type")) if detailed else (e.get("component"),)
        g = groups.setdefault(key, {"component": e.get("component"), "error_type": e.get("error_type"),
                                    "count": 0, "first_ts": e.get("ts")})
        g["count"] += 1
        g["last_ts"] = e.get("ts")
        g["last_message"] = e.get("message")
        g["last_endpoint"] = e.get("endpoint")
    return list(groups.values())


def indicators(a):
    """Rule-based indicators; each rule is documented in the security README."""
    out = []

    def add(name, severity, items, detail):
        if items:
            out.append({"indicator": name, "severity": severity, "count": len(items),
                        "first_ts": items[0].get("ts"), "last_ts": items[-1].get("ts"), "detail": detail})

    net = a["network"]
    unexpected = [n for n in net if not n.get("expected") and n.get("host") not in (None, "")]
    hosts = sorted({f"{n.get('host')}:{n.get('port')}" for n in unexpected})
    add("unexpected_destination", "high", unexpected, "contacted: " + ", ".join(hosts[:10]))
    auth = [n for n in net if n.get("layer") == "http" and n.get("status") in (401, 403)]
    auth += [e for e in a["errors"] if e.get("status") in (401, 403)
             or e.get("error_type") in ("AuthenticationError", "PermissionDeniedError")]
    auth.sort(key=_ts)
    add("api_auth_failure", "high", auth,
        f"{len(auth)} auth failures" + (" (repeated)" if len(auth) >= 2 else ""))
    plain = [n for n in net if n.get("layer") == "http" and n.get("scheme") in ("http", "ws")
             and not n.get("expected")]
    add("plaintext_to_unexpected_host", "medium", plain, "unencrypted HTTP to a host outside the expected list")
    add("dns_failure", "medium", [n for n in net if n.get("layer") == "dns" and n.get("outcome") == "failed"],
        "hosts: " + ", ".join(sorted({str(n.get("host")) for n in net
                                      if n.get("layer") == "dns" and n.get("outcome") == "failed"})))
    add("connection_failure", "medium", [n for n in net if n.get("layer") == "connect" and n.get("outcome") == "failed"],
        "TCP/UDP connect failures")
    by_comp = defaultdict(list)
    for e in a["errors"]:
        by_comp[e.get("component")].append(e)
    for comp, items in by_comp.items():
        times = [_ts(e) for e in items]
        burst = any(times[i + REPEAT_COUNT - 1] - times[i] <= REPEAT_WINDOW_S
                    for i in range(len(times) - REPEAT_COUNT + 1))
        if burst:
            add(f"repeated_failure:{comp}", "medium", items,
                f">= {REPEAT_COUNT} {comp} errors within {REPEAT_WINDOW_S // 60} min")
    add("robot_mode_change", "high", [r for r in a["robot"] if r.get("change") == "changed"],
        "mode_pr / mode_machine changed while sb01 was running")
    add("gesture_fault", "medium", [g for g in a["gesture"] if g.get("action") == "fault"],
        "gesture client stopped a gesture (see gesture_events.csv)")
    add("gesture_server_unavailable", "low",
        [g for g in a["gesture"] if g.get("action") == "server_health" and not g.get("ok")], "health check failed")
    add("abnormal_input_length", "low",
        [t for t in a["turns"] if isinstance(t.get("user_chars"), int) and t["user_chars"] > LONG_INPUT_CHARS],
        f"speech input longer than {LONG_INPUT_CHARS} characters")
    add("file_access_failure", "low", [f for f in a["files"] if not f.get("ok")], "a file read/write failed")
    if a["dropped"] >= 5:
        out.append({"indicator": "many_late_asr_drops", "severity": "low", "count": a["dropped"],
                    "first_ts": "", "last_ts": "", "detail": "speech arrived while the robot was talking"})
    stops = [p for p in a["process"] if p.get("action") == "stop"]
    crashed = [p for p in stops if p.get("exit_type") in ("crashed", "abnormal")]
    if not stops:
        crashed = [{"ts": a["end"]}]
    add("abnormal_exit", "medium", crashed, a["exit_reason"] or "no shutdown recorded (crash or power loss)")
    add("stopped_by_signal", "low", [p for p in stops if p.get("exit_type") == "signal"],
        a["exit_reason"] or "terminated by a signal (e.g. SIGTERM)")
    return out


# ── builders ─────────────────────────────────────────────────────────────────

def _header(a, session, title):
    label = "SIMULATED SESSION" if a["simulated"] else "REAL G1 SESSION"
    return [f"# {title} - session {session}", "",
            f"> **{label}.** Validation status: **{a['validation']}**.", "",
            f"- Started: {a['start']}", f"- Last event: {a['end']}",
            f"- Ended: {a['exit_reason'] or 'NO shutdown recorded (crash, power loss, or still running)'}",
            f"- Turns: {len(a['turns'])} | Errors: {len(a['errors'])} | "
            f"Late speech dropped while robot was talking: {a['dropped']}", ""]


def _latency(a):
    rows = []
    for key, name in METRIC_LABELS.items():
        vals = [t[key] for t in a["turns"] if isinstance(t.get(key), (int, float))]
        rows.append([name, _pct(vals, 50), _pct(vals, 95), _pct(vals, 100), len(vals)])
    return ["## Response time (ms)", ""] + _md_table(["Metric", "p50", "p95", "max", "n"], rows) + [""]


def _status_rows(a):
    label = {True: "OK", False: "FAIL", None: "N/A (unconfirmed)"}
    return [{"service": k, "status": label.get(c.get("ok"), "FAIL"), "detail": c.get("detail"),
             "checked_at": c.get("ts")} for k, c in a["checks"].items()]


_NET_DETAIL = [
    re.compile(r"\b(?:https?|wss?)://\S+"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"),
    re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?\b", re.I),
    re.compile(r"headers=\{[^}]*\}+"),
]


def _ops(text):
    """Team 1 view: drop hosts, IPs, URLs and header dumps; keep what failed and why."""
    text = _s(text) or ""
    for pattern in _NET_DETAIL:
        text = pattern.sub("[endpoint]", text)
    return " ".join(text.split())


def build_team1(events, session):
    a = analyse(events)
    groups = [{**g, "last_message": _ops(g.get("last_message"))} for g in errors_by_component(a["errors"])]
    md = _header(a, session, "sb01 Team 1 operational report") + _latency(a)
    status = [{**r, "detail": _ops(r["detail"])} for r in _status_rows(a)]
    md += ["## Service status (latest check)", ""]
    md += _md_table(["Service", "Status", "Detail"], [[r["service"], r["status"], r["detail"]] for r in status])
    md += ["", "## Errors by component", ""]
    md += [f"- **{g['component']}**: {g['count']}x (last {g['last_ts']}): {_s(g['last_message'])}" for g in groups] or ["- none"]
    md += ["", "Contains timings, error components and service status only. No API keys, audio, "
           "transcripts, replies, names, camera data or network details.", ""]
    return {
        "summary.md": "\n".join(md),
        "turns.csv": _csv([{**t, **{k: _num(t.get(k)) for k in METRIC_LABELS}} for t in a["turns"]],
                          ["n", "ts", "claude_ms", "tts_ms", "response_ms", "turn_ms", "error"]),
        "errors_by_component.csv": _csv(groups, ["component", "count", "first_ts", "last_ts", "last_message"]),
        "service_status.csv": _csv(status, ["service", "status", "detail", "checked_at"]),
    }


TIMELINE_FIELDS = {
    "session_start": ("validation", "simulated"),
    "process": ("action", "pid", "python", "platform", "interface", "gesture_enabled", "exit_type", "exit_reason"),
    "instrumentation": ("layers",),
    "check": ("service", "ok", "detail"),
    "network": ("layer", "client", "method", "scheme", "protocol", "host", "ip", "port", "url", "status",
                "outcome", "expected", "purpose", "source", "error", "duration_ms"),
    "error": ("component", "error_type", "message", "endpoint", "status"),
    "face": ("outcome", "confidence", "enrolled", "faces_seen", "frames"),
    "robot_state": ("field", "old", "new", "change", "expected"),
    "gesture": ("action", "ok", "endpoint", "error", "detail", "track_error_max", "blocks"),
    "robot_command": ("target", "action", "value", "code", "stream", "bytes", "seconds"),
    "file_access": ("op", "target", "ok", "error", "keys", "count", "chars"),
    "turn": ("n", "claude_ms", "tts_ms", "response_ms", "turn_ms", "error", "user_chars", "reply_chars"),
    "asr_dropped": (),
    "event_cap_reached": ("max_events",),
    "observation": ("name", "seen", "note"),
    "session_end": ("exit_reason", "exit_type", "turns", "errors"),
}


def _clean_record(e):
    allowed = TIMELINE_FIELDS.get(e.get("event"))
    if allowed is None:
        return None                               # unknown event types are never exported
    out = {"ts": e.get("ts"), "event": e.get("event")}
    for k in allowed:
        v = e.get(k)
        if isinstance(v, list):
            v = [_s(x) for x in v]
        elif isinstance(v, dict):
            v = {_s(x): _s(y) for x, y in v.items()}
        else:
            v = _s(v)
        out[k] = v
    return out


def build_security(events, session):
    a = analyse(events)
    inds = indicators(a)
    groups = errors_by_component(a["errors"], detailed=True)
    net = a["network"]
    dests = Counter((n.get("host"), n.get("port"), n.get("expected")) for n in net if n.get("host"))
    md = _header(a, session, "sb01 security-study report")
    md += ["## Indicators", ""]
    md += _md_table(["Severity", "Indicator", "Count", "Detail"],
                    [[i["severity"], i["indicator"], i["count"], i["detail"]] for i in
                     sorted(inds, key=lambda i: ("high", "medium", "low").index(i["severity"]))])
    md += ["", "## Destinations contacted", ""]
    md += _md_table(["Host", "Port", "Expected", "Events"],
                    [[h, p, "yes" if ex else "**NO**", c] for (h, p, ex), c in sorted(dests.items(), key=lambda x: str(x))])
    md += ["", "## Face recognition", ""]
    md += _md_table(["Time", "Outcome", "Confidence", "Enrolled"],
                    [[f.get("ts"), f.get("outcome"), f.get("confidence"), f.get("enrolled")] for f in a["faces"]])
    md += ["", "## Robot mode / gesture", ""]
    md += [f"- Robot state events: {len(a['robot'])} "
           f"({sum(1 for r in a['robot'] if r.get('change') == 'changed')} unexpected changes)",
           f"- Gesture events: {len(a['gesture'])} "
           f"({sum(1 for g in a['gesture'] if g.get('action') == 'fault')} faults)",
           f"- Commands sent to the robot's audio / LED service: {len(a['commands'])} "
           f"(see robot_commands.csv)", ""]
    md += ["## Unconfirmed observations (informational only)", ""]
    md += [f"- {o.get('name')}: {'seen' if o.get('seen') else 'not seen'} - {_s(o.get('note'))}"
           for o in a["observations"]] or ["- none"]
    md += [""]
    md += _latency(a)
    md += ["## Errors by component and type", ""]
    md += _md_table(["Component", "Type", "Count", "Last endpoint", "Last message"],
                    [[g["component"], g["error_type"], g["count"], g["last_endpoint"], g["last_message"]] for g in groups])
    md += ["", f"Instrumented layers this session: {', '.join(a['layers']) or 'none recorded'}.",
           "See README.md for what was collected, excluded, and retention limits.", ""]

    timeline = [r for r in (_clean_record(e) for e in events) if r]
    files = {
        "README.md": security_readme(a, session),
        "summary.md": "\n".join(md),
        "indicators.csv": _csv(inds, ["severity", "indicator", "count", "first_ts", "last_ts", "detail"]),
        "network_contacts.csv": _csv(net, list(TIMELINE_FIELDS["network"]) + ["ts"]),
        "errors.csv": _csv(a["errors"], ["ts"] + list(TIMELINE_FIELDS["error"])),
        "errors_by_component.csv": _csv(groups, ["component", "error_type", "count", "first_ts", "last_ts",
                                                 "last_endpoint", "last_message"]),
        "face_events.csv": _csv(a["faces"], ["ts"] + list(TIMELINE_FIELDS["face"])),
        "robot_state.csv": _csv(a["robot"], ["ts"] + list(TIMELINE_FIELDS["robot_state"])),
        "gesture_events.csv": _csv(a["gesture"], ["ts"] + list(TIMELINE_FIELDS["gesture"])),
        "robot_commands.csv": _csv(a["commands"], ["ts"] + list(TIMELINE_FIELDS["robot_command"])),
        "process_events.csv": _csv(a["process"], ["ts"] + list(TIMELINE_FIELDS["process"])),
        "file_access.csv": _csv([{**f, "keys": ";".join(f.get("keys") or [])} for f in a["files"]],
                                ["ts"] + list(TIMELINE_FIELDS["file_access"])),
        "turns.csv": _csv([{**t, **{k: _num(t.get(k)) for k in METRIC_LABELS}} for t in a["turns"]],
                          ["ts"] + list(TIMELINE_FIELDS["turn"])),
        "service_status.csv": _csv(_status_rows(a), ["service", "status", "detail", "checked_at"]),
        "timeline.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in timeline),
    }
    return files


def security_readme(a, session):
    real = "a SIMULATED run (no physical robot)" if a["simulated"] else "a run on the REAL G1 robot"
    return f"""# sb01 security-study telemetry - session {session}

**This package comes from {real}.**
**Validation status of the monitoring code: {a['validation']}.**
Instrumented layers: {', '.join(a['layers']) or 'none recorded'}.

## What was collected
- Exact timestamps (local time, millisecond precision) for every event.
- Outbound contacts made by the sb01 Python process: DNS lookups (host, resolved
  IPs, failures), TCP/UDP connects (host, IP, port, protocol, outcome), and HTTP
  requests via urllib / httpx / httpx2 / aiohttp (method, scheme, host, port, redacted
  URL path, response status, duration). Each is marked `expected` or not
  against a fixed list (CSUSB, wttr.in, api.anthropic.com,
  speech.platform.bing.com, the configured gesture server). `source` is
  `monitor_check` for the monitor's own startup probes, otherwise `program`.
- Errors: component, error type, redacted message, endpoint, HTTP status.
- Startup service checks (API key present - never its value -, Claude/TTS
  reachability, gesture server health, and the robot audio service's reply
  code to SetVolume over DDS; "N/A" when that reply cannot be interpreted).
- Informational observation (does not affect health): whether a play_state
  message arrived during the greeting. Whether the G1 sends play_state for
  streamed audio is unconfirmed, so its absence is not treated as a failure.
- Face-recognition outcome at startup: recognized / unrecognized / no_face /
  camera_failed / no_enrolled / unavailable, a coarse confidence category
  (strong / moderate / none) and the number of enrolled faces.
- Robot mode_pr / mode_machine as already received by the gesture client
  (read-only), and any change during the session; the robot FSM id, read once
  at startup when gestures are enabled.
- Commands the program sent to the robot's audio and LED service
  (`robot_commands.csv`): SetVolume, each LED colour change, and one PlayStream
  record per spoken reply (stream id, bytes, seconds), each with the robot's
  reply code where the SDK returns one. Arm commands are sent 30 times a second
  and are summarized per reply in `gesture_events.csv`, not listed one by one.
- Gesture events: server health, started / not started, faults raised by the
  gesture client's FSM/state guard, maximum arm tracking error per utterance.
- File/config access: which env file and which key NAMES were loaded, preload
  files read, number of enrolled face files, memory profile read/write
  (person shown as `[person]`).
- Process start/stop, exit type and reason; per-turn timings and the LENGTH
  (characters) of each speech input and reply.
- Indicators computed by fixed rules: unexpected destination (high), API auth
  failure (high), robot mode change (high), plaintext HTTP to an unexpected
  host, DNS/connection failures, >= {REPEAT_COUNT} errors of one component within
  {REPEAT_WINDOW_S // 60} min, gesture fault, abnormal exit (medium), gesture
  server unavailable, input over {LONG_INPUT_CHARS} chars, file access failure,
  >= 5 late speech drops (low).

## What was intentionally excluded
API keys, bearer tokens, cookies, passwords, authorization headers, every URL
query value, the weather-lookup location (it comes from student speech),
audio, camera images, face encodings or distances, the words spoken by the
person and by the robot (those are saved only in the separate transcript
report), person names (enrolled names and memory file names are redacted to
[person] wherever they appear), media file names, detected emotion, usernames
and home-directory paths.

## Not observable (not invented)
- Robot DDS traffic (CycloneDDS's C library, below Python) and anything the
  robot itself sends; the program makes no Unitree web contacts at runtime.
- Robot FSM id after startup (it is read once at startup; later it is only
  checked inside the gesture client, which reports a fault if it is wrong).
- Gesture tracking, DDS-write and sidecar-stream faults: the gesture client
  prints these but does not store them, and gesture_client.py is not modified.
- Gesture-server availability between startup and shutdown (no periodic
  probing: the server handles one request at a time).
- Recognitions in the live camera window; a pseudonymous person id.
- Network activity of other processes (e.g. the gesture server itself).

## Retention limitations
- During the session data lived only in a private temporary folder
  (`<system temp>/sb01-monitor-<uid>/<session>/`, owner-only), deleted once
  this package was saved or the operator declined to save.
- If sb01 crashed, the temporary folder was kept and offered for saving at the
  next startup, then deleted. Folders older than {7} days are removed.
- Data is lost on reboot or /tmp cleanup before it is saved; on power loss the
  last line may be incomplete.
- This ZIP is NOT encrypted. Keep it only as long as the study needs it, share
  it only with the security team, and delete it afterwards. It may describe
  identifiable students' interactions (times, recognition outcomes); handle it
  under CSUSB data policy / FERPA as your instructor directs.
"""


def build_transcript(events, session):
    """What the person said and what the robot said, in order, and nothing else."""
    a = analyse(events)
    speakers = {"user": "User", "robot": "Yotie"}
    rows = []
    for e in a["speech"]:
        rows.append({"ts": _s(e.get("ts")) or "", "speaker": speakers.get(e.get("speaker"), "Unknown"),
                     "language": _s(e.get("language")) or "", "status": _s(e.get("status")) or "",
                     "text": sanitize_speech(e.get("text") or "")})
    md = _header(a, session, "Yotie conversation transcript")
    md += ["## Transcript", ""]
    for r in rows:
        who = r["speaker"] + (f" ({r['status']})" if r["status"] else "")
        md.append(f"- `{r['ts']}` **{who}:** {r['text']}")
    md += [] if rows else ["- nothing was said in this session"]
    md += ["", "'User (not answered)' is speech that arrived while Yotie was still talking. "
           "Enrolled names are written as (user). No audio, API keys, passwords or tokens. "
           "Speech-recognition text is shown as the robot received it, so it may contain mistakes.", ""]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["ts", "speaker", "language", "status", "text"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return {"transcript.md": "\n".join(md), "transcript.csv": buf.getvalue()}


def build(events, session, audience):
    register_person_terms(person_terms_from_memory())     # redact enrolled names again at export
    if audience == "team1":
        return build_team1(events, session)
    if audience == "security":
        return build_security(events, session)
    if audience == "transcript":
        return build_transcript(events, session)
    raise ValueError(f"unknown audience {audience!r}")


# ── safety scan + writing ────────────────────────────────────────────────────

def scan(files) -> list[str]:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    problems = []
    for name, text in files.items():
        if len(key) >= 8 and key in text:
            problems.append(f"{name}: contains the API key value")
        for pattern, what in FORBIDDEN:
            if pattern.search(text):
                problems.append(f"{name}: {what}")
    return problems


def write_zip(files, dest, audience):
    dest_dir = os.path.dirname(dest) or "."
    tmp = os.path.join(dest_dir, f".{os.path.basename(dest)}.partial")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    if audience in SENSITIVE:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, dest)


def export(events_path, session, audience, dest, out=print) -> bool:
    files = build(load_events(events_path), session, audience)
    problems = scan(files)
    if problems:
        out("Export REFUSED - sensitive content detected; nothing was written:")
        for p in problems:
            out(f"  - {p}")
        return False
    try:
        write_zip(files, dest, audience)
    except OSError as exc:
        partial = os.path.join(os.path.dirname(dest) or ".", f".{os.path.basename(dest)}.partial")
        try:
            os.remove(partial)
        except OSError:
            pass
        out(f"Could not write {dest}: {sanitize(f'{type(exc).__name__}: {exc}')}. Nothing was saved.")
        return False
    out(f"Saved {AUDIENCES[audience]}: {dest}")
    return True


# ── choosing where to save ───────────────────────────────────────────────────

def default_name(audience, session):
    return f"sb01_{audience}_{session}.zip"


def _inside_repo(path):
    try:
        return os.path.commonpath([os.path.abspath(path), REPO_ROOT]) == REPO_ROOT
    except ValueError:
        return False


def _zenity(default_path, audience):
    if not shutil.which("zenity") or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "unavailable", None
    try:
        r = subprocess.run(
            ["zenity", "--file-selection", "--save", "--confirm-overwrite",
             f"--title=Save sb01 {AUDIENCES[audience]}"
             + (" (SENSITIVE, not encrypted)" if audience in SENSITIVE else ""),
             f"--filename={default_path}",
             "--file-filter=ZIP files | *.zip"],
            capture_output=True, text=True, timeout=600)
    except Exception:
        return "unavailable", None
    if r.returncode == 0 and r.stdout.strip():
        return "chosen", r.stdout.strip()
    return "cancelled", None


def choose_destination(audience, session, ask=input, out=print):
    """Returns an absolute path chosen by the operator, or None to discard."""
    default_path = os.path.join(os.path.expanduser("~"), default_name(audience, session))
    status, path = _zenity(default_path, audience)
    confirmed_overwrite = status == "chosen"          # zenity already asked
    if status == "cancelled":
        out("Save dialog cancelled.")
    if status != "chosen":
        if status == "unavailable":
            out("No graphical Save As dialog available (zenity not installed or no display).")
        for _ in range(3):
            answer = ask(f"Full path to save the ZIP [Enter = {default_path}, q = discard]: ").strip()
            if answer.lower() in ("q", "quit", "n", "no"):
                return None
            path = os.path.expanduser(answer or default_path)
            if os.path.isdir(path):
                path = os.path.join(path, default_name(audience, session))
            if not os.path.isdir(os.path.dirname(os.path.abspath(path))):
                out(f"Folder does not exist: {os.path.dirname(os.path.abspath(path))}")
                continue
            break
        else:
            return None
        confirmed_overwrite = False
    path = os.path.abspath(path)
    if not path.lower().endswith(".zip"):
        path += ".zip"
        confirmed_overwrite = False
    if _inside_repo(path):
        if ask("That location is inside the sb01 project folder and could be committed to git. "
               "Save there anyway? [y/N]: ").strip().lower() not in ("y", "yes"):
            return choose_destination(audience, session, ask, out) if status != "chosen" else None
    if os.path.exists(path) and not confirmed_overwrite:
        if ask(f"{path} exists. Overwrite? [y/N]: ").strip().lower() not in ("y", "yes"):
            return None
    return path


def offer_save(session_dir, session, ask=input, out=print, reason="shutdown") -> str | None:
    """The operator decides whether, which, and where. Returns the saved path or None.
    Temporary data is deleted afterwards unless the export was refused or failed."""
    events = os.path.join(session_dir, "events.jsonl")
    saved, keep = None, False
    try:
        prompt = ("Save monitoring report? [y/N]: " if reason == "shutdown" else
                  f"A previous sb01 session ({session}) ended without saving its monitoring data. "
                  "Save its report now? [y/N]: ")
        if ask(prompt).strip().lower() in ("y", "yes"):
            audience = None
            for _ in range(3):
                pick = ask("Which report? [1] Team 1 operational  [2] Security study  "
                           "[3] Conversation transcript: ").strip().lower()
                audience = {"1": "team1", "team1": "team1", "team 1": "team1",
                            "2": "security", "security": "security",
                            "3": "transcript", "transcript": "transcript"}.get(pick)
                if audience:
                    break
                out("Please type 1, 2 or 3.")
            if audience == "security" and not confirm_security(ask, out):
                out("Security report not saved.")
                audience = None
            if audience == "transcript" and not confirm_transcript(ask, out):
                out("Transcript not saved.")
                audience = None
            if audience:
                dest = choose_destination(audience, session, ask, out)
                if dest:
                    if export(events, session, audience, dest, out):
                        saved = dest
                    else:
                        keep = True
                else:
                    out("Not saved.")
    except (KeyboardInterrupt, EOFError):
        out("\nNot saved.")
    except Exception as exc:                     # never let reporting crash shutdown
        out(f"Report could not be created: {sanitize(f'{type(exc).__name__}: {exc}')}")
        keep = True
    if keep:
        out(f"Temporary monitoring data kept for retry: python3 scripts/monitor_export.py --session {session}")
    else:
        discard_session(session_dir)
        out("Temporary monitoring data deleted." if not saved else "Temporary monitoring data deleted.")
    return saved
