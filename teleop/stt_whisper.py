"""
stt_whisper.py  -  speech recognition on the PC for scripts/sb01_conversation.py

Uses the speech framework in experimental/speech-framework as it is: the same
config.yaml (and config.local.yaml), the same Silero VAD end-of-turn detection
and the same faster-whisper engine as experimental/speech-framework/run_robot.py.

Where the speech comes from (stt.source in config.yaml, or SB01_STT):
  g1-mic   the robot's microphones, streamed to the PC  -> Silero VAD -> Whisper   (default)
  pc-mic   a microphone plugged into the PC              -> Silero VAD -> Whisper
  g1-asr   the robot's own onboard recognizer (rt/audio_msg), as before; Whisper is not loaded

Environment:
  SB01_STT                g1-mic | pc-mic | g1-asr     overrides stt.source
  SB01_WHISPER_MODEL      e.g. small, large-v3-turbo  overrides stt.whisper.model
  SB01_WHISPER_LANGUAGE   e.g. en (or "auto")         overrides stt.whisper.language

Whisper's settings (model, device, language, beam size) and the VAD settings
(threshold, min_silence_ms, ...) are read from config.yaml. Nothing here
changes them except the three overrides above.

If anything is missing (faster-whisper, silero-vad, the framework folder),
STTUnavailable says what, and the conversation uses the onboard recognizer.
"""

import importlib
import os
import sys
import threading

REPO_ROOT     = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FRAMEWORK_DIR = os.path.join(REPO_ROOT, "experimental", "speech-framework")
INSTALL_HINT  = "pip install faster-whisper silero-vad pyyaml"

LOCAL_SOURCES = ("g1-mic", "pc-mic")
SOURCES       = LOCAL_SOURCES + ("g1-asr",)

# The timings of experimental/speech-framework, used when config.yaml cannot be read.
DEFAULT_TIMINGS = {"debounce_s": 0.8, "playback_grace_s": 0.5, "echo_tail_s": 0.3}


class STTUnavailable(Exception):
    """PC-side speech recognition cannot be used; the message says why."""


def _framework():
    """Put the framework's folders on the import path and return its settings module."""
    if not os.path.isdir(FRAMEWORK_DIR):
        raise STTUnavailable("experimental/speech-framework is not in this copy of the project")
    if FRAMEWORK_DIR not in sys.path:
        sys.path.insert(0, FRAMEWORK_DIR)
    try:
        importlib.import_module("_paths")          # puts experimental/utilities/* on the path
        return importlib.import_module("settings")
    except ModuleNotFoundError as exc:
        raise STTUnavailable(f"a Python package is missing ({exc.name}): {INSTALL_HINT}") from exc


def load_config() -> dict:
    """The framework's full config (config.yaml merged with config.local.yaml), or {}."""
    try:
        return _framework().load_settings() or {}
    except Exception:
        return {}


def timings(config: dict | None = None) -> dict:
    """debounce_s, playback_grace_s, echo_tail_s from config.yaml, else the framework defaults."""
    config = load_config() if config is None else config
    robot = config.get("robot") or {}
    g1_asr = (config.get("stt") or {}).get("g1_asr") or {}
    out = dict(DEFAULT_TIMINGS)
    for key, value in (("debounce_s", g1_asr.get("debounce_s")),
                       ("playback_grace_s", robot.get("playback_grace_s")),
                       ("echo_tail_s", robot.get("echo_tail_s"))):
        if isinstance(value, (int, float)) and value >= 0:
            out[key] = float(value)
    return out


def chosen_source(config: dict | None = None) -> str:
    """SB01_STT if set, else stt.source from config.yaml, else g1-mic."""
    config = load_config() if config is None else config
    source = os.environ.get("SB01_STT", "").strip().lower()
    if not source:
        source = str((config.get("stt") or {}).get("source") or "g1-mic").strip().lower()
    return source


def stt_settings(config: dict) -> dict:
    """The stt section, with the environment overrides applied."""
    stt = dict(config.get("stt") or {})
    whisper = dict(stt.get("whisper") or {})
    model = os.environ.get("SB01_WHISPER_MODEL", "").strip()
    if model:
        whisper["model"] = model
    language = os.environ.get("SB01_WHISPER_LANGUAGE", "").strip().lower()
    if language:
        whisper["language"] = None if language in ("auto", "none", "null") else language
    stt["whisper"] = whisper
    return stt


def start_local_asr(source: str, speaking: threading.Event, config: dict | None = None):
    """Build and start the framework's LocalASRInput (VAD + Whisper) on `source`.

    Audio heard while `speaking` is set is thrown away, so the robot does not
    transcribe itself. Returns the started input; its listen() blocks until the
    next utterance and returns its text. Raises STTUnavailable with the reason.
    """
    if source not in LOCAL_SOURCES:
        raise STTUnavailable(f"{source!r} is not a PC-side source ({' or '.join(LOCAL_SOURCES)})")
    settings = _framework()
    config = settings.load_settings() if config is None else config
    stt = stt_settings(config)
    try:
        speech_recog = importlib.import_module("speech_recog")
        asr = speech_recog.make_local_asr(stt, source, speaking)
        _record_language(asr.engine)
        asr.start()                     # loads Whisper and Silero VAD, opens the audio source
    except ModuleNotFoundError as exc:
        raise STTUnavailable(f"a Python package is missing ({exc.name}): {INSTALL_HINT}") from exc
    except Exception as exc:
        raise STTUnavailable(f"{type(exc).__name__}: {exc}") from exc
    return asr


def _record_language(engine):
    """Make the framework's Whisper engine remember the language Whisper detected
    for the last utterance, and how sure it was: engine.last_language =
    (code, probability). Whisper's call is wrapped, not changed, so the text and
    the framework's own filtering are exactly as before."""
    engine.last_language = None
    load = engine.load

    def load_and_wrap():
        load()
        model = engine.model
        if model is None or getattr(model, "_sb01_records_language", False):
            return
        transcribe = model.transcribe

        def transcribe_and_record(*args, **kwargs):
            segments, info = transcribe(*args, **kwargs)
            engine.last_language = (getattr(info, "language", None),
                                    float(getattr(info, "language_probability", 0.0) or 0.0))
            return segments, info

        model.transcribe = transcribe_and_record
        model._sb01_records_language = True

    engine.load = load_and_wrap


def last_language(asr) -> tuple | None:
    """(language code, probability) Whisper detected for the utterance just
    returned by asr.listen(), or None if it is not known."""
    return getattr(getattr(asr, "engine", None), "last_language", None)


def describe(config: dict) -> str:
    stt = stt_settings(config)
    whisper = stt.get("whisper") or {}
    vad = stt.get("vad") or {}
    return (f"Whisper {whisper.get('model', 'small')} on {whisper.get('device', 'auto')}, "
            f"language {whisper.get('language') or 'auto-detect'}, "
            f"end of turn after {vad.get('min_silence_ms', 500)} ms of silence")
