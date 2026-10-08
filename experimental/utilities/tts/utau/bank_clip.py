"""
bank_clip.py  -  a Chatterbox reference clip made from an installed UTAU voicebank

Chatterbox clones a voice from ~10 s of audio. A UTAU bank has hundreds of
short recordings of the singer, so a clip of short slices taken across the bank
gives Chatterbox her timbre, while the intonation comes from the neural model
(no hand-tuned pitch rules). Oct 7 test on 9 Gemini replies: Teto similarity
0.84 (UTAU 0.89, Chatterbox built-in 0.71), Whisper word error 1.3% en / 2.6% ja.

The clip is derived from the bank, so like the bank it is never written into
the repo: it goes to ~/.cache/sb01-utau/clips/ and is rebuilt if missing.
"""

import glob
import hashlib
import os

import numpy as np
import soundfile as sf

CACHE_DIR = "~/.cache/sb01-utau/clips"

# slices that tested best: every Nth sample, M of them, from a to b seconds into each file
PRESETS = {
    "en": dict(step=23, count=24, start=0.05, end=0.75),    # CVVC files: "_ba+_ba+_b-.wav" ...
    "ja": dict(step=7, count=20, start=0.0, end=0.6),       # CV files: one mora each
}


def _is_japanese(folder: str) -> bool:
    names = " ".join(os.path.basename(p) for p in glob.glob(os.path.join(folder, "*.wav"))[:50])
    return any(0x3040 <= ord(c) <= 0x30FF for c in names)


def ensure_clip(bank: str, cache_dir: str = CACHE_DIR) -> str:
    """Path of the reference clip for this bank, building it on first use."""
    bank = os.path.abspath(os.path.expanduser(bank))
    files = sorted(glob.glob(os.path.join(bank, "_*.wav")) or glob.glob(os.path.join(bank, "*.wav")))
    if not files:
        raise FileNotFoundError(f"no .wav samples in {bank}")
    preset = PRESETS["ja" if _is_japanese(bank) else "en"]
    key = hashlib.sha1(f"{bank}|{len(files)}|{sorted(preset.items())}".encode()).hexdigest()[:12]
    out = os.path.join(os.path.expanduser(cache_dir), f"{key}.wav")
    if os.path.isfile(out):
        return out
    parts, sr = [], None
    for path in files[::preset["step"]][:preset["count"]]:
        x, file_sr = sf.read(path, dtype="float32", always_2d=True)
        if sr is None:
            sr = file_sr
        if file_sr != sr:
            continue
        x = x.mean(axis=1)
        parts.append(x[int(preset["start"] * sr):int(preset["end"] * sr)])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    sf.write(out, np.concatenate(parts), sr)
    return out
