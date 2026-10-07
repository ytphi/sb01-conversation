#!/usr/bin/env python3
"""
run_robot.py  -  sb01 voice conversation on the G1 (run on the Ubuntu machine wired to the robot)

  speech input → LLM provider → Chatterbox TTS → G1 speaker (PlayStream)

Speech input (config.yaml → stt.source, or --stt):
  g1-mic  - G1 microphones → Silero VAD + faster-whisper on the PC (default)
  pc-mic  - microphone on the PC → Silero VAD + faster-whisper
  g1-asr  - the G1's onboard ASR (rt/audio_msg)

Usage:
  python3 experimental/speech-framework/run_robot.py [interface] [--stt g1-mic|pc-mic|g1-asr]
                                                     [--provider gemini|deepseek|claude] [--model ID]
"""

import argparse
import time

import _paths  # noqa: F401  (sets up sys.path for utilities/)
from bootstrap import add_common_args, build_conversation
from conversation import run_conversation
from speech_recog import LOCAL_SOURCES, make_local_asr
from speech_synth import SpeechSynth
from tts_common import to_pcm16

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from g1_asr import G1ASRInput
from g1_speaker import G1Speaker, LED_LISTENING, LED_OFF, LED_THINKING


class RobotVoice:
    """SpeechOutput: Chatterbox → 16 kHz PCM → G1 speaker."""

    def __init__(self, synth, speaker):
        self.synth   = synth
        self.speaker = speaker

    def speak(self, text: str, lang: str):
        self.speaker.led(LED_THINKING)
        try:
            t0 = time.time()
            wav, sr = self.synth.synthesize(text, lang)
            print(f"[timing] tts {time.time() - t0:.2f}s for {len(wav) / sr:.1f}s of audio")
            self.speaker.play_pcm(to_pcm16(wav, sr))
        except Exception as exc:
            print(f"[error] TTS: {exc}")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("interface", nargs="?", help="network interface to the G1 (default from config: eno0)")
    parser.add_argument("--stt", choices=("g1-asr",) + LOCAL_SOURCES,
                        help="speech input (default from config: stt.source)")
    add_common_args(parser)
    args = parser.parse_args()

    cfg, conv = build_conversation(args)
    robot_cfg = cfg["robot"]
    name      = robot_cfg["name"]

    synth = SpeechSynth(cfg["tts"])
    if cfg["tts"].get("preload", True):
        synth.preload()

    interface = args.interface or robot_cfg["network_interface"]
    print(f"[{name}] DDS on {interface}")
    ChannelFactoryInitialize(0, interface)

    speaker = G1Speaker(app_name=name, volume=robot_cfg.get("volume", 100),
                        playback_grace_s=robot_cfg.get("playback_grace_s", 0.5),
                        echo_tail_s=robot_cfg.get("echo_tail_s", 0.3))
    speaker.start()

    stt_cfg = cfg["stt"]
    source  = args.stt or stt_cfg["source"]
    if source == "g1-asr":
        asr = G1ASRInput(speaker.speaking, speaker.playback_done, topic=robot_cfg["asr_topic"],
                         debounce_s=stt_cfg.get("g1_asr", {}).get("debounce_s", 0.8))
    else:
        asr = make_local_asr(stt_cfg, source, speaker.speaking)
    asr.start()

    try:
        run_conversation(conv, RobotEars(asr, speaker), RobotVoice(synth, speaker),
                         greeting=cfg["conversation"].get("greeting"), robot_name=name)
    except KeyboardInterrupt:
        print(f"\n[{name}] shutting down...")
        RobotVoice(synth, speaker).speak("Goodbye!", "en")
    finally:
        speaker.led((0, 0, 0))


if __name__ == "__main__":
    main()
