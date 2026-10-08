"""
reply_tags.py  -  the one tag grammar the LLM may use inside a spoken reply

  [laugh]                  sound tag      → TTS (spoken by models that support it, else dropped)
  {point}                  gesture cue    → gesture track (Track G), timed by character position
  <tone rise speed=1.1>    prosody plan   → TTS, applies to the text after it until the next
                                            <tone>, a </tone>, or the end of the sentence
  <pause 300>              silence (ms)   → TTS

<tone> attributes (any order, all optional):
  fall | rise | flat | question      contour
  pitch=+2                           semitones above / below the voice's normal pitch
  range=1.3                          more (>1) or less (<1) pitch movement
  speed=0.9                          speaking rate
  emph=word  or  emph="two words"    words to stress (comma or | separated)

Every tag is removed from the spoken text, so no consumer ever reads one aloud.
Unknown <...> tags are dropped too. Sound tags are kept in `tts_text` in place
so an engine with sound_tags=True can voice them.
"""

import re
from dataclasses import dataclass, field, replace

from tts_common import CONTOURS, Prosody

_TAG_RE = re.compile(
    r"\[(?P<sound>[a-zA-Z][a-zA-Z _-]{0,30})\]"
    r"|\{(?P<gesture>[a-zA-Z][a-zA-Z0-9 _-]{0,30})\}"
    r"|<(?P<close>/)?(?P<name>[a-zA-Z]+)(?P<attrs>[^<>]*)>"
)
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)|([+-]?\d+(?:\.\d+)?)|(\w+)""")
_MD_RE   = re.compile(r"[*_#`>~|]")


@dataclass
class Cue:
    name: str
    pos: int                  # character offset into the phrase's spoken text


@dataclass
class Phrase:
    """One stretch of a reply with a single prosody plan: the unit handed to TTS."""
    text: str                              # what is spoken (no tags)
    tts_text: str                          # same, with [sound] tags kept in place
    prosody: Prosody = field(default_factory=Prosody)
    gestures: list[Cue] = field(default_factory=list)
    sounds: list[Cue] = field(default_factory=list)
    lang: str = "en"


def clean_for_speech(text: str) -> str:
    """Strip markdown / emoji-ish symbols the TTS would read literally."""
    text = _MD_RE.sub("", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def parse_tone(attrs: str) -> Prosody:
    p = Prosody()
    for key, value, number, word in _ATTR_RE.findall(attrs):
        if key:
            key, value = key.lower(), value.strip("\"'")
            if key == "pitch":
                p.pitch = _float(value)
            elif key == "range":
                p.range = _float(value)
            elif key == "speed":
                p.speed = _float(value)
            elif key in ("emph", "emphasis", "stress"):
                p.emphasis = [w.strip().lower() for w in re.split(r"[,|]", value) if w.strip()]
            elif key in ("contour", "shape"):
                p.contour = value.lower() if value.lower() in CONTOURS else None
            elif key in ("pause", "ms"):
                p.pause_ms = int(_float(value) or 0)
        elif word and word.lower() in CONTOURS:
            p.contour = word.lower()
    return p


def strip_tags(text: str) -> str:
    """Spoken text only: every tag removed."""
    return clean_for_speech(_TAG_RE.sub(" ", text))


def parse_sentence(text: str, lang: str = "en", base: Prosody | None = None) -> list[Phrase]:
    """Split one sentence into phrases at <tone> / <pause> tags and collect cues."""
    base = base or Prosody()
    phrases: list[Phrase] = []
    spoken, tts, gestures, sounds = [], [], [], []
    prosody = base

    def flush(pause_ms: int | None = None):
        nonlocal spoken, tts, gestures, sounds
        raw = "".join(spoken)
        text_out = clean_for_speech(raw)
        if text_out or pause_ms:
            lead = len(raw) - len(raw.lstrip())
            shift = lambda cues: [Cue(c.name, max(0, min(len(text_out), c.pos - lead))) for c in cues]
            p = replace(prosody, emphasis=list(prosody.emphasis))
            if pause_ms:
                p.pause_ms = (p.pause_ms or 0) + pause_ms
            if text_out or not phrases:
                phrases.append(Phrase(text_out, clean_for_speech("".join(tts)), p,
                                      shift(gestures), shift(sounds), lang))
            else:                                   # a bare pause extends the previous phrase
                prev = phrases[-1].prosody
                prev.pause_ms = (prev.pause_ms or 0) + pause_ms
        spoken, tts, gestures, sounds = [], [], [], []

    pos = 0
    for m in _TAG_RE.finditer(text):
        chunk = text[pos:m.start()]
        spoken.append(chunk)
        tts.append(chunk)
        pos = m.end()
        here = len("".join(spoken))
        if m.group("sound"):
            name = m.group("sound").strip().lower()
            sounds.append(Cue(name, here))
            tts.append(f"[{name}]")
        elif m.group("gesture"):
            gestures.append(Cue(m.group("gesture").strip().lower(), here))
        else:
            name = m.group("name").lower()
            if name == "tone":
                flush()
                prosody = base if m.group("close") else base.merged(parse_tone(m.group("attrs")))
            elif name == "pause" and not m.group("close"):
                number = re.search(r"\d+", m.group("attrs"))
                flush(int(number.group()) if number else 250)
            # any other <tag> is dropped
    tail = text[pos:]
    spoken.append(tail)
    tts.append(tail)
    flush()
    return [p for p in phrases if p.text or p.prosody.pause_ms]


# ── streaming sentence splitter ─────────────────────────────────────────────

_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s+|[。！？]+")
_CLAUSE_END   = re.compile(r"[,;:]\s+|[、，；]")
_WORD_RE      = re.compile(r"\w+")


def _outside_tags(text: str, index: int) -> bool:
    return text.rfind("<", 0, index) <= text.rfind(">", 0, index)


class SentenceSplitter:
    """Feed LLM text deltas, get back complete sentences as soon as they end.

    The very first chunk of a reply is flushed early at a comma once it has
    `first_clause_words` words, so TTS can start before the first sentence ends.
    """

    def __init__(self, first_clause_words: int = 6, first_clause_chars_cjk: int = 10,
                 min_chars: int = 12, min_chars_cjk: int = 4):
        self.buffer = ""
        self.first_clause_words = first_clause_words
        self.first_clause_chars_cjk = first_clause_chars_cjk
        self.min_chars = min_chars                # "Hi!" alone makes Chatterbox Multilingual babble,
        self.min_chars_cjk = min_chars_cjk        # so very short sentences join the next one
        self.emitted = 0

    def _long_enough(self, text: str) -> bool:
        spoken = strip_tags(text)
        cjk = sum(1 for ch in spoken if ord(ch) > 0x2E80)
        return len(spoken) >= self.min_chars or cjk >= self.min_chars_cjk

    def feed(self, delta: str) -> list[str]:
        self.buffer += delta
        out = []
        while True:
            cut = None
            for m in _SENTENCE_END.finditer(self.buffer):
                if _outside_tags(self.buffer, m.start()) and self._long_enough(self.buffer[:m.end()]):
                    cut = m.end()
                    break
            if cut is None and self.emitted == 0 and not out and self.first_clause_words:
                cut = self._early_cut()
            if cut is None:
                break
            sentence, self.buffer = self.buffer[:cut].strip(), self.buffer[cut:]
            if strip_tags(sentence) or sentence:
                out.append(sentence)
        self.emitted += len(out)
        return out

    def _early_cut(self) -> int | None:
        for m in _CLAUSE_END.finditer(self.buffer):
            head = self.buffer[:m.start()]
            if not _outside_tags(self.buffer, m.start()):
                continue
            words = len(_WORD_RE.findall(strip_tags(head)))
            cjk = sum(1 for ch in head if ord(ch) > 0x2E80)
            if words >= self.first_clause_words or cjk >= self.first_clause_chars_cjk:
                return m.end()
        return None

    def flush(self) -> list[str]:
        rest, self.buffer = self.buffer.strip(), ""
        return [rest] if rest else []


def split_sentences(text: str) -> list[str]:
    """Whole-text version of SentenceSplitter (no early clause flush)."""
    s = SentenceSplitter(first_clause_words=0)
    return s.feed(text) + s.flush()
