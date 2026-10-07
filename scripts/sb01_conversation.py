#!/usr/bin/env python3
"""
sb01_conversation.py  -  LLM-powered conversation loop for the G1 robot "Yotie"

Features:
  - Face recognition at startup → loads per-person memory profile
  - Persistent memory: user profile + session summaries saved across runs
  - Emotion-aware: LED reacts to detected voice emotion; Claude gets emotion hint
  - Edge TTS with automatic language detection (EN / ZH / ES)
  - Optional arm gestures while talking (G1 built-in arm actions, picked by Claude)
  - --demo mode: visitor-facing persona instead of the classroom assistant

Network layout:
  eno0 (Ethernet, 192.168.123.x)  -- DDS: ASR messages in, TTS/LED commands out
  wlp0s20f3 (WiFi)                -- Claude API + web fetches over the internet

Usage:
  python3 scripts/sb01_conversation.py [network_interface] [--demo] [--gestures | --no-gestures]
"""

import sys
import os

# Load .env / env file from the project root so ANTHROPIC_API_KEY is available
# without needing to `source env` manually before running the script.
_env_file = os.path.join(os.path.dirname(__file__), "..", "env")
if os.path.isfile(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ[_k.strip()] = _v.strip()
import io
import re
import argparse
import datetime
import json
import time
import queue
import random
import asyncio
import threading
import urllib.request
import urllib.parse
from html.parser import HTMLParser

import anthropic
import edge_tts
from pydub import AudioSegment

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient, action_map

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from teleop.face_id import FaceIdentifier
from teleop.memory_manager import MemoryManager
from teleop.knowledge_base import KnowledgeBase

# ── config ───────────────────────────────────────────────────────────────────
NETWORK_INTERFACE = "eno0"
ROBOT_NAME        = "Yotie"
ASR_TOPIC         = "rt/audio_msg"
VOICE_EN = "en-US-JennyNeural"
VOICE_ZH = "zh-CN-XiaoxiaoNeural"
VOICE_ES = "es-MX-DaliaNeural"

PCM_SAMPLE_RATE   = 16000
PCM_CHUNK_BYTES   = 96000
MAX_HISTORY_TURNS = 10

# Live conversation turns: Sonnet 5.5 at low effort had the fastest first word in a
# 2026-10-07 benchmark (0.5s vs 1.2s for Opus 4.8 + web search). The end-of-session
# summary isn't latency-sensitive and stays on Opus.
LIVE_MODEL    = "claude-sonnet-5-5"
LIVE_EFFORT   = "low"
SUMMARY_MODEL = "claude-opus-4-8"
MIN_SENTENCE_CHARS = 25

COURSE_START = datetime.date(2026, 8, 24)   # Monday of IST 5930 Week 1 (Fall 2026)
COURSE_WEEKS = 15   # shorter pieces ("U.S.", "Hi!") are merged into the next sentence

# People whose conversations are never saved and never prompted for consent
# (e.g. the instructor/owner running the robot, as opposed to students).
NO_MEMORY_NAMES = {"yutong"}

PRELOAD_URLS = [
    "https://www.csusb.edu/",
]
PRELOAD_FILES = [
    os.path.join(os.path.dirname(__file__), "../unitree_sdk2_python/README.md"),
    os.path.join(os.path.dirname(__file__), "../unitree_sdk2_python/example/g1/readme.md"),
]

WEATHER_KEYWORDS = {
    "weather", "temperature", "temp", "rain", "raining", "sunny", "sunshine",
    "cold", "hot", "warm", "forecast", "humid", "humidity", "wind", "windy",
    "snow", "snowing", "cloudy", "overcast",
}

# Built-in G1 arm actions sb01 may use while talking — a subset of the SDK's
# action_map (hug / kiss / x-ray / hands up left out on purpose for visitors).
# Value = seconds to hold the pose before "release arm"; None = the action
# returns the arm on its own.
GESTURES = {
    "high wave":  None,
    "face wave":  None,
    "clap":       None,
    "shake hand": 4.0,
    "high five":  3.0,
    "heart":      2.0,
}
GESTURE_SETTLE_S = 3.0   # min gap after a self-releasing action before the next one

# emotion → (LED color while thinking, hint for Claude)
EMOTION_MAP = {
    "happy":   ((128, 128, 0),  "The user sounds happy and upbeat."),
    "sad":     ((128, 0, 128),  "The user sounds sad. Be warm and supportive."),
    "angry":   ((128, 0, 0),    "The user sounds frustrated. Be calm and understanding."),
    "neutral": ((0, 0, 0),      ""),
}

BASE_SYSTEM_PROMPT = (
    f"You are {ROBOT_NAME}, a friendly and curious humanoid robot built by Unitree Robotics, "
    "trained by Yutong at California State University, San Bernardino (CSUSB). "
    "You are talking to people face-to-face in real life. "
    "Keep every reply to 1-3 short sentences — you will be speaking aloud. "
    "Keep each sentence under about 20 words; long sentences delay when you start talking. "
    "Do not use markdown, bullet points, or special characters. "
    "When given reference information in square brackets, use it naturally to answer questions. "
    "You have access to the course's lecture slides, weekly course pages, lab instructions, "
    "lecture notes, assigned readings, and Yutong's CV — "
    "when a 'Course material reference' is given, answer from it directly and speak the "
    "content naturally without reading out filenames or page numbers. "
    "The weekly course pages and lab pages show what the class actually did; if they disagree "
    "with the original semester plan, trust the weekly pages. Each message starts with today's "
    "date and course week — use it for questions like 'what's due this week'. Never guess a "
    "due date, time, or room: if the reference doesn't say, tell the student to check Canvas or "
    "ask Yutong. "
    "\n\n"
    "You are primarily here to help students in Yutong's embodied AI and robotics seminar — "
    "topics like embodied AI, robotics, the G1 robot itself, the course's lectures and readings, "
    "and Yutong's research and background are all in scope. If a student asks something clearly "
    "unrelated to the class (e.g. general chit-chat unrelated to class, unrelated personal advice, "
    "homework for other classes, or trivia), still give a brief, helpful answer, then add a short, "
    "friendly reminder that you're best used for course questions — all within your normal 1-3 "
    "sentence limit. Simple greetings and pleasantries don't need a reminder. "
    "Answer order for course-related questions: (1) if a 'Course material reference' is given "
    "and it actually answers the question, use it and don't web search; (2) your own knowledge, "
    "if you're confident; (3) web search, only as a last resort — when there's no reference, or "
    "the given reference doesn't actually cover what was asked (e.g. it's topically related but "
    "doesn't have the specific fact, like recent news or this year's results), and you're not "
    "confident from your own knowledge either. Never use web search for questions outside the "
    "class scope."
)

# --demo: talking to lab visitors (e.g. officials, partners) rather than students
DEMO_SYSTEM_PROMPT = (
    f"You are {ROBOT_NAME}, a friendly humanoid robot (a Unitree G1) in Yutong's embodied AI and "
    "robotics lab at California State University, San Bernardino (CSUSB). "
    "Right now you are being shown to visitors — guests from outside the university, possibly "
    "including military or government officials. Be warm, confident, and professional. "
    "You are talking to people face-to-face in real life. "
    "Keep every reply to 1-3 short sentences — you will be speaking aloud. "
    "Keep each sentence under about 20 words; long sentences delay when you start talking. "
    "Do not use markdown, bullet points, or special characters. "
    "When given reference information in square brackets, use it naturally to answer questions, "
    "without reading out filenames or page numbers. "
    "Answer any question the visitor asks; do not redirect them to course topics. "
    "\n\n"
    "If asked what the lab works on, you can describe: "
    "(1) this conversation system — speech recognition on the robot, a large language model "
    "(Anthropic's Claude) for reasoning, cloud text-to-speech for your voice, face recognition, "
    "and per-person memory that is only kept if the person agrees. Face recognition runs on the "
    "lab laptop with a camera attached to it (not your head cameras) and only matches people "
    "who were enrolled; unrecognized visitors are not stored; "
    "(2) motion-capture teleoperation — a Qualisys motion-capture system tracks a human operator "
    "and their motion is retargeted onto your joints, which is still being developed and tested; "
    "(3) text-to-motion — generating whole-body motions from a text description and validating "
    "them in physics simulation before they are allowed on the real robot; "
    "(4) a student security lab that studies what data leaves the robot over the network. "
    "Describe ongoing work as ongoing — never claim capabilities the lab has not demonstrated. "
    "\n\n"
    "If asked where data goes, be candid: the recognized text of what people say is sent over the "
    "internet to Anthropic's Claude API, and your spoken replies are generated by Microsoft's cloud "
    "text-to-speech; whether the robot's built-in speech recognition runs fully on the robot is "
    "something the lab's security group is verifying. "
    "If asked about defense or naval uses, you may talk about general possibilities such as remote "
    "operation in hazardous spaces, training, or human-robot teaming, framed as research "
    "directions, not existing capabilities. Do not speculate about weapons or targeting. "
    "You may describe the course and what student teams work on, but never name individual "
    "students. "
    "Use web search only if you truly need a current fact."
)

# appended to either prompt when arm gestures are enabled
GESTURE_PROMPT = (
    "\n\nYou can move your arms. When a gesture fits naturally, put exactly one tag like "
    "[GESTURE: high wave] at the start of your reply; it is removed before you speak. "
    f"Available gestures: {', '.join(GESTURES)}. "
    "Use them sparingly — only when the person actually says hello or goodbye (high wave or face "
    "wave; never just because the conversation is friendly), when someone offers to "
    "shake hands or says nice to meet you (shake hand), celebrating (clap or high five), or "
    "warmth and thanks (heart). Most replies should have no gesture."
)

# ── web utilities ─────────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header", "aside"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header", "aside"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def fetch_url(url: str, max_chars: int = 3000) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        parser = _TextExtractor()
        parser.feed(html)
        return " ".join(parser.parts)[:max_chars]
    except Exception as exc:
        return f"[Could not fetch {url}: {exc}]"


def fetch_weather(location: str = "San Bernardino, CA") -> str:
    try:
        loc = urllib.parse.quote(location)
        url = f"https://wttr.in/{loc}?format=3"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception as exc:
        return f"[Weather unavailable: {exc}]"


def extract_location(text: str) -> str:
    lower = text.lower()
    for marker in (" in ", " for ", " at "):
        idx = lower.find(marker)
        if idx != -1:
            candidate = text[idx + len(marker):].strip().rstrip("?.")
            if candidate:
                return candidate
    return "San Bernardino, CA"


def load_context() -> str:
    parts = []
    for url in PRELOAD_URLS:
        print(f"[{ROBOT_NAME}] fetching {url}...")
        parts.append(f"--- {url} ---\n{fetch_url(url)}")
    for path in PRELOAD_FILES:
        path = os.path.abspath(path)
        try:
            with open(path) as f:
                parts.append(f"--- {path} ---\n{f.read(3000)}")
        except Exception as exc:
            parts.append(f"--- {path} ---\n[Could not read: {exc}]")
    return "\n\n".join(parts)


# ── helpers ───────────────────────────────────────────────────────────────────

def course_week(day: datetime.date) -> int:
    """1-based IST 5930 week for a date (≤0 before the semester, >COURSE_WEEKS after)."""
    return (day - COURSE_START).days // 7 + 1


def resolve_relative_weeks(text: str, week: int) -> str:
    """Add 'weekN' terms for 'this/next/last week' so the course-notes search finds that week."""
    for phrase, offset in (("this week", 0), ("next week", 1), ("last week", -1)):
        if phrase in text.lower() and 1 <= week + offset <= COURSE_WEEKS:
            text += f" week{week + offset}"
    return text


def _parse_emotion(raw: str) -> str:
    """'<|HAPPY|>' → 'happy',  '<|EMO_UNKNOWN|>' → ''"""
    clean = raw.replace("<|", "").replace("|>", "").replace("EMO_", "").lower()
    return "" if clean in ("unknown", "") else clean


_GESTURE_TAG = re.compile(r"\[\s*gesture\s*:\s*([^\]]*)\]", re.IGNORECASE)


def split_gesture(reply: str) -> tuple[str, str | None]:
    """'[GESTURE: high wave] Hi there!' → ('Hi there!', 'high wave'). Unknown names → None."""
    match = _GESTURE_TAG.search(reply)
    gesture = match.group(1).strip().lower() if match else None
    spoken = re.sub(r"\s{2,}", " ", _GESTURE_TAG.sub("", reply)).strip()
    return spoken, (gesture if gesture in GESTURES else None)


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")


def pop_sentences(buf: str) -> tuple[list[str], str]:
    """Split complete sentences off the front of a streaming buffer → (sentences, remainder)."""
    parts = _SENTENCE_END.split(buf)
    remainder = parts.pop()
    sentences, pending = [], ""
    for part in parts:
        pending = f"{pending} {part}".strip() if pending else part.strip()
        if len(pending) >= MIN_SENTENCE_CHARS:
            sentences.append(pending)
            pending = ""
    if pending:
        remainder = f"{pending} {remainder}"
    return sentences, remainder


class GestureController:
    """Plays G1 built-in arm actions on a background thread so they run alongside speech."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.client: G1ArmActionClient | None = None
        self._lock = threading.Lock()

    def init(self):
        if not self.enabled:
            return
        try:
            self.client = G1ArmActionClient()
            self.client.SetTimeout(10.0)
            self.client.Init()
            print(f"[{ROBOT_NAME}] arm gestures enabled")
        except Exception as exc:
            print(f"[{ROBOT_NAME}] arm action client failed ({exc}); gestures disabled")
            self.enabled = False

    def play(self, name: str | None):
        if not self.enabled or name not in GESTURES:
            return
        if self._lock.locked():
            print(f"[gesture] busy, skipping {name!r}")
            return
        threading.Thread(target=self._run, args=(name,), daemon=True).start()

    def _run(self, name: str):
        with self._lock:
            if not self.enabled:   # arm stop pressed while this was queued
                return
            print(f"[gesture] {name}")
            try:
                code = self.client.ExecuteAction(action_map[name])
                if code != 0:
                    print(f"[gesture] {name!r} failed, code {code} (is the robot in a standing/locomotion mode?)")
                    return
                hold = GESTURES[name]
                if hold is None:
                    time.sleep(GESTURE_SETTLE_S)
                else:
                    time.sleep(hold)
                    self.client.ExecuteAction(action_map["release arm"])
            except Exception as exc:
                print(f"[gesture] {name!r} error: {exc}")

    def stop(self):
        """Arm stop: turn gestures off for the rest of the session and release the arms
        right away, without waiting for a running gesture to finish."""
        if self.client is None:
            return
        self.enabled = False
        print("[gesture] ARM STOP — gestures disabled for this session, releasing arms")
        try:
            self.client.ExecuteAction(action_map["release arm"])
        except Exception as exc:
            print(f"[gesture] release error: {exc}")


# ── main class ────────────────────────────────────────────────────────────────

class SB01ConversationLoop:
    def __init__(self, interface: str, web_context: str, demo: bool = False, gestures: bool = False):
        self.interface   = interface
        self.web_context = web_context
        self.demo        = demo
        self.audio       = AudioClient()
        self.gestures    = GestureController(gestures)
        self.asr_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.speaking    = threading.Event()
        self.tts_done    = threading.Event()
        self.history: list[dict] = []
        self.claude      = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._pending_text    = ""
        self._pending_emotion = ""
        self._asr_timer: threading.Timer | None = None

        self.memory  = MemoryManager()
        self.face_id = FaceIdentifier()
        self.kb      = KnowledgeBase()
        self.person_name: str | None = None
        self.memory_consent: bool | None = None

    # ── startup ──────────────────────────────────────────────────────────────

    def _memory_excluded(self) -> bool:
        """True for people who should never be asked about or have conversations saved."""
        return bool(self.person_name) and self.person_name.lower() in NO_MEMORY_NAMES

    def _identify_person(self) -> str | None:
        print(f"[{ROBOT_NAME}] scanning for known faces...")
        name = self.face_id.identify()
        if name:
            print(f"[{ROBOT_NAME}] recognized: {name}")
        else:
            print(f"[{ROBOT_NAME}] face not recognized")
        return name

    def _build_system_prompt(self) -> str:
        prompt = DEMO_SYSTEM_PROMPT if self.demo else BASE_SYSTEM_PROMPT
        if self.gestures.enabled:
            prompt += GESTURE_PROMPT
        if self.person_name:
            ctx = self.memory.build_context(self.person_name)
            prompt += f"\n\nWhat you know about this person:\n{ctx}"
        if self.web_context:
            prompt += f"\n\nReference information:\n{self.web_context}"
        return prompt

    def setup(self):
        self.audio.SetTimeout(10.0)
        self.audio.Init()
        self.audio.SetVolume(100)
        self.gestures.init()

        self.person_name = self._identify_person()
        self.face_id.start_live_window()

        self.asr_sub = ChannelSubscriber(ASR_TOPIC, String_)
        self.asr_sub.Init(self._asr_callback, 10)
        print(f"[{ROBOT_NAME}] subscribed to {ASR_TOPIC}")

        if self.person_name and not self._memory_excluded():
            self.memory_consent = self.memory.get_consent(self.person_name)

    # ── consent ──────────────────────────────────────────────────────────────

    def _ask_memory_consent(self) -> bool:
        self._speak(
            f"Hey {self.person_name}, is it okay if I remember what we talk about today, "
            "so I can recall it next time we meet? You can say yes or no."
        )
        self.audio.LedControl(0, 128, 0)
        try:
            text, _ = self.asr_queue.get(timeout=10.0)
        except queue.Empty:
            text = ""
        print(f"[{ROBOT_NAME}] consent reply: {text!r}")

        lower = text.lower()
        if any(w in lower for w in ("no", "nope", "don't", "do not", "nah", "not okay", "not ok")):
            consent = False
        elif any(w in lower for w in ("yes", "yeah", "yep", "sure", "okay", "ok", "go ahead", "fine", "alright")):
            consent = True
        else:
            consent = False  # unclear/no answer → default to not saving

        self._speak(
            "Got it, I'll remember our chats." if consent
            else "Understood, I won't save our conversations."
        )
        self.audio.LedControl(0, 128, 0)
        return consent

    # ── DDS callback ─────────────────────────────────────────────────────────

    def _asr_callback(self, msg: String_):
        try:
            data = json.loads(msg.data)
        except (json.JSONDecodeError, AttributeError):
            return

        if "play_state" in data:
            if data["play_state"] == 0:
                self.tts_done.set()
            return

        if self.speaking.is_set():
            return

        text = data.get("text", "").strip()
        if not text or len(text) < 3:
            return

        emotion = _parse_emotion(data.get("emotion", ""))

        if data.get("is_final", False):
            if self._asr_timer:
                self._asr_timer.cancel()
            self._pending_text = ""
            self.asr_queue.put((text, emotion))
        else:
            self._pending_text    = text
            self._pending_emotion = emotion
            if self._asr_timer:
                self._asr_timer.cancel()
            self._asr_timer = threading.Timer(1.5, self._flush_pending)
            self._asr_timer.daemon = True
            self._asr_timer.start()

    def _flush_pending(self):
        text, emotion = self._pending_text, self._pending_emotion
        self._pending_text = self._pending_emotion = ""
        if text and not self.speaking.is_set():
            self.asr_queue.put((text, emotion))

    # ── TTS ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _pick_voice(text: str) -> str:
        if any('一' <= c <= '鿿' for c in text):
            return VOICE_ZH
        if any(c in {'á','é','í','ó','ú','ñ','¿','¡','ü'} for c in text.lower()):
            return VOICE_ES
        return VOICE_EN

    async def _text_to_pcm(self, text: str) -> bytes:
        communicate = edge_tts.Communicate(text, self._pick_voice(text))
        mp3_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_chunks.append(chunk["data"])
        mp3_data = b"".join(mp3_chunks)
        audio = AudioSegment.from_mp3(io.BytesIO(mp3_data))
        audio = audio.set_frame_rate(PCM_SAMPLE_RATE).set_channels(1).set_sample_width(2)
        return audio.raw_data

    def _speak(self, text: str):
        self._speak_sentences([text])

    def _speak_sentences(self, sentences):
        """Speak an iterable of sentences (e.g. a streaming Claude reply), synthesizing the
        next sentence while the current one plays. ASR is ignored for the whole reply."""
        self.speaking.set()
        pcm_queue: queue.Queue[bytes | None] = queue.Queue()

        def synthesize():
            try:
                for text in sentences:
                    text = re.sub(r'\bCSUSB\b', 'Cal State San Bernardino', text, flags=re.IGNORECASE)
                    if text.strip():
                        pcm_queue.put(asyncio.run(self._text_to_pcm(text)))
            except Exception as exc:
                print(f"[{ROBOT_NAME}] TTS failed ({exc}); skipping speech")
            finally:
                pcm_queue.put(None)

        threading.Thread(target=synthesize, daemon=True).start()
        try:
            first = True
            while (pcm := pcm_queue.get()) is not None:
                if first:
                    self.audio.LedControl(0, 0, 128)
                    first = False
                self._play_pcm(pcm)
        except Exception as exc:
            print(f"[{ROBOT_NAME}] playback failed ({exc})")
        finally:
            self.audio.LedControl(0, 0, 0)
            self.speaking.clear()

    def _play_pcm(self, pcm: bytes):
        """Stream one clip to the robot speaker and wait until it finishes playing."""
        self.tts_done.clear()
        stream_id = str(int(time.time() * 1000))
        total = len(pcm)
        for offset in range(0, total, PCM_CHUNK_BYTES):
            chunk = pcm[offset:offset + PCM_CHUNK_BYTES]
            ret_code, _ = self.audio.PlayStream(ROBOT_NAME, stream_id, list(chunk))
            if ret_code != 0:
                print(f"[{ROBOT_NAME}] PlayStream error, return code: {ret_code}")
                break
            if offset + PCM_CHUNK_BYTES < total:
                time.sleep(1.0)
        self.tts_done.wait(timeout=total / (PCM_SAMPLE_RATE * 2) + 3.0)
        time.sleep(0.4)

    # ── Claude ───────────────────────────────────────────────────────────────

    def _ask_claude(self, user_text: str, emotion: str):
        """Stream Claude's reply, yielding it sentence by sentence as it arrives.
        Gesture tags are acted on and stripped; errors become a spoken apology."""
        context_parts = []

        # weather injection
        if set(user_text.lower().split()) & WEATHER_KEYWORDS:
            location = extract_location(user_text)
            weather = fetch_weather(location)
            print(f"[weather] {weather}")
            context_parts.append(f"[Current weather: {weather}]")

        # emotion hint
        _, emotion_hint = EMOTION_MAP.get(emotion, ((0, 0, 0), ""))
        if emotion_hint:
            context_parts.append(f"[{emotion_hint}]")

        # today's date + course week, so "what's due this week?" can be answered
        today = datetime.date.today()
        week = course_week(today)
        week_note = f", IST 5930 week {week}" if 1 <= week <= COURSE_WEEKS else ""
        context_parts.append(f"[Today is {today:%A, %B} {today.day}, {today.year}{week_note}.]")

        # course knowledge base (lecture slides, course notes, readings, instructor CV) — checked
        # first; web search is only a fallback when this comes up empty (see system prompt).
        # Course notes name students, so demo mode (outside visitors) searches PDFs only.
        kb_hits = self.kb.retrieve(resolve_relative_weeks(user_text, week), include_notes=not self.demo)
        if kb_hits:
            context_parts.append(f"[Course material reference:\n{kb_hits}]")
        elif not self.demo:
            context_parts.append(
                "[No local course material matched this question. If it's an in-scope "
                "class question you can't answer from what you already know, you may web search.]"
            )

        augmented = " ".join(context_parts) + " " + user_text if context_parts else user_text
        self.history.append({"role": "user", "content": augmented})

        if len(self.history) > MAX_HISTORY_TURNS * 2:
            self.history = self.history[-(MAX_HISTORY_TURNS * 2):]

        request = dict(
            model=LIVE_MODEL,
            max_tokens=1024,
            system=[{"type": "text", "text": self._build_system_prompt(),
                     "cache_control": {"type": "ephemeral"}}],
            messages=self.history,
            output_config={"effort": LIVE_EFFORT},
            # on a safety-classifier decline, retry server-side on Anthropic's recommended model
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if not self.demo:   # web search adds seconds; demo answers come from the prompt
            request["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}]

        reply, buf, spoken_any = "", "", False

        def emit(sentence: str):
            nonlocal spoken_any
            spoken, gesture = split_gesture(sentence)
            self.gestures.play(gesture)
            if spoken:
                print(f"[{ROBOT_NAME}]  {sentence}")
                spoken_any = True
            return spoken

        try:
            with self.claude.beta.messages.stream(**request) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        buf += event.delta.text
                        reply += event.delta.text
                    elif event.type == "content_block_stop" and buf and not buf.endswith(" "):
                        buf += " "      # text blocks split by a web search
                        reply += " "
                    else:
                        continue
                    sentences, buf = pop_sentences(buf)
                    for sentence in sentences:
                        if spoken := emit(sentence):
                            yield spoken
                final = stream.get_final_message()
            if buf.strip() and (spoken := emit(buf.strip())):
                yield spoken
            if final.stop_reason == "refusal":
                print(f"[{ROBOT_NAME}] refusal: {final.stop_details}")
                if not spoken_any:
                    yield "Sorry, that's not something I can help with."
        except Exception as exc:
            print(f"[error]  Claude API: {exc}")
            if not spoken_any:
                yield "Sorry, I had trouble with that. Could you say it again?"

        if reply.strip():
            self.history.append({"role": "assistant", "content": reply.strip()})

    # ── session save ─────────────────────────────────────────────────────────

    def _save_session(self):
        if not self.person_name or self._memory_excluded() or not self.memory_consent or len(self.history) < 4:
            return
        print(f"[{ROBOT_NAME}] saving session for {self.person_name}...")
        try:
            summary_request = (
                "In 3-5 sentences, summarize what was discussed in this conversation. "
                "Then on a new line starting with exactly 'TOPIC:', give a short 3-6 word label "
                "for the main topic of this conversation. "
                "Then on a new line starting with exactly 'FACTS:', list any new facts you learned "
                "about the user as a comma-separated list. If none, write 'FACTS: none'."
            )
            resp = self.claude.messages.create(
                model=SUMMARY_MODEL,
                max_tokens=300,
                messages=self.history + [{"role": "user", "content": summary_request}],
            )
            raw = resp.content[0].text

            summary_lines, topic, facts_raw = [], "", ""
            for line in raw.strip().splitlines():
                stripped = line.strip()
                if stripped.upper().startswith("TOPIC:"):
                    topic = stripped.split(":", 1)[1].strip()
                elif stripped.upper().startswith("FACTS:"):
                    facts_raw = stripped.split(":", 1)[1].strip()
                else:
                    summary_lines.append(line)
            summary = "\n".join(summary_lines).strip()

            if facts_raw and facts_raw.lower() != "none":
                new_facts = [f.strip() for f in facts_raw.split(",") if f.strip()]
                self.memory.add_facts(self.person_name, new_facts)
                print(f"[{ROBOT_NAME}] saved facts: {new_facts}")

            self.memory.save_session_summary(self.person_name, summary, topic=topic)
            print(f"[{ROBOT_NAME}] session summary saved (topic: {topic or 'n/a'})")
        except Exception as exc:
            print(f"[{ROBOT_NAME}] could not save session: {exc}")

    # ── main loop ────────────────────────────────────────────────────────────

    def _watch_keyboard(self):
        """Operator console: 'x' (or 'stop') + Enter = arm stop, conversation continues."""
        for line in sys.stdin:
            if line.strip().lower() in ("x", "stop"):
                self.gestures.stop()
                return

    def run(self):
        time.sleep(1.0)
        print(f"[{ROBOT_NAME}] starting up...")

        if self.demo and not self.person_name:
            greeting = random.choice([
                f"Hello, and welcome to the lab! I'm {ROBOT_NAME}.",
                f"Hi there! I'm {ROBOT_NAME}. Welcome — it's great to have you here.",
                f"Welcome! I'm {ROBOT_NAME}, the lab's humanoid robot. Ask me anything.",
            ])
        elif self.person_name:
            greeting = random.choice([
                f"Hey {self.person_name}! Good to see you.",
                f"Oh, {self.person_name}! You're back.",
                f"Well, well — {self.person_name}. What's up?",
                f"{self.person_name}! Perfect timing. What's on your mind?",
                f"Hey! I was just thinking about you, {self.person_name}.",
            ])
        else:
            greeting = random.choice([
                f"Hey! I'm {ROBOT_NAME}. What's up?",
                f"Hi there! Name's {ROBOT_NAME}. Ask me anything.",
                f"Oh, a new face! I'm {ROBOT_NAME}. Nice to meet you.",
                f"Hello! I'm {ROBOT_NAME} — part robot, all ears.",
                f"Hey! {ROBOT_NAME} here. What can I do for you?",
            ])

        self.gestures.play("high wave")
        self._speak(greeting)
        self.audio.LedControl(0, 128, 0)

        if self.person_name and not self._memory_excluded() and self.memory_consent is None:
            self.memory_consent = self._ask_memory_consent()
            self.memory.set_consent(self.person_name, self.memory_consent)

        if self.gestures.enabled:
            threading.Thread(target=self._watch_keyboard, daemon=True).start()
            print(f"[{ROBOT_NAME}] type x + Enter to stop the arms (conversation keeps going)")
        print(f"[{ROBOT_NAME}] listening  (Ctrl-C to quit)")

        while True:
            try:
                try:
                    user_text, emotion = self.asr_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if self.speaking.is_set():
                    print(f"[{ROBOT_NAME}] (dropped late ASR: {user_text!r})")
                    continue

                print(f"[user{'/' + emotion if emotion else ''}]  {user_text}")

                # LED while thinking — color based on emotion
                emotion_led = EMOTION_MAP.get(emotion, ((0, 0, 0), ""))[0]
                self.audio.LedControl(*emotion_led)

                self._speak_sentences(self._ask_claude(user_text, emotion))
                self.audio.LedControl(0, 128, 0)

            except KeyboardInterrupt:
                print(f"\n[{ROBOT_NAME}] shutting down...")
                self.face_id.stop_live_window()
                self.gestures.stop()   # arms down first, no wave on exit
                self._speak("Goodbye! It was great talking with you.")
                self._save_session()
                break


# ── entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=f"{ROBOT_NAME} voice conversation loop")
    parser.add_argument("interface", nargs="?", default=NETWORK_INTERFACE,
                        help=f"DDS network interface (default: {NETWORK_INTERFACE})")
    parser.add_argument("--demo", action="store_true",
                        help="visitor-facing persona instead of the classroom assistant (enables gestures)")
    parser.add_argument("--gestures", action=argparse.BooleanOptionalAction, default=None,
                        help="arm gestures while talking (default: on with --demo, off otherwise)")
    args = parser.parse_args()
    interface = args.interface
    gestures = args.demo if args.gestures is None else args.gestures
    print(f"[{ROBOT_NAME}] using network interface: {interface}"
          f"  (demo={args.demo}, gestures={gestures})")

    print(f"[{ROBOT_NAME}] loading web context...")
    web_context = load_context()
    print(f"[{ROBOT_NAME}] context loaded ({len(web_context)} chars)")

    ChannelFactoryInitialize(0, interface)

    bot = SB01ConversationLoop(interface, web_context, demo=args.demo, gestures=gestures)
    bot.setup()
    bot.run()


if __name__ == "__main__":
    main()
