"""
conversation.py  -  the INPUT → RESPONSE → INPUT loop, independent of hardware

  Conversation    - holds the LLM provider, persona prompt and short in-session history
  SpeechInput     - anything with listen() -> str | None   (None = stop)
  SpeechOutput    - anything with speak(text, lang); optionally speak_stream(sentences, timer=)
  run_conversation(conv, inp, out) - the loop itself

The robot runner plugs in G1 / PC speech input + the G1 speaker; the non-robot
test mode plugs in the keyboard + local speakers. Swap either side without
touching the loop.

With streaming on (conversation.stream, default true) and an output that has
speak_stream, the reply is spoken sentence by sentence while the LLM is still
writing it. Replies may carry tags (reply_tags.py); they are kept in history
so the model stays consistent, and never spoken.
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from lang_detect import detect_language
from llm_providers import LLMProvider, Message
from reply_tags import SentenceSplitter, clean_for_speech, strip_tags  # noqa: F401 (re-export)
from settings import resolve_path
from timing_log import TimingLog, TurnTimer

FALLBACK_REPLY = "Sorry, I had trouble with that. Could you say it again?"


class SpeechInput(Protocol):
    def listen(self) -> str | None: ...


class SpeechOutput(Protocol):
    def speak(self, text: str, lang: str) -> None: ...


@dataclass
class Reply:
    text: str                 # spoken text (tags removed)
    language: str
    raw: str = ""             # as the LLM wrote it, tags included


def load_persona(conv_cfg: dict, robot_name: str) -> str:
    path = resolve_path(conv_cfg.get("persona"))
    with open(path, encoding="utf-8") as f:
        return f.read().strip().replace("{robot_name}", robot_name)


def load_tag_guide(conv_cfg: dict) -> str:
    path = resolve_path(conv_cfg.get("tone_guide"))
    if not path:
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class Conversation:
    def __init__(self, llm: LLMProvider, system_prompt: str,
                 max_history_turns: int = 6, default_language: str = "en",
                 stream: bool = True, first_clause_words: int = 6):
        self.llm               = llm
        self.base_prompt       = system_prompt
        self.system_prompt     = system_prompt
        self.max_history_turns = max_history_turns
        self.default_language  = default_language
        self.stream            = stream
        self.first_clause_words = first_clause_words
        self.history: list[Message] = []
        self.last_reply: Reply | None = None

    def add_instructions(self, text: str):
        """Append e.g. the <tone> tag guide when the voice can use it."""
        self.system_prompt = self.base_prompt + ("\n\n" + text.strip() if text.strip() else "")

    def _push_user(self, user_text: str):
        self.history.append(Message("user", user_text))
        keep = max(self.max_history_turns, 0) * 2 + 1
        self.history = self.history[-keep:]

    def _finish(self, raw: str) -> Reply:
        spoken = strip_tags(raw)
        if not spoken:
            self.history.pop()
            self.last_reply = Reply(FALLBACK_REPLY, "en", FALLBACK_REPLY)
            return self.last_reply
        self.history.append(Message("assistant", raw.strip()))
        reply = Reply(spoken, detect_language(spoken, self.default_language), raw.strip())
        self.last_reply = reply
        return reply

    def respond(self, user_text: str) -> Reply:
        self._push_user(user_text)
        try:
            raw = self.llm.chat(self.system_prompt, self.history)
        except Exception as exc:
            print(f"[error] {self.llm.name}: {exc}")
            self.history.pop()          # don't keep a turn the model never answered
            self.last_reply = Reply(FALLBACK_REPLY, "en", FALLBACK_REPLY)
            return self.last_reply
        return self._finish(raw)

    def respond_stream(self, user_text: str, timer: TurnTimer | None = None) -> Iterator[str]:
        """Yield the reply sentence by sentence (tags still in) as the LLM writes it."""
        self._push_user(user_text)
        splitter = SentenceSplitter(self.first_clause_words)
        parts: list[str] = []
        try:
            for delta in self.llm.stream(self.system_prompt, self.history):
                if timer and delta:
                    timer.mark("llm_first_token")
                parts.append(delta)
                yield from splitter.feed(delta)
            yield from splitter.flush()
        except Exception as exc:
            print(f"[error] {self.llm.name}: {exc}")
            if not strip_tags("".join(parts)):
                self.history.pop()
                self.last_reply = Reply(FALLBACK_REPLY, "en", FALLBACK_REPLY)
                yield FALLBACK_REPLY
                return
        if timer:
            timer.mark("llm_done")
        self._finish("".join(parts))
        if not strip_tags("".join(parts)):          # nothing speakable came back
            yield FALLBACK_REPLY


def _stamp_input(timer: TurnTimer, inp):
    for attr, stage in (("last_speech_end", "speech_end"), ("last_turn_end", "turn_detected"),
                        ("last_transcribed", "transcript_ready")):
        t = getattr(inp, attr, None)
        if t:
            timer.mark(stage, t)


def run_conversation(conv: Conversation, inp: SpeechInput, out: SpeechOutput,
                     greeting: str | None = None, robot_name: str = "sb01",
                     timing_log: TimingLog | None = None):
    if greeting:
        print(f"[{robot_name}]  {greeting}")
        out.speak(greeting, detect_language(greeting, conv.default_language))

    timing_log = timing_log or TimingLog(None)
    streaming = conv.stream and hasattr(out, "speak_stream")
    print(f"[{robot_name}] listening  (Ctrl-C to quit){'  [streaming]' if streaming else ''}")
    while True:
        user_text = inp.listen()
        if user_text is None:
            break
        timer = timing_log.new_turn()
        _stamp_input(timer, inp)
        timer.mark("transcript_ready")            # keyboard input: the turn starts now
        print(f"[user/{detect_language(user_text, conv.default_language)}]  {user_text}")
        if streaming:
            out.speak_stream(conv.respond_stream(user_text, timer), timer=timer)
            reply = conv.last_reply
            if reply:
                print(f"[{robot_name}/{reply.language}]  {reply.raw}")
        else:
            t0 = time.time()
            reply = conv.respond(user_text)
            timer.mark("llm_done")
            print(f"[timing] llm {time.time() - t0:.2f}s")
            print(f"[{robot_name}/{reply.language}]  {reply.raw or reply.text}")
            out.speak(reply.raw or reply.text, reply.language)
            timer.mark("playback_end")
        timer.add(stream=streaming, voice=getattr(out, "voice_name", None))
        print(f"[timing] {timer.summary()}")
        timer.finish()
