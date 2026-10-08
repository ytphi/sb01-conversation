"""
world_cache.py  -  WORLD analysis of voicebank samples, cached in memory and on disk

Each sample .wav is analysed once (pyworld: DIO + StoneMask f0, CheapTrick
envelope, D4C aperiodicity) at the engine's sample rate, and stored coded
(mel-cepstrum-like envelope of `sp_dims` coefficients + band aperiodicity) so
a whole bank fits in a few hundred MB on disk and blends linearly.

Disk cache: <cache_dir>/<bank id>/<file id>.npz, keyed by path, size and mtime,
so editing a sample re-analyses it. The cache is derived data: delete it any time.
"""

import hashlib
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import soundfile as sf

FRAME_MS = 5.0


@dataclass
class Features:
    f0: np.ndarray            # (T,) Hz, 0 = unvoiced
    sp: np.ndarray            # (T, sp_dims) coded spectral envelope
    ap: np.ndarray            # (T, bands) coded aperiodicity
    rms: np.ndarray           # (T,) frame energy of the original, for silence trimming


def _load_mono(path: str, fs: int) -> np.ndarray:
    x, sr = sf.read(path, dtype="float64", always_2d=True)
    x = x.mean(axis=1)
    if sr != fs:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(sr, fs)
        x = resample_poly(x, fs // g, sr // g)
    return np.ascontiguousarray(x, dtype=np.float64)


def analyse(path: str, fs: int, sp_dims: int) -> Features:
    import pyworld as pw
    x = _load_mono(path, fs)
    f0, t = pw.dio(x, fs, frame_period=FRAME_MS, f0_floor=70.0, f0_ceil=800.0)
    f0 = pw.stonemask(x, f0, t, fs)
    sp = pw.cheaptrick(x, f0, t, fs)
    ap = pw.d4c(x, f0, t, fs)
    hop = int(fs * FRAME_MS / 1000)
    pad = np.pad(x, (0, len(t) * hop + hop))
    rms = np.sqrt(np.mean(pad[:len(t) * hop].reshape(len(t), hop) ** 2, axis=1) + 1e-12)
    return Features(f0.astype(np.float32),
                    pw.code_spectral_envelope(sp, fs, sp_dims).astype(np.float32),
                    pw.code_aperiodicity(ap, fs).astype(np.float32),
                    rms.astype(np.float32))


class WorldCache:
    def __init__(self, fs: int = 24000, sp_dims: int = 60, cache_dir: str | None = None,
                 max_in_memory: int = 600):
        self.fs        = fs
        self.sp_dims   = sp_dims
        self.cache_dir = os.path.expanduser(cache_dir) if cache_dir else None
        self.max_in_memory = max_in_memory
        self._mem: OrderedDict[str, Features] = OrderedDict()
        self._lock = threading.Lock()
        self._file_locks: dict[str, threading.Lock] = {}

    def _disk_path(self, wav: str) -> str | None:
        if not self.cache_dir:
            return None
        st = os.stat(wav)
        bank = hashlib.sha1(os.path.dirname(os.path.abspath(wav)).encode()).hexdigest()[:12]
        key = f"{os.path.basename(wav)}|{st.st_size}|{int(st.st_mtime)}|{self.fs}|{self.sp_dims}"
        name = hashlib.sha1(key.encode()).hexdigest()[:20] + ".npz"
        return os.path.join(self.cache_dir, f"{bank}-{self.fs}", name)

    def cached(self, wav: str) -> bool:
        if wav in self._mem:
            return True
        path = self._disk_path(wav)
        return bool(path and os.path.isfile(path))

    def get(self, wav: str) -> Features:
        with self._lock:
            feats = self._mem.get(wav)
            if feats is not None:
                self._mem.move_to_end(wav)
                return feats
            file_lock = self._file_locks.setdefault(wav, threading.Lock())
        with file_lock:                               # one analysis per file, even across threads
            with self._lock:
                if wav in self._mem:
                    return self._mem[wav]
            feats = self._load_or_analyse(wav)
            with self._lock:
                self._mem[wav] = feats
                while len(self._mem) > self.max_in_memory:
                    self._mem.popitem(last=False)
            return feats

    def _load_or_analyse(self, wav: str) -> Features:
        path = self._disk_path(wav)
        if path and os.path.isfile(path):
            try:
                with np.load(path) as z:
                    return Features(z["f0"], z["sp"], z["ap"], z["rms"])
            except Exception:
                pass                                   # corrupt / partial file: redo it
        feats = analyse(wav, self.fs, self.sp_dims)
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + f".{os.getpid()}.tmp.npz"
            np.savez(tmp, f0=feats.f0, sp=feats.sp, ap=feats.ap, rms=feats.rms)
            os.replace(tmp, path)
        return feats

    def warm(self, wavs: list[str], stop: threading.Event | None = None) -> int:
        """Analyse every file not cached on disk yet (run in a background thread)."""
        done = 0
        for wav in wavs:
            if stop is not None and stop.is_set():
                break
            if not self.cached(wav):
                try:
                    self._load_or_analyse(wav)
                    done += 1
                except Exception as exc:
                    print(f"[utau] could not analyse {os.path.basename(wav)}: {exc}")
        return done
