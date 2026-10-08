#!/usr/bin/env python3
"""
run_robot.py  -  sb01 voice conversation on the G1 (run on the Ubuntu machine wired to the robot)

  speech input → LLM provider (streamed) → TTS voice, sentence by sentence → G1 speaker (PlayStream)

Speech input (config.yaml → stt.source, or --stt):
  g1-mic  - G1 microphones → Silero VAD + faster-whisper on the PC (default; falls back
            to g1-asr if no robot audio arrives within stt.fallback_after_s)
  pc-mic  - microphone on the PC → Silero VAD + faster-whisper
  g1-asr  - the G1's onboard ASR (rt/audio_msg)

Voice (config.yaml → tts.voice, or --voice; --list-voices shows the registry).
Each turn's timings go to logs/turns.jsonl (conversation.timing_log).

Usage:
  python3 experimental/speech-framework/run_robot.py [interface] [--stt g1-mic|pc-mic|g1-asr]
                                                     [--voice NAME] [--no-stream]
                                                     [--provider gemini|deepseek|claude] [--model ID]
"""

import argparse

import _paths  # noqa: F401  (sets up sys.path for utilities/)
from bootstrap import add_common_args, build_conversation, build_synth, list_voices, make_timing_log
from conversation import run_conversation
from settings import load_settings
from speech_pipeline import SpeechPipeline
from speech_recog import LOCAL_SOURCES, make_local_asr

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from g1_asr import G1ASRInput
from g1_speaker import G1Speaker, LED_LISTENING, LED_THINKING


class RobotVoice:
    """SpeechOutput: TTS voice → 16 kHz PCM → G1 speaker, streamed sentence by sentence."""

    def __init__(self, synth, speaker, default_language: str = "en"):
        self.speaker  = speaker
        self.pipeline = SpeechPipeline(synth, speaker, default_language)
        self.voice_name = synth.voice.name

    def speak(self, text: str, lang: str | None = None):
        self.speak_stream([text], lang)

    def speak_stream(self, sentences, lang: str | None = None, timer=None):
        self.speaker.led(LED_THINKING)
        try:
            self.pipeline.speak_stream(sentences, lang, timer)
        except Exception as exc:
            print(f"[error] speech output: {exc}")
        self.speaker.led(LED_LISTENING)


class RobotEars:
    """SpeechInput: G1 ASR or PC-side STT, LED shows 'thinking' once an utterance is captured."""

    def __init__(self, asr, speaker):
        self.asr     = asr
        self.speaker = speaker

    def listen(self) -> str:
        text = self.asr.listen()
        self.speaker.led(LED_THINKING)
        return text

    def __getattr__(self, name):            # last_speech_end etc. for the timing log
        if name.startswith("last_"):
            return getattr(self.asr, name, None)
        raise AttributeError(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("interface", nargs="?", help="network interface to the G1 (default from config: eno0)")
    parser.add_argument("--stt", choices=("g1-asr",) + LOCAL_SOURCES,
                        help="speech input (default from config: stt.source)")
    add_common_args(parser)
    args = parser.parse_args()
    if args.list_voices:
        return list_voices(load_settings(args.config))

    cfg, conv = build_conversation(args)
    robot_cfg = cfg["robot"]
    name      = robot_cfg["name"]

    synth = build_synth(cfg, args, conv)
    if cfg["tts"].get("preload", True):
        synth.preload()

    interface = args.interface or robot_cfg["network_interface"]
    print(f"[{name}] DDS on {interface}")
    ChannelFactoryInitialize(0, interface)

    speaker = G1Speaker(app_name=name, volume=robot_cfg.get("volume", 100),
                        playback_grace_s=robot_cfg.get("playback_grace_s", 0.5),
                        echo_tail_s=robot_cfg.get("echo_tail_s", 0.3),
                        chunk_s=robot_cfg.get("stream_chunk_s", 1.0),
                        lead_s=robot_cfg.get("stream_lead_s", 1.5))
    speaker.start()

    stt_cfg = cfg["stt"]
    source  = args.stt or stt_cfg["source"]

    def onboard_asr():
        return G1ASRInput(speaker.speaking, speaker.playback_done, topic=robot_cfg["asr_topic"],
                          debounce_s=stt_cfg.get("g1_asr", {}).get("debounce_s", 0.8))

    if source == "g1-asr":
        asr = onboard_asr()
        asr.start()
    else:
        asr = make_local_asr(stt_cfg, source, speaker.speaking)
        asr.start()
        wait_s = stt_cfg.get("fallback_after_s", 5.0)
        if source == "g1-mic" and wait_s and not asr.heard_audio.wait(wait_s):
            print(f"[stt] no G1 mic audio in {wait_s:.0f} s; falling back to the onboard ASR (g1-asr)")
            asr = onboard_asr()
            asr.start()

    voice = RobotVoice(synth, speaker, conv.default_language)
    try:
        run_conversation(conv, RobotEars(asr, speaker), voice,
                         greeting=cfg["conversation"].get("greeting"), robot_name=name,
                         timing_log=make_timing_log(cfg))
    except KeyboardInterrupt:
        print(f"\n[{name}] shutting down...")
        voice.speak("Goodbye!", "en")
    finally:
        speaker.led((0, 0, 0))


if __name__ == "__main__":
    main()
