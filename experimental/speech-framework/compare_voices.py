#!/usr/bin/env python3
"""
compare_voices.py  -  render the same test lines in every registered voice, side by side

  python3 experimental/speech-framework/compare_voices.py                 # all voices → out/voices/
  python3 experimental/speech-framework/compare_voices.py --voices teto testmp --play
  python3 experimental/speech-framework/compare_voices.py --lines my_lines.txt

Writes out/voices/<voice>/<nn>_<lang>.wav (gitignored, next to non-robot-testmode/out)
and prints how long each line took to synthesize. Lines may carry reply tags,
e.g. "<tone question>Did you build it yourself?". A line starting with "ja:",
"zh:" or "es:" forces that language.
"""

import argparse
import os
import time

import _paths
from lang_detect import detect_language
from reply_tags import parse_sentence, split_sentences
from settings import load_settings

DEFAULT_LINES = [
    "Hi! I'm sb01, a robot at Cal State San Bernardino. Nice to meet you.",
    "<tone question>Really? You built that yourself?",
    "<tone fall speed=0.9>Please stand back a little while I move my arms.",
    "<tone rise pitch=+2>That's amazing, thank you so much!",
    "ja:こんにちは、私はロボットです。今日はいい天気ですね。",
    "zh:你好，我是机器人。很高兴认识你。",
    "es:¡Hola! Soy un robot. ¿Cómo estás?",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--voices", nargs="*", help="voice names (default: all)")
    parser.add_argument("--lines", help="text file, one line per test sentence")
    parser.add_argument("--out", default=os.path.join(_paths.REPO_ROOT, "non-robot-testmode", "out", "voices"))
    parser.add_argument("--play", action="store_true", help="also play each render")
    parser.add_argument("--config", help="extra YAML file merged over config.yaml")
    args = parser.parse_args()

    import soundfile as sf
    from speech_synth import SpeechSynth

    cfg = load_settings(args.config)
    lines = DEFAULT_LINES
    if args.lines:
        with open(args.lines, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    synth = SpeechSynth(cfg["tts"])
    names = args.voices or synth.registry.names()
    for name in names:
        synth.set_voice(name)
        folder = os.path.join(args.out, name)
        os.makedirs(folder, exist_ok=True)
        print(f"\n== {name}: {synth.voice.description}")
        for i, line in enumerate(lines):
            lang = None
            if len(line) > 3 and line[2] == ":" and line[:2] in ("en", "ja", "zh", "es"):
                lang, line = line[:2], line[3:]
            import numpy as np
            parts, sr, t0 = [], 24000, time.time()
            for sentence in split_sentences(line):
                s_lang = lang or detect_language(sentence)
                for ph in parse_sentence(sentence, s_lang):
                    if ph.text:
                        wav, sr = synth.synthesize(ph.text, s_lang, ph.prosody, tts_text=ph.tts_text)
                        parts.append(wav)
            dt = time.time() - t0
            if not parts:
                continue
            wav = np.concatenate(parts)
            path = os.path.join(folder, f"{i:02d}_{lang or detect_language(line)}.wav")
            sf.write(path, wav, sr)
            print(f"  {dt:5.2f}s for {len(wav) / sr:4.1f}s  {os.path.relpath(path)}")
            if args.play:
                import sounddevice as sd
                sd.play(wav, sr)
                sd.wait()


if __name__ == "__main__":
    main()
