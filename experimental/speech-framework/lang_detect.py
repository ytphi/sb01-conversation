"""
lang_detect.py  -  lightweight script/keyword language ID for en / zh / ja / es

Used on the LLM's reply to pick the TTS engine + voice. No model, no deps:
  - any kana                → ja
  - Han characters, no kana → zh   (kanji-only Japanese is rare in speech replies)
  - Spanish marks/words     → es
  - otherwise               → en
"""

import re

LANGUAGES = {"en": "English", "zh": "Chinese", "ja": "Japanese", "es": "Spanish"}

_SPANISH_CHARS = set("ñ¿¡áéíóú")
_SPANISH_WORDS = {
    "el", "la", "los", "las", "que", "qué", "de", "del", "y", "es", "por", "para",
    "con", "una", "un", "muy", "pero", "porque", "también", "yo", "tú", "usted",
    "estoy", "estás", "está", "eres", "soy", "como", "cómo", "dónde", "cuál",
    "hola", "gracias", "buenos", "buenas", "días", "tardes", "noches", "sí", "bien",
    "mucho", "gusto", "puedo", "puedes", "quiero", "tengo", "hoy", "aquí", "adiós",
}
# Single words that are confidently Spanish on their own
_SPANISH_STRONG = {"hola", "gracias", "adiós", "buenos", "buenas", "qué", "cómo", "dónde"}

_WORD_RE = re.compile(r"[a-záéíóúüñ]+", re.IGNORECASE)


def _is_kana(o: int) -> bool:
    return 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9F


def _is_han(o: int) -> bool:
    return 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF


def detect_language(text: str, default: str = "en") -> str:
    has_han = False
    for ch in text:
        o = ord(ch)
        if _is_kana(o):
            return "ja"
        if _is_han(o):
            has_han = True
    if has_han:
        return "zh"

    lower = text.lower()
    words = _WORD_RE.findall(lower)
    if not words:
        return default
    if any(c in _SPANISH_CHARS for c in lower) or _SPANISH_STRONG & set(words):
        return "es"
    hits = sum(w in _SPANISH_WORDS for w in words)
    if hits >= 2 and hits / len(words) >= 0.4:
        return "es"
    return "en"
