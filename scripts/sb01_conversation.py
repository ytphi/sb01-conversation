#!/usr/bin/env python3
"""
sb01_conversation.py  -  LLM-powered conversation loop for the G1 robot "sb01"

Features:
  - Face recognition at startup → loads per-person memory profile
  - Persistent memory: user profile + session summaries saved across runs
  - Emotion-aware: LED reacts to detected voice emotion; Claude gets emotion hint
  - Edge TTS with automatic language detection (EN / ZH / ES)
  - Optional co-speech arm gestures via scripts/gesture_server.py
  - Monitoring: timings, errors, service status and security telemetry kept in a
    temp folder; at shutdown the operator chooses whether/where to save a report

Network layout:
  eno0 (Ethernet, 192.168.123.x)  -- DDS: ASR messages in, TTS/LED commands out
  wlp0s20f3 (WiFi)                -- Claude API + web fetches over the internet

Usage:
  python3 scripts/sb01_conversation.py [network_interface]
"""

import sys
import os

# Load .env / env file from the project root so ANTHROPIC_API_KEY is available
# without needing to `source env` manually before running the script.
# setup.sh creates ".env"; older checkouts used "env". Either works.
# Precedence, highest first:  "env" file  >  shell environment  >  ".env" file
#   - "env" overrides the shell, exactly as the committed script did.
#   - ".env" only fills in variables that are not set yet. It was not read at
#     all before, so it must never replace a value that works today (setup.sh
#     creates it with a placeholder key until the operator edits it).
_ENV_LOADED = []   # (file name, key NAMES) for monitoring; values are never recorded
for _name, _overrides_shell in ((".env", False), ("env", True)):
    _env_file = os.path.join(os.path.dirname(__file__), "..", _name)
    if os.path.isfile(_env_file):
        _keys = []
        with open(_env_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    if _overrides_shell or _k.strip() not in os.environ:
                        os.environ[_k.strip()] = _v.strip()
                    _keys.append(_k.strip())
        _ENV_LOADED.append((_name, _keys))
import io
import re
import signal
import json
import time
import queue
import random
import asyncio
import threading
import urllib.request
import urllib.parse
from collections import deque
from html.parser import HTMLParser

import anthropic
import edge_tts
from pydub import AudioSegment

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from teleop.face_id import FaceIdentifier
from teleop.memory_manager import MemoryManager
from teleop.gesture_client import GestureClient, GESTURE_SAMPLE_RATE
from teleop.monitor import (Monitor, RUNTIME_BASE, cleanup_old_sessions, discard_session,
                            install_hooks, list_sessions, register_person_terms, sanitize)
from teleop.monitor_reports import offer_save

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

# Co-speech arm gestures: set SB01_GESTURE_URL to the scripts/gesture_server.py
# address to enable, e.g. http://127.0.0.1:8765. Unset = no arm motion.
GESTURE_URL = os.environ.get("SB01_GESTURE_URL", "").strip()
# Motion is delayed by the speaker's measured start-up time so it lands on the
# first sound. Never by more than this, in case that measurement is off.
AUDIO_LATENCY_MAX = 0.5

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

# The robot's name. Speech recognition often writes "Yotie" some other way
# ("Yodee", "You-Tee", "Yoti" ...). NAME_HEARD finds those in what a person said,
# so Claude can be told they mean the robot. NAME_SPOKEN_WRONG finds them, and
# the old "sb01" designation, in a reply, so the robot always says its own name
# as ROBOT_NAME. (File names and technical identifiers keep "sb01".)
# Matched only as a whole word among Latin letters and digits, so a name written
# right next to Chinese characters is still found.
_NAME_START, _NAME_END = "(?<![A-Za-z0-9])", "(?![A-Za-z0-9])"
NAME_HEARD = re.compile(
    _NAME_START + "(?:yo+d(?:ee|ie|ey|i|y)|yo+tt?(?:ie|ee|ey|i|y)|yo[- ]tee|you[- ]?tee)" + _NAME_END,
    re.IGNORECASE)
NAME_SPOKEN_WRONG = re.compile(
    _NAME_START + "(?:yo+d(?:ee|ie|ey|i|y)|yo+tt?(?:ie|ee|ey|i|y)|yo-?tee|you-?tee|sb[- ]?01)" + _NAME_END,
    re.IGNORECASE)

# Reply language. The robot answers in the language it is spoken to in. A short
# or unclear phrase never switches it, so a misheard word is answered in the
# language the robot last spoke.
LANGUAGE_NAMES  = {"en": "English", "es": "Spanish", "zh": "Chinese"}
LANGUAGE_VOICES = {"en": VOICE_EN, "es": VOICE_ES, "zh": VOICE_ZH}
SPANISH_WORDS = {
    "hola", "gracias", "qué", "que", "cómo", "como", "está", "estás", "estoy", "eres",
    "buenos", "buenas", "días", "dias", "tardes", "noches", "por", "favor", "dónde",
    "donde", "cuál", "cual", "quién", "quien", "el", "la", "los", "las", "un", "una",
    "es", "y", "de", "del", "para", "con", "tú", "usted", "puedes", "puede", "tiempo",
    "hace", "hoy", "sí", "adiós", "adios", "bien", "muy", "nombre", "llamas", "hablas",
    "español", "espanol", "tu", "te", "se", "en", "mi", "pero", "porque", "cuando",
    "cuándo", "tienes", "tiene", "quiero", "soy", "universidad", "clima",
}
# Short phrases that are a reliable sign of the speaker's language on their own.
CLEAR_GREETINGS = {
    "en": {"hi", "hello", "hey", "hi there", "hello there", "good morning", "good afternoon",
           "good evening", "thank you", "thanks"},
    "es": {"hola", "buenos días", "buenos dias", "buenas tardes", "buenas noches", "gracias"},
    "zh": {"你好", "您好", "你好吗", "谢谢", "谢谢你", "早上好", "晚上好"},
}

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
    "Do not use markdown, bullet points, or special characters. "
    "When given reference information in square brackets, use it naturally to answer questions. "
    "You speak English, Spanish and Chinese. Always reply in the language named in the "
    "bracketed language note on the latest message, even if the message itself or earlier "
    "turns are in another language. If the message is unclear or does not make sense, say "
    "briefly in that language that you did not catch it and ask them to repeat. "
    f"Your name is {ROBOT_NAME}. Speech recognition often writes your name differently, for "
    "example Yodee, You-Tee, Yoti, Yotee or Yodie. When someone uses a name like that for "
    f"you, they mean you. Always call yourself {ROBOT_NAME}, spelled exactly that way, and "
    "never use any other name for yourself. Do not point out or correct how someone says or "
    "spells your name."
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


def load_context(mon: "Monitor | None" = None) -> str:
    parts = []
    for url in PRELOAD_URLS:
        print(f"[{ROBOT_NAME}] fetching {url}...")
        parts.append(f"--- {url} ---\n{fetch_url(url)}")
    for path in PRELOAD_FILES:
        path = os.path.abspath(path)
        try:
            with open(path) as f:
                parts.append(f"--- {path} ---\n{f.read(3000)}")
            if mon:
                mon.file_access("read", os.path.relpath(path, os.path.join(os.path.dirname(__file__), "..")))
        except Exception as exc:
            parts.append(f"--- {path} ---\n[Could not read: {exc}]")
            if mon:
                mon.file_access("read", os.path.relpath(path, os.path.join(os.path.dirname(__file__), "..")),
                                ok=False, error=exc)
    return "\n\n".join(parts)


# ── name ──────────────────────────────────────────────────────────────────────

def heard_own_name(text: str) -> bool:
    """Did the person use a likely mis-transcription of the robot's name?"""
    return any(m.group(0).lower() != ROBOT_NAME.lower() for m in NAME_HEARD.finditer(text))


def say_own_name(text: str) -> str:
    """A reply with every variant of the robot's name replaced by ROBOT_NAME."""
    return NAME_SPOKEN_WRONG.sub(ROBOT_NAME, text)


# ── language ──────────────────────────────────────────────────────────────────

def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", text.lower())


def _has_cjk(text: str) -> bool:
    return any("一" <= c <= "鿿" for c in text)


def detect_language(text: str) -> str:
    """'en', 'es' or 'zh' for a piece of text."""
    if _has_cjk(text):
        return "zh"
    words = _words(text)
    if any(c in "áéíóúñ¿¡ü" for c in text.lower()):
        return "es"
    if words and sum(w in SPANISH_WORDS for w in words) / len(words) >= 0.5:
        return "es"
    return "en"


def is_clear_language(text: str, language: str) -> bool:
    """Is this phrase enough to switch the conversation to `language`?

    Short phrases are where speech recognition guesses the language wrong (an
    English "Hi" can come back as a Chinese character), so only a clear greeting
    or a longer sentence may switch. Anything else is answered in the language
    the robot last spoke."""
    cleaned = re.sub(r"[^\w\s]", "", text.lower()).strip()
    if cleaned in CLEAR_GREETINGS.get(language, ()):
        return True
    if language == "zh":
        return sum(_has_cjk(c) for c in text) >= 4
    return len(_words(text)) >= 3


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_emotion(raw: str) -> str:
    """'<|HAPPY|>' → 'happy',  '<|EMO_UNKNOWN|>' → ''"""
    clean = raw.replace("<|", "").replace("|>", "").replace("EMO_", "").lower()
    return "" if clean in ("unknown", "") else clean


# ── main class ────────────────────────────────────────────────────────────────

class SB01ConversationLoop:
    def __init__(self, interface: str, web_context: str, mon: Monitor):
        self.interface   = interface
        self.web_context = web_context
        self.audio       = AudioClient()
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
        self.person_name: str | None = None
        self.gestures: GestureClient | None = None
        self._play_called: float | None = None   # when the current audio was sent
        self._audio_latencies: deque[float] = deque(maxlen=5)   # sent -> sound, seconds

        # monitoring (goal 5): timings, errors, service status, security telemetry
        self.mon = mon
        register_person_terms(getattr(self.face_id, "known_names", []))   # redact names; never stored
        self.mon.file_access("list", "memory/faces", count=len(getattr(self.face_id, "known_names", [])))
        self._first_audio_sent: float | None = None   # first PCM chunk of current reply
        self._speech_started: float | None = None     # robot reported play_state 1
        self._last_tts_ms: float | None = None
        self.language = "en"   # language the robot last spoke; replies stay in it until a clear switch

    # ── startup ──────────────────────────────────────────────────────────────

    def _identify_person(self) -> str | None:
        print(f"[{ROBOT_NAME}] scanning for known faces...")
        name = self.face_id.identify()
        self.mon.face(getattr(self.face_id, "last_result", None))   # outcome only, never the name
        if name:
            print(f"[{ROBOT_NAME}] recognized: {name}")
        else:
            print(f"[{ROBOT_NAME}] face not recognized")
        return name

    def _build_system_prompt(self) -> str:
        prompt = BASE_SYSTEM_PROMPT
        if self.person_name:
            try:
                ctx = self.memory.build_context(self.person_name)
            except Exception as exc:
                self.mon.file_access("read", "memory/profiles/[person]", ok=False, error=exc)
                raise
            if self.mon.once("profile_read"):
                self.mon.file_access("read", "memory/profiles/[person]", chars=len(ctx))
            prompt += f"\n\nWhat you know about this person:\n{ctx}"
        if self.web_context:
            prompt += f"\n\nReference information:\n{self.web_context}"
        return prompt

    def setup(self):
        self.mon.startup_checks(GESTURE_URL)
        self.audio.SetTimeout(10.0)
        self.audio.Init()
        self._volume_reply = self.audio.SetVolume(100)   # robot's reply code, kept for monitoring
        self.mon.command("audio", "SetVolume", value=100, code=self._volume_reply)

        self.person_name = self._identify_person()
        self.face_id.start_live_window()

        self.asr_sub = ChannelSubscriber(ASR_TOPIC, String_)
        self.asr_sub.Init(self._asr_callback, 10)
        print(f"[{ROBOT_NAME}] subscribed to {ASR_TOPIC}")

        if GESTURE_URL:
            self.gestures = GestureClient(GESTURE_URL)
            print(f"[{ROBOT_NAME}] arm gestures enabled via {GESTURE_URL}")
            self.mon.watch_gestures(self.gestures)   # read-only polling of its state
            self.mon.robot_fsm(self.gestures)        # FSM id, read once before any gesture

    # ── DDS callback ─────────────────────────────────────────────────────────

    def _asr_callback(self, msg: String_):
        try:
            data = json.loads(msg.data)
        except (json.JSONDecodeError, AttributeError):
            return

        if "play_state" in data:
            if data["play_state"] == 0:
                self.tts_done.set()
            elif data["play_state"] == 1:
                self._speech_started = time.time()
                self._note_audio_started()
            return

        if self.speaking.is_set():
            return

        text = data.get("text", "").strip()
        # Drop noise (nothing, punctuation, a lone letter) but keep short real
        # phrases such as "Hi", "Hola" and "你好" for the language handling.
        # One Chinese character is a whole word, so it is kept too.
        spoken = re.sub(r"[\W_]", "", text)
        if len(spoken) < 2 and not _has_cjk(spoken):
            return

        emotion = _parse_emotion(data.get("emotion", ""))
        self.mon.asr_seen()

        if data.get("is_final", False):
            if self._asr_timer:
                self._asr_timer.cancel()
            self._pending_text = ""
            self.asr_queue.put((text, emotion, time.time()))
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
            self.asr_queue.put((text, emotion, time.time()))

    # ── TTS ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _pick_voice(text: str) -> str:
        if any('一' <= c <= '鿿' for c in text):
            return VOICE_ZH
        if any(c in {'á','é','í','ó','ú','ñ','¿','¡','ü'} for c in text.lower()):
            return VOICE_ES
        return VOICE_EN

    async def _text_to_pcm(self, text: str, language: str | None = None) -> tuple[bytes, bytes | None]:
        """Returns (playback PCM, gesture-model PCM or None if gestures are off)."""
        voice = self._pick_voice(text)
        if voice == VOICE_EN and language in ("es", "zh"):
            voice = LANGUAGE_VOICES[language]   # e.g. a Spanish reply with no accented letters
        communicate = edge_tts.Communicate(text, voice)
        mp3_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_chunks.append(chunk["data"])
        mp3_data = b"".join(mp3_chunks)
        audio = AudioSegment.from_mp3(io.BytesIO(mp3_data))
        audio = audio.set_channels(1).set_sample_width(2)
        gesture_pcm = (
            audio.set_frame_rate(GESTURE_SAMPLE_RATE).raw_data if self.gestures else None
        )
        return audio.set_frame_rate(PCM_SAMPLE_RATE).raw_data, gesture_pcm

    def _begin_gesture(self):
        """Start the motion so its first frame lands on the first sound, allowing
        for how long the speaker has recently taken to start playing."""
        recent = sorted(self._audio_latencies)
        latency = recent[len(recent) // 2] if recent else 0.0
        self._play_called = time.time()
        self.gestures.begin(min(latency, AUDIO_LATENCY_MAX))

    def _note_audio_started(self):
        called, self._play_called = self._play_called, None
        if called is None:
            return
        latency = time.time() - called
        if 0.0 <= latency <= 2.0:
            self._audio_latencies.append(latency)

    def _led(self, r: int, g: int, b: int):
        """Set the head LED, and note the command for monitoring."""
        code = self.audio.LedControl(r, g, b)
        self.mon.command("led", "LedControl", value=f"{r},{g},{b}", code=code)

    def _speak(self, text: str, language: str | None = None):
        text = re.sub(r'\bCSUSB\b', 'Cal State San Bernardino', text, flags=re.IGNORECASE)
        self.speaking.set()
        self.tts_done.clear()
        self._first_audio_sent = None
        self.mon.set_state("speaking")
        step = "robot_audio"            # which step was running, for error classification only
        try:
            self._led(0, 0, 128)
            self._last_tts_ms = None
            step = "tts"
            tts_started = time.perf_counter()
            pcm, gesture_pcm = asyncio.run(self._text_to_pcm(text, language))
            self._last_tts_ms = (time.perf_counter() - tts_started) * 1000.0
            step = "gesture"
            # Arms are taken and the first second of motion is ready, or no gesture.
            gesturing = bool(gesture_pcm) and self.gestures.start(gesture_pcm)
            if self.gestures:
                self.mon.gesture("started" if gesturing else "not_started")

            step = "robot_audio"
            stream_id = str(int(time.time() * 1000))
            total = len(pcm)
            for offset in range(0, total, PCM_CHUNK_BYTES):
                chunk = pcm[offset:offset + PCM_CHUNK_BYTES]
                if offset == 0 and gesturing:
                    self._begin_gesture()
                if offset == 0:
                    self._first_audio_sent = time.time()
                code = self.audio.PlayStream("sb01", stream_id, list(chunk))
                if offset == 0:      # one record per reply: the words and the play command
                    self.mon.speech("robot", text, language)
                    self.mon.command("audio", "PlayStream", stream=stream_id, code=code, bytes=total,
                                     seconds=round(total / (PCM_SAMPLE_RATE * 2), 1))
                if offset + PCM_CHUNK_BYTES < total:
                    time.sleep(1.0)
            self.tts_done.wait(timeout=total / (PCM_SAMPLE_RATE * 2) + 3.0)
            time.sleep(0.4)
            if gesturing:
                stats = getattr(self.gestures, "stats", {}) or {}
                self.mon.gesture("utterance_done", track_error_max=stats.get("track_error_max"),
                                 blocks=stats.get("blocks"))
        except BaseException as exc:
            if self.gestures:
                self.gestures.stop()
            if isinstance(exc, Exception):       # not Ctrl-C / SIGTERM / SystemExit
                endpoint = {"tts": "speech.platform.bing.com", "gesture": GESTURE_URL or None,
                            "robot_audio": f"dds:{self.interface}"}.get(step)
                self.mon.error(step, exc, endpoint=endpoint)
            raise
        finally:
            self._led(0, 0, 0)
            self.speaking.clear()
            self.mon.set_state("listening")

    # ── Claude ───────────────────────────────────────────────────────────────

    def _ask_claude(self, user_text: str, emotion: str) -> str:
        context_parts = []

        # reply language: follow the speaker, but a short or unclear phrase never
        # switches it - a misheard phrase is answered in the language last spoken
        heard = detect_language(user_text)
        if heard != self.language and is_clear_language(user_text, heard):
            print(f"[language] {LANGUAGE_NAMES[self.language]} -> {LANGUAGE_NAMES[heard]}")
            self.language = heard
        context_parts.append(f"[Reply in {LANGUAGE_NAMES[self.language]}.]")

        # the robot's name as speech recognition tends to write it
        if heard_own_name(user_text):
            context_parts.append(f"[The name in this message is how your name, {ROBOT_NAME}, was "
                                 f"transcribed; the person is talking to you.]")

        # weather injection
        if set(user_text.lower().split()) & WEATHER_KEYWORDS:
            location = extract_location(user_text)
            weather = fetch_weather(location)
            print(f"[weather] {weather}")
            context_parts.append(f"[Current weather: {weather}]")

        # emotion hint
        emotion_led, emotion_hint = EMOTION_MAP.get(emotion, ((0, 0, 0), ""))
        if emotion_hint:
            context_parts.append(f"[{emotion_hint}]")

        augmented = " ".join(context_parts) + " " + user_text if context_parts else user_text
        self.history.append({"role": "user", "content": augmented})

        if len(self.history) > MAX_HISTORY_TURNS * 2:
            self.history = self.history[-(MAX_HISTORY_TURNS * 2):]

        try:
            system = self._build_system_prompt()
        except Exception as exc:
            _tag_component(exc, "memory")    # for monitoring: a profile read, not the Claude API
            raise
        response = self.claude.messages.create(
            model="claude-opus-4-8",
            max_tokens=256,
            system=system,
            messages=self.history,
        )
        reply = say_own_name(response.content[0].text)   # it only ever calls itself ROBOT_NAME
        self.history.append({"role": "assistant", "content": reply})
        return reply, emotion_led

    # ── session save ─────────────────────────────────────────────────────────

    def _save_session(self):
        if not self.person_name or len(self.history) < 4:
            return
        print(f"[{ROBOT_NAME}] saving session for {self.person_name}...")
        step = "claude"                  # which step was running, for error classification only
        try:
            summary_request = (
                "In 3-5 sentences, summarize what was discussed in this conversation. "
                "Then on a new line starting with exactly 'FACTS:', list any new facts you learned "
                "about the user as a comma-separated list. If none, write 'FACTS: none'."
            )
            resp = self.claude.messages.create(
                model="claude-opus-4-8",
                max_tokens=300,
                messages=self.history + [{"role": "user", "content": summary_request}],
            )
            raw = resp.content[0].text
            step = "memory"

            if "FACTS:" in raw:
                summary, facts_line = raw.split("FACTS:", 1)
                facts_raw = facts_line.strip()
                if facts_raw.lower() != "none":
                    new_facts = [f.strip() for f in facts_raw.split(",") if f.strip()]
                    self.memory.add_facts(self.person_name, new_facts)
                    self.mon.file_access("write", "memory/profiles/[person]", count=len(new_facts))
                    print(f"[{ROBOT_NAME}] saved facts: {new_facts}")
            else:
                summary = raw

            self.memory.save_session_summary(self.person_name, summary.strip())
            self.mon.file_access("write", "memory/sessions/[person]", chars=len(summary.strip()))
            print(f"[{ROBOT_NAME}] session summary saved")
        except Exception as exc:
            print(f"[{ROBOT_NAME}] could not save session: {sanitize(f'{type(exc).__name__}: {exc}')}")
            self.mon.error(step, exc, endpoint="api.anthropic.com" if step == "claude" else None)
            if step == "memory":
                self.mon.file_access("write", "memory/[person]", ok=False, error=exc)

    # ── main loop ────────────────────────────────────────────────────────────

    def run(self):
        time.sleep(1.0)
        print(f"[{ROBOT_NAME}] starting up...")

        if self.person_name:
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

        self._speech_started = None
        self._last_tts_ms = None
        self._speak(greeting)
        # Confirmed signal: the robot audio service's reply code to SetVolume (0 = OK).
        reply = getattr(self, "_volume_reply", None)
        if isinstance(reply, int) and not isinstance(reply, bool):
            self.mon.check("robot_audio", reply == 0,
                           f"SetVolume reply code {reply} on {self.interface}"
                           + ("" if reply == 0 else " - robot audio service did not confirm"))
        else:
            self.mon.check("robot_audio", None, f"SetVolume returned {type(reply).__name__}; not interpretable")
        # play_state after PlayStream is not a documented signal: informational only.
        self.mon.observe("play_state_during_greeting", self._speech_started is not None,
                         "unconfirmed signal; absence is not treated as a failure")
        self.mon.check("edge_tts", self._last_tts_ms is not None,
                       f"greeting synthesized in {self._last_tts_ms:.0f}ms" if self._last_tts_ms else "")
        self.mon.set_state("listening")
        self._led(0, 128, 0)
        print(f"[{ROBOT_NAME}] listening  (Ctrl-C to quit)")

        while True:
            try:
                try:
                    user_text, emotion, heard_at = self.asr_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if self.speaking.is_set():
                    print(f"[{ROBOT_NAME}] (dropped late ASR: {user_text!r})")
                    self.mon.event("asr_dropped", chars=len(user_text))
                    self.mon.speech("user", user_text, status="not answered")
                    continue

                self.mon.set_state("thinking")
                turn = {"error": None}          # timings only; the words go to mon.speech
                self.mon.speech("user", user_text)

                print(f"[user{'/' + emotion if emotion else ''}]  {user_text}")

                # LED while thinking — color based on emotion
                emotion_led = EMOTION_MAP.get(emotion, ((0, 0, 0), ""))[0]
                self._led(*emotion_led)

                try:
                    with self.mon.timed("claude_ms", turn):
                        reply, emotion_led = self._ask_claude(user_text, emotion)
                except Exception as exc:
                    component = getattr(exc, "_sb01_component", "claude")
                    label = {"claude": "Claude API", "memory": "memory profile"}.get(component, component)
                    print(f"[error]  {label}: {sanitize(f'{type(exc).__name__}: {exc}')}")
                    self.mon.error(component, exc,
                                   endpoint="api.anthropic.com" if component == "claude" else None)
                    turn["error"] = component
                    reply = "Sorry, I had trouble with that. Could you say it again?"
                    emotion_led = (0, 0, 0)

                print(f"[{ROBOT_NAME}]  {reply}")
                # Claude's reply is in the conversation language; the fixed apology is English.
                self._speak(reply, language=self.language if turn["error"] is None else None)
                # (a TTS failure still ends the script, as before)
                self._led(0, 128, 0)

                done = time.time()
                if self._last_tts_ms is not None:
                    turn["tts_ms"] = round(self._last_tts_ms, 1)
                    self.mon.latency["tts_ms"].append(self._last_tts_ms)
                if self._first_audio_sent:
                    turn["response_ms"] = round((self._first_audio_sent - heard_at) * 1000.0, 1)
                turn["turn_ms"] = round((done - heard_at) * 1000.0, 1)
                turn["user_chars"], turn["reply_chars"] = len(user_text), len(reply)
                self.mon.turn(**turn)

            except KeyboardInterrupt:
                print(f"\n[{ROBOT_NAME}] shutting down...")
                self.face_id.stop_live_window()
                self._speak("Goodbye! It was great talking with you.")
                if self.gestures:
                    self.gestures.wait()
                self._save_session()
                break


# ── entry point ──────────────────────────────────────────────────────────────

def _tag_component(exc, component):
    try:
        exc._sb01_component = component
    except Exception:
        pass


def _exit_kind(exc):
    """(exit_type, reason) for monitoring. Classification only; changes nothing."""
    if exc is None:
        return "clean", "stopped by operator"
    if isinstance(exc, SystemExit):
        code = exc.code
        if isinstance(code, int) and code > 128:          # gesture_client: SystemExit(128 + signum)
            try:
                name = signal.Signals(code - 128).name
            except ValueError:
                name = f"signal {code - 128}"
            return "signal", f"stopped by {name}"
        return "exit", f"exited with code {code}"
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted", "interrupted by operator (Ctrl-C during shutdown)"
    return "crashed", f"crashed: {type(exc).__name__}: {exc}"


def _interactive() -> bool:
    """Ask the operator at the terminal? SB01_SAVE_PROMPT=0 never asks."""
    mode = os.environ.get("SB01_SAVE_PROMPT", "").strip().lower()
    if mode == "0":
        return False
    return mode == "force" or sys.stdin.isatty()


def _startup_recovery():
    """Monitoring data left by a run that crashed: offer to save it, then delete it."""
    removed = cleanup_old_sessions()
    if removed:
        print(f"[monitor] removed {removed} unsaved monitoring session(s) older than 7 days")
    for left in (s for s in list_sessions() if not s["alive"]):
        if _interactive():
            offer_save(left["path"], left["session"], reason="recovery")
        else:
            discard_session(left["path"])
            print(f"[monitor] deleted unsaved monitoring data from session {left['session']}")


def main():
    interface = sys.argv[1] if len(sys.argv) > 1 else NETWORK_INTERFACE
    print(f"[{ROBOT_NAME}] using network interface: {interface}")

    try:
        _startup_recovery()
    except Exception as exc:
        print(f"[monitor] crash-recovery check skipped: {sanitize(f'{type(exc).__name__}: {exc}')}")
    mon = Monitor(ROBOT_NAME, interface=interface, gesture_enabled=bool(GESTURE_URL))   # never raises
    if mon.enabled:
        install_hooks(mon, GESTURE_URL)      # passive: records, never changes, network calls
    for name, keys in _ENV_LOADED:
        mon.file_access("read", name, keys=keys)
    if not _ENV_LOADED:
        mon.file_access("read", ".env", ok=False, error="no .env or env file found")

    bot = None
    try:
        print(f"[{ROBOT_NAME}] loading web context...")
        web_context = load_context(mon)
        print(f"[{ROBOT_NAME}] context loaded ({len(web_context)} chars)")

        ChannelFactoryInitialize(0, interface)

        bot = SB01ConversationLoop(interface, web_context, mon)
        bot.setup()
        bot.run()
    finally:
        if bot is not None and bot.gestures:
            bot.gestures.close()   # arms are released however the script ends
        # Monitoring only records why the script ended; it never changes that,
        # and nothing below can raise (the original exception, if any, continues).
        exit_type, reason = _exit_kind(sys.exc_info()[1])
        mon.close(reason, exit_type)
        try:
            if not mon.enabled:
                pass
            elif exit_type == "clean" and _interactive():
                offer_save(mon.dir, mon.session)      # operator chooses: save or discard
            else:
                print(f"[monitor] monitoring data for session {mon.session} is kept in temporary storage.\n"
                      f"[monitor] Save it with: python3 scripts/monitor_export.py --session {mon.session} "
                      f"--audience team1   (or security, or transcript)\n"
                      f"[monitor] The next interactive start offers to save it; a non-interactive start "
                      f"deletes it.")
        except Exception as exc:
            print(f"[monitor] report step skipped: {sanitize(f'{type(exc).__name__}: {exc}')}")


if __name__ == "__main__":
    main()
