#!/usr/bin/env python3
"""
Compare Whisper settings on utterances recorded from the robot mic.

Record first (speak to the robot as you normally would, Ctrl-C when done):
  python3 sb01/whisper_asr.py eno0 --save asr_samples
Then:
  python3 scripts/compare_asr.py asr_samples

For each NNN.wav prints what every setting heard, so you can see which one gets your
speech right: auto language vs forced English, large-v3-turbo vs large-v3.
"""

import argparse
import glob
import os
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from sb01.whisper_asr import PROMPT  # noqa: E402

SETTINGS = [  # (label, model, language)
    ("turbo auto (current)", "large-v3-turbo", None),
    ("turbo en",             "large-v3-turbo", "en"),
    ("large-v3 auto",        "large-v3",       None),
    ("large-v3 en",          "large-v3",       "en"),
]


def load(path: str) -> np.ndarray:
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    args = ap.parse_args()
    paths = sorted(glob.glob(os.path.join(args.dir, "*.wav")))
    if not paths:
        sys.exit(f"no .wav files in {args.dir}")
    clips = {p: load(p) for p in paths}
    results = {p: [] for p in paths}

    from faster_whisper import WhisperModel
    for model_name in dict.fromkeys(m for _, m, _ in SETTINGS):   # one model in GPU memory at a time
        model = WhisperModel(model_name, device="cuda", compute_type="float16")
        for label, m, lang in SETTINGS:
            if m != model_name:
                continue
            for p, audio in clips.items():
                t = time.time()
                segs, info = model.transcribe(audio, language=lang, beam_size=5, initial_prompt=PROMPT,
                                              condition_on_previous_text=False)
                text = " ".join(s.text.strip() for s in segs)
                results[p].append((label, info.language, info.language_probability, time.time() - t, text))
        del model

    for p in paths:
        audio = clips[p]
        print(f"\n{os.path.basename(p)}  ({len(audio) / 16000:.1f}s, peak {np.abs(audio).max():.3f})")
        for label, lang, prob, dt, text in results[p]:
            print(f"  {label:<22} [{lang} {prob:.2f}] {dt:4.2f}s  {text}")


if __name__ == "__main__":
    main()
