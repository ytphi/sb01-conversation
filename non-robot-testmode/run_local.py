#!/usr/bin/env python3
"""
run_local.py  -  test the experimental speech framework without the robot (Ubuntu or Windows)

  keyboard (or PC mic + Whisper) → LLM provider (streamed) → TTS voice → PC speakers (and/or .wav files)

Usage:
  python non-robot-testmode/run_local.py                          # type, hear replies
  python non-robot-testmode/run_local.py --text-only              # LLM only, no TTS install needed
  python non-robot-testmode/run_local.py --mic                    # talk instead of type
  python non-robot-testmode/run_local.py --provider claude --save-wav out/
  python non-robot-testmode/run_local.py --say "こんにちは、元気ですか？"   # TTS only, no LLM
  python non-robot-testmode/run_local.py --voice teto --say "<tone question>Really? You built it?"
  python non-robot-testmode/run_local.py --list-voices
"""

import argparse
import os
import queue
import sys
import threading
import time

# Windows consoles default to cp1252, which can't print Chinese / Japanese replies
for _stream in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0,os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "experimental", "speech-framework"))

import _paths  # noqa: E402,F401
from bootstrap import (add_common_args, build_conversation, build_synth,  # noqa: E402
                       list_voices, make_timing_log)
from conversation import run_conversation  # noqa: E402
from lang_detect import LANGUAGES  # noqa: E402
from settings import load_settings  # noqa: E402
from speech_pipeline import SpeechPipeline  # noqa: E402


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


class LocalPlayer:
    """AudioPlayer: PC speakers via sounddevice (one sentence after another), and/or .wav files."""

    def __init__(self, play: bool = True, save_dir: str | None = None):
        self.play_audio = play
        self.save_dir   = save_dir
        self.speaking   = threading.Event()      # mic input ignores audio while set
        self._q: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._parts: list = []
        self._timer = None
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

    def begin(self, timer=None):
        self.speaking.set()
        self._timer, self._parts = timer, []
        self._q = queue.Queue()
        self._thread = threading.Thread(target=self._loop, args=(self._q,), daemon=True)
        self._thread.start()

    def play(self, wav, sr):
        self._parts.append((wav, sr))
        self._q.put((wav, sr))

    def finish(self):
        self._q.put(None)
        self._thread.join()
        if self.save_dir and self._parts:
            import numpy as np
            import soundfile as sf
            sr = self._parts[0][1]
            wav = np.concatenate([w for w, r in self._parts if r == sr])
            path = os.path.join(self.save_dir, f"{int(time.time() * 1000)}.wav")
            sf.write(path, wav, sr)
            print(f"[tts] saved {path}")
        if self.play_audio:
            time.sleep(0.3)                      # let the room echo die
        self.speaking.clear()

    def _loop(self, q):
        first = True
        while True:
            item = q.get()
            if item is None:
                return
            if first and self._timer:
                self._timer.mark("first_audio_played")
            first = False
            if self.play_audio:
                import sounddevice as sd
                sd.play(*item)
                sd.wait()


class LocalVoice:
    """SpeechOutput over SpeechPipeline + LocalPlayer."""

    def __init__(self, synth, play: bool = True, save_dir: str | None = None,
                 default_language: str = "en"):
        self.player   = LocalPlayer(play, save_dir)
        self.speaking = self.player.speaking
        self.voice_name = synth.voice.name

        def show(text, lang):
            print(f"[tts] {LANGUAGES.get(lang, lang)}: {text}")
        self.pipeline = SpeechPipeline(synth, self.player, default_language, on_sentence=show)

    def speak(self, text: str, lang: str | None = None):
        t0 = time.time()
        self.pipeline.speak(text, lang)
        print(f"[tts] done in {time.time() - t0:.1f}s")

    def speak_stream(self, sentences, lang: str | None = None, timer=None):
        self.pipeline.speak_stream(sentences, lang, timer)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(parser)
    parser.add_argument("--text-only", action="store_true", help="skip TTS entirely")
    parser.add_argument("--mic", action="store_true",
                        help="speak into the PC mic (Silero VAD + faster-whisper) instead of typing")
    parser.add_argument("--no-play", action="store_true", help="don't play audio (use with --save-wav)")
    parser.add_argument("--save-wav", metavar="DIR", help="also write each reply to DIR/*.wav")
    parser.add_argument("--say", metavar="TEXT", help="synthesize TEXT once and exit (no LLM)")
    parser.add_argument("--lang", choices=sorted(LANGUAGES), help="force the language for --say")
    args = parser.parse_args()
    if args.list_voices:
        return list_voices(load_settings(args.config))

    if args.say:
        _paths.load_env()
        cfg = load_settings(args.config)
        voice = LocalVoice(build_synth(cfg, args), play=not args.no_play, save_dir=args.save_wav)
        voice.speak(args.say, args.lang)
        return

    cfg, conv = build_conversation(args)
    if args.text_only:
        out = TextOnlyOutput()
    else:
        synth = build_synth(cfg, args, conv)
        if cfg["tts"].get("preload", True):
            synth.preload()
        out = LocalVoice(synth, play=not args.no_play, save_dir=args.save_wav,
                         default_language=conv.default_language)

    if args.mic:
        from speech_recog import make_local_asr
        inp = make_local_asr(cfg["stt"], "pc-mic", getattr(out, "speaking", threading.Event()))
        inp.start()
    else:
        inp = KeyboardInput()

    try:
        run_conversation(conv, inp, out,
                         greeting=cfg["conversation"].get("greeting"),
                         robot_name=cfg["robot"]["name"],
                         timing_log=make_timing_log(cfg))
    except KeyboardInterrupt:
        pass
    print("\nbye")


if __name__ == "__main__":
    main()
