"""
conversation.py  -  the INPUT → RESPONSE → INPUT loop, independent of hardware

  Conversation    - holds the LLM provider, persona prompt and short in-session history
  SpeechInput     - anything with listen() -> str | None   (None = stop)
  SpeechOutput    - anything with speak(text, lang)
  run_conversation(conv, inp, out) - the loop itself

The robot runner plugs in G1 ASR + G1 speaker; the non-robot test mode plugs
in the keyboard + local speakers. Swap either side without touching the loop.
"""

import re
from dataclasses import dataclass
from typing import Protocol

from lang_detect import detect_language
from llm_providers import LLMProvider, Message
from settings import resolve_path

FALLBACK_REPLY = "Sorry, I had trouble with that. Could you say it again?"


class SpeechInput(Protocol):
    def listen(self) -> str | None: ...


class SpeechOutput(Protocol):
    def speak(self, text: str, lang: str) -> None: ...


@dataclass
class Reply:
    text: str
    language: str


def load_persona(conv_cfg: dict, robot_name: str) -> str:
    path = resolve_path(conv_cfg.get("persona"))
    with open(path, encoding="utf-8") as f:
        return f.read().strip().replace("{robot_name}", robot_name)


def clean_for_speech(text: str) -> str:
    """Strip markdown / emoji-ish symbols the TTS would read literally."""
    text = re.sub(r"[*_#`>~|]", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


class Conversation:
    def __init__(self, llm: LLMProvider, system_prompt: str,
                 max_history_turns: int = 6, default_language: str = "en"):
        self.llm               = llm
        self.system_prompt     = system_prompt
        self.max_history_turns = max_history_turns
        self.default_language  = default_language
        self.history: list[Message] = []

    def respond(self, user_text: str) -> Reply:
        self.history.append(Message("user", user_text))
        keep = max(self.max_history_turns, 0) * 2 + 1
        self.history = self.history[-keep:]

        try:
            text = clean_for_speech(self.llm.chat(self.system_prompt, self.history))
        except Exception as exc:
            print(f"[error] {self.llm.name}: {exc}")
            self.history.pop()          # don't keep a turn the model never answered
            return Reply(FALLBACK_REPLY, "en")

        if not text:
            self.history.pop()
            return Reply(FALLBACK_REPLY, "en")

        self.history.append(Message("assistant", text))
        return Reply(text, detect_language(text, self.default_language))


def run_conversation(conv: Conversation, inp: SpeechInput, out: SpeechOutput,
                     greeting: str | None = None, robot_name: str = "sb01"):
    if greeting:
        print(f"[{robot_name}]  {greeting}")
        out.speak(greeting, detect_language(greeting, conv.default_language))

    print(f"[{robot_name}] listening  (Ctrl-C to quit)")
    while True:
        user_text = inp.listen()
        if user_text is None:
            break
        print(f"[user/{detect_language(user_text, conv.default_language)}]  {user_text}")
        reply = conv.respond(user_text)
        print(f"[{robot_name}/{reply.language}]  {reply.text}")
        out.speak(reply.text, reply.language)
