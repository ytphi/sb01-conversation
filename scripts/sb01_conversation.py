#!/usr/bin/env python3
"""
sb01_conversation.py  -  LLM-powered conversation loop for the G1 robot "sb01"

Features:
  - Face recognition at startup → loads per-person memory profile
  - Persistent memory: user profile + session summaries saved across runs
  - Emotion-aware: LED reacts to detected voice emotion; Claude gets emotion hint
  - Edge TTS with automatic language detection (EN / ZH / ES)

Network layout:
  eno0 (Ethernet, 192.168.123.x)  -- DDS: ASR messages in, TTS/LED commands out
  wlp0s20f3 (WiFi)                -- Claude API + web fetches over the internet

Usage:
  python3 scripts/sb01_conversation.py [network_interface]
"""

import sys
import os
import io
import re
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from teleop.face_id import FaceIdentifier
from teleop.memory_manager import MemoryManager

# ── config ───────────────────────────────────────────────────────────────────
NETWORK_INTERFACE = "eno0"
ROBOT_NAME        = "sb01"
ASR_TOPIC         = "rt/audio_msg"
VOICE_EN = "en-US-JennyNeural"
VOICE_ZH = "zh-CN-XiaoxiaoNeural"
VOICE_ES = "es-MX-DaliaNeural"

PCM_SAMPLE_RATE   = 16000
PCM_CHUNK_BYTES   = 96000
MAX_HISTORY_TURNS = 10

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
    "When given reference information in square brackets, use it naturally to answer questions."
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

def _parse_emotion(raw: str) -> str:
    """'<|HAPPY|>' → 'happy',  '<|EMO_UNKNOWN|>' → ''"""
    clean = raw.replace("<|", "").replace("|>", "").replace("EMO_", "").lower()
    return "" if clean in ("unknown", "") else clean


# ── main class ────────────────────────────────────────────────────────────────

class SB01ConversationLoop:
    def __init__(self, interface: str, web_context: str):
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

    # ── startup ──────────────────────────────────────────────────────────────

    def _identify_person(self) -> str | None:
        print(f"[{ROBOT_NAME}] scanning for known faces...")
        name = self.face_id.identify()
        if name:
            print(f"[{ROBOT_NAME}] recognized: {name}")
        else:
            print(f"[{ROBOT_NAME}] face not recognized")
        return name

    def _build_system_prompt(self) -> str:
        prompt = BASE_SYSTEM_PROMPT
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

        self.person_name = self._identify_person()
        self.face_id.start_live_window()

        self.asr_sub = ChannelSubscriber(ASR_TOPIC, String_)
        self.asr_sub.Init(self._asr_callback, 10)
        print(f"[{ROBOT_NAME}] subscribed to {ASR_TOPIC}")

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
        # Chinese temporarily disabled
        # if any('一' <= c <= '鿿' for c in text):
        #     return VOICE_ZH
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
        text = re.sub(r'\bCSUSB\b', 'Cal State San Bernardino', text, flags=re.IGNORECASE)
        self.speaking.set()
        self.tts_done.clear()
        try:
            self.audio.LedControl(0, 0, 128)
            pcm = asyncio.run(self._text_to_pcm(text))
            stream_id = str(int(time.time() * 1000))
            total = len(pcm)
            for offset in range(0, total, PCM_CHUNK_BYTES):
                chunk = pcm[offset:offset + PCM_CHUNK_BYTES]
                self.audio.PlayStream("sb01", stream_id, list(chunk))
                if offset + PCM_CHUNK_BYTES < total:
                    time.sleep(1.0)
            self.tts_done.wait(timeout=total / (PCM_SAMPLE_RATE * 2) + 3.0)
            time.sleep(0.4)
        finally:
            self.audio.LedControl(0, 0, 0)
            self.speaking.clear()

    # ── Claude ───────────────────────────────────────────────────────────────

    def _ask_claude(self, user_text: str, emotion: str) -> str:
        context_parts = []

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

        response = self.claude.messages.create(
            model="claude-opus-4-8",
            max_tokens=256,
            system=self._build_system_prompt(),
            messages=self.history,
        )
        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply, emotion_led

    # ── session save ─────────────────────────────────────────────────────────

    def _save_session(self):
        if not self.person_name or len(self.history) < 4:
            return
        print(f"[{ROBOT_NAME}] saving session for {self.person_name}...")
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

            if "FACTS:" in raw:
                summary, facts_line = raw.split("FACTS:", 1)
                facts_raw = facts_line.strip()
                if facts_raw.lower() != "none":
                    new_facts = [f.strip() for f in facts_raw.split(",") if f.strip()]
                    self.memory.add_facts(self.person_name, new_facts)
                    print(f"[{ROBOT_NAME}] saved facts: {new_facts}")
            else:
                summary = raw

            self.memory.save_session_summary(self.person_name, summary.strip())
            print(f"[{ROBOT_NAME}] session summary saved")
        except Exception as exc:
            print(f"[{ROBOT_NAME}] could not save session: {exc}")

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

        self._speak(greeting)
        self.audio.LedControl(0, 128, 0)
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

                try:
                    reply, emotion_led = self._ask_claude(user_text, emotion)
                except Exception as exc:
                    print(f"[error]  Claude API: {exc}")
                    reply = "Sorry, I had trouble with that. Could you say it again?"
                    emotion_led = (0, 0, 0)

                print(f"[{ROBOT_NAME}]  {reply}")
                self._speak(reply)
                self.audio.LedControl(0, 128, 0)

            except KeyboardInterrupt:
                print(f"\n[{ROBOT_NAME}] shutting down...")
                self.face_id.stop_live_window()
                self._speak("Goodbye! It was great talking with you.")
                self._save_session()
                break


# ── entry point ──────────────────────────────────────────────────────────────

def main():
    interface = sys.argv[1] if len(sys.argv) > 1 else NETWORK_INTERFACE
    print(f"[{ROBOT_NAME}] using network interface: {interface}")

    print(f"[{ROBOT_NAME}] loading web context...")
    web_context = load_context()
    print(f"[{ROBOT_NAME}] context loaded ({len(web_context)} chars)")

    ChannelFactoryInitialize(0, interface)

    bot = SB01ConversationLoop(interface, web_context)
    bot.setup()
    bot.run()


if __name__ == "__main__":
    main()
