#!/usr/bin/env python3
"""
run_local.py  -  test the experimental speech framework without the robot (Ubuntu or Windows)

  keyboard input → LLM provider → Chatterbox TTS → PC speakers (and/or .wav files)

Usage:
  python non-robot-testmode/run_local.py                          # type, hear replies
  python non-robot-testmode/run_local.py --text-only              # LLM only, no TTS install needed
  python non-robot-testmode/run_local.py --provider claude --save-wav out/
  python non-robot-testmode/run_local.py --say "こんにちは、元気ですか？"   # TTS only, no LLM
"""

import argparse
import os
import sys
import time

# Windows consoles default to cp1252, which can't print Chinese / Japanese replies
for _stream in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0,os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "experimental", "speech-framework"))

import _paths  # noqa: E402,F401
from bootstrap import add_common_args, build_conversation  # noqa: E402
from conversation import run_conversation  # noqa: E402
from lang_detect import LANGUAGES, detect_language  # noqa: E402
from settings import load_settings  # noqa: E402


class KeyboardInput:
    def listen(self) -> str | None:
        while True:
            try:
                text = input("\nyou> ").strip()
            except EOFError:
                return None
            if text.lower() in {"quit", "exit", "/q"}:
                return None
            if text:
                return text


class TextOnlyOutput:
    def speak(self, text: str, lang: str):
        pass                     # run_conversation already prints the reply


class LocalVoice:
    """Chatterbox → PC speakers via sounddevice, and/or saved .wav files."""

    def __init__(self, synth, play: bool = True, save_dir: str | None = None):
        self.synth    = synth
        self.play     = play
        self.save_dir = save_dir
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

    def speak(self, text: str, lang: str):
        t0 = time.time()
        wav, sr = self.synth.synthesize(text, lang)
        print(f"[tts] {LANGUAGES.get(lang, lang)}: {len(wav) / sr:.1f}s audio in {time.time() - t0:.1f}s")
        if self.save_dir:
            import soundfile as sf
            path = os.path.join(self.save_dir, f"{int(time.time() * 1000)}_{lang}.wav")
            sf.write(path, wav, sr)
            print(f"[tts] saved {path}")
        if self.play:
            import sounddevice as sd
            sd.play(wav, sr)
            sd.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(parser)
    parser.add_argument("--text-only", action="store_true", help="skip TTS entirely")
    parser.add_argument("--no-play", action="store_true", help="don't play audio (use with --save-wav)")
    parser.add_argument("--save-wav", metavar="DIR", help="also write each reply to DIR/*.wav")
    parser.add_argument("--say", metavar="TEXT", help="synthesize TEXT once and exit (no LLM)")
    parser.add_argument("--lang", choices=sorted(LANGUAGES), help="force the language for --say")
    args = parser.parse_args()

    if args.say:
        _paths.load_env()
        from speech_synth import SpeechSynth
        voice = LocalVoice(SpeechSynth(load_settings(args.config)["tts"]),
                           play=not args.no_play, save_dir=args.save_wav)
        voice.speak(args.say, args.lang or detect_language(args.say))
        return

    cfg, conv = build_conversation(args)
    if args.text_only:
        out = TextOnlyOutput()
    else:
        from speech_synth import SpeechSynth
        synth = SpeechSynth(cfg["tts"])
        if cfg["tts"].get("preload", True):
            synth.preload()
        out = LocalVoice(synth, play=not args.no_play, save_dir=args.save_wav)

    try:
        run_conversation(conv, KeyboardInput(), out,
                         greeting=cfg["conversation"].get("greeting"),
                         robot_name=cfg["robot"]["name"])
    except KeyboardInterrupt:
        pass
    print("\nbye")


if __name__ == "__main__":
    main()
