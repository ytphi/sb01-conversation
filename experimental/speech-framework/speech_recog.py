"""
speech_recog.py  -  build PC-side speech recognition from config.yaml → stt

Sources:
  g1-mic  - the robot's microphones, streamed to the PC (UDP multicast)
  pc-mic  - a microphone plugged into the PC
(The third option, g1-asr = the robot's onboard ASR, is built in run_robot.py.)
"""

import threading

from stt_common import LocalASRInput, PcMicSource

LOCAL_SOURCES = ("g1-mic", "pc-mic")


def _make_source(kind: str, stt_cfg: dict):
    if kind == "g1-mic":
        from g1_mic import G1MicSource
        return G1MicSource(**stt_cfg.get("g1_mic", {}))
    if kind == "pc-mic":
        return PcMicSource(**stt_cfg.get("pc_mic", {}))
    raise ValueError(f"unknown audio source {kind!r} (expected one of {LOCAL_SOURCES})")


def _make_engine(stt_cfg: dict):
    kind = stt_cfg.get("engine", "faster-whisper")
    if kind == "faster-whisper":
        from whisper_stt import FasterWhisper
        return FasterWhisper(**stt_cfg.get("whisper", {}))
    raise ValueError(f"unknown STT engine {kind!r} (expected 'faster-whisper')")


def make_local_asr(stt_cfg: dict, source: str, speaking: threading.Event) -> LocalASRInput:
    return LocalASRInput(_make_source(source, stt_cfg), _make_engine(stt_cfg), speaking,
                         **stt_cfg.get("vad", {}))
