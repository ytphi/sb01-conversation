"""
utau_engine.py  -  speak with a UTAU voicebank (no neural model: samples + WORLD)

  pip install pyworld cmudict pykakasi soundfile scipy

The `voice` passed to synthesize() is the voicebank folder (the one with
oto.ini). English banks must use CVVC X-SAMPA aliases (e.g. Kasane Teto
English); Japanese banks use kana CV aliases. The bank language is detected
from its aliases.

Route params (config.yaml voice entries) and their defaults:
  base_pitch: -2      semitones relative to the bank's recorded pitch (speech sits lower than song)
  pitch_hz:   null    or an absolute speaking pitch in Hz (overrides base_pitch)
  speed:      1.0     default speaking rate (a <tone speed=..> plan multiplies it)
  range:      1.0     default pitch movement (a <tone range=..> plan multiplies it)

Voicebank audio is never copied anywhere: the WORLD analysis cache
(~/.cache/sb01-utau) holds derived features only, outside the repo.
"""

import os
import threading
import time

import numpy as np

from g2p_en import text_to_items as en_items
from g2p_ja import text_to_items as ja_items
from oto import Voicebank
from planner import plan_english, plan_japanese
from render import render
from tts_common import Prosody, TTSEngine, pause
from world_cache import WorldCache


class UTAUEngine(TTSEngine):
    name = "utau"
    languages = frozenset({"en", "ja"})

    def __init__(self, sample_rate: int = 24000, cache_dir: str | None = "~/.cache/sb01-utau",
                 sp_dims: int = 60, warmup: bool = True, recursive: bool = False):
        self.cache = WorldCache(fs=sample_rate, sp_dims=sp_dims, cache_dir=cache_dir)
        self.warmup = warmup
        self.recursive = recursive
        self._banks: dict[str, Voicebank] = {}
        self._base_hz: dict[str, float] = {}
        self._warmers: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def load(self):
        import pyworld  # noqa: F401  (fail early if it's missing)

    def bank(self, folder: str) -> Voicebank:
        if not folder:
            raise ValueError("utau: a voice route needs `bank:` (the folder holding oto.ini)")
        folder = os.path.abspath(os.path.expanduser(folder))
        with self._lock:
            if folder not in self._banks:
                bank = Voicebank(folder, recursive=self.recursive)
                self._banks[folder] = bank
                print(f"[utau] {os.path.basename(folder)}: {len(bank.aliases)} aliases, "
                      f"{len(bank.files)} samples ({'ja' if bank.is_japanese else 'en'})")
            return self._banks[folder]

    def supports(self, lang: str, asset: str | None = None) -> bool:
        if asset is None:
            return lang in self.languages
        try:
            return lang == ("ja" if self.bank(asset).is_japanese else "en")
        except FileNotFoundError:
            return False

    def base_hz(self, bank: Voicebank) -> float:
        """The bank's recorded pitch: median voiced f0 over a spread of samples."""
        if bank.folder not in self._base_hz:
            files = bank.files[:: max(1, len(bank.files) // 12)][:12]
            f0s = np.concatenate([self.cache.get(f).f0 for f in files])
            voiced = f0s[f0s > 0]
            self._base_hz[bank.folder] = float(np.median(voiced)) if len(voiced) else 220.0
        return self._base_hz[bank.folder]

    def warm(self, folder: str | None):
        """Analyse the whole bank in the background so first replies aren't slow."""
        if not folder or not self.warmup:
            return
        bank = self.bank(folder)
        if bank.folder in self._warmers:
            return
        todo = [f for f in bank.files if not self.cache.cached(f)]
        if not todo:
            return

        def run():
            t0 = time.time()
            done = self.cache.warm(todo, self._stop)
            print(f"[utau] analysed {done} samples of {os.path.basename(bank.folder)} "
                  f"in {time.time() - t0:.0f}s (cached for next time)")

        print(f"[utau] analysing {len(todo)} samples in the background (one-off)")
        th = threading.Thread(target=run, daemon=True, name="utau-warmup")
        self._warmers[bank.folder] = th
        th.start()

    def synthesize(self, text, lang="en", voice=None, prosody=None,
                   base_pitch: float = -2.0, pitch_hz: float | None = None,
                   speed: float = 1.0, range: float = 1.0, **_ignored):
        bank = self.bank(voice)
        p = Prosody(speed=speed, range=range).merged(prosody)
        if prosody and prosody.speed:
            p.speed = speed * prosody.speed
        if prosody and prosody.range is not None:
            p.range = range * prosody.range
        if bank.is_japanese:
            plan = plan_japanese(ja_items(text, p.emphasis), bank, p)
        else:
            plan = plan_english(en_items(text, p.emphasis), bank, p)
        fs = self.cache.fs
        if not plan.units:
            return pause((p.pause_ms or 200) / 1000, fs), fs
        hz = pitch_hz or self.base_hz(bank) * 2 ** (base_pitch / 12)
        wav = render(plan, self.cache, hz, p)
        if p.pause_ms:
            wav = np.concatenate([wav, pause(p.pause_ms / 1000, fs)])
        return wav, fs
