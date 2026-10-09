"""
languages.py  -  the languages the robot can hold a conversation in

The list is Chatterbox Multilingual's (experimental/utilities/tts/chatterbox-ml3):
every language here can be spoken by the Chatterbox voice, and each has an
Edge TTS voice for when Chatterbox is off or fails.

  NAMES                 code -> English name, for the "[Reply in ...]" note to Claude
  EDGE_VOICES           code -> Edge TTS voice
  script_language(text) a language that the writing system alone shows, else None
  from_whisper(code)    Whisper's language code -> one of ours, or None
"""

NAMES = {
    "en": "English", "es": "Spanish", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
    "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
    "ru": "Russian", "pl": "Polish", "tr": "Turkish", "ar": "Arabic", "hi": "Hindi",
    "he": "Hebrew", "el": "Greek", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "ms": "Malay", "sw": "Swahili",
}

# Edge TTS fallback voices, one per language. The first three are the ones the
# program has always used.
EDGE_VOICES = {
    "en": "en-US-JennyNeural",   "es": "es-MX-DaliaNeural",     "zh": "zh-CN-XiaoxiaoNeural",
    "ja": "ja-JP-NanamiNeural",  "ko": "ko-KR-SunHiNeural",     "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",   "it": "it-IT-ElsaNeural",      "pt": "pt-BR-FranciscaNeural",
    "nl": "nl-NL-ColetteNeural", "ru": "ru-RU-SvetlanaNeural",  "pl": "pl-PL-ZofiaNeural",
    "tr": "tr-TR-EmelNeural",    "ar": "ar-SA-ZariyahNeural",   "hi": "hi-IN-SwaraNeural",
    "he": "he-IL-HilaNeural",    "el": "el-GR-AthinaNeural",    "sv": "sv-SE-SofieNeural",
    "da": "da-DK-ChristelNeural", "no": "nb-NO-PernilleNeural", "fi": "fi-FI-NooraNeural",
    "ms": "ms-MY-YasminNeural",  "sw": "sw-KE-ZuriNeural",
}

# Languages written without spaces between words: "clear" is counted in characters.
CHARACTER_LANGUAGES = ("zh", "ja")

# Whisper codes that are another name for one of ours.
_WHISPER_ALIASES = {"yue": "zh", "nn": "no", "nb": "no"}


def _is_kana(o: int) -> bool:
    return 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9F


def _is_han(o: int) -> bool:
    return 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF


# (test, language) in order; the first writing system found decides.
_SCRIPTS = (
    (_is_kana, "ja"),                                             # any kana: Japanese (as in lang_detect.py)
    (lambda o: 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F, "ko"),
    (lambda o: 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F, "ar"),
    (lambda o: 0x0590 <= o <= 0x05FF, "he"),
    (lambda o: 0x0400 <= o <= 0x04FF, "ru"),
    (lambda o: 0x0370 <= o <= 0x03FF, "el"),
    (lambda o: 0x0900 <= o <= 0x097F, "hi"),
)


def script_language(text: str) -> str | None:
    """The language the writing system alone shows (ja, ko, ar, he, ru, el, hi,
    then zh for Chinese characters without kana), or None for Latin script."""
    codes = [ord(c) for c in text]
    for test, language in _SCRIPTS:
        if any(test(o) for o in codes):
            return language
    han = any(_is_han(o) for o in codes)
    return "zh" if han else None


def script_characters(text: str, language: str) -> int:
    """How many characters of that language's writing system the text has."""
    if language == "ja":
        return sum(_is_kana(ord(c)) or _is_han(ord(c)) for c in text)
    if language == "zh":
        return sum(_is_han(ord(c)) for c in text)
    return 0


def from_whisper(code: str | None) -> str | None:
    """Whisper's language code as one of ours, or None if Chatterbox cannot speak it."""
    if not code:
        return None
    code = _WHISPER_ALIASES.get(code.lower(), code.lower())
    return code if code in NAMES else None


def spoken_list() -> str:
    """'English, Spanish, ... and Swahili'"""
    names = list(NAMES.values())
    return ", ".join(names[:-1]) + " and " + names[-1]
