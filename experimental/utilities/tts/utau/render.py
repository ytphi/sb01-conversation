"""
render.py  -  turn a Plan into audio with WORLD

Every planned sample is mapped onto the output timeline (its fixed consonant
region at natural speed, the rest stretched or squeezed to fill its slot), and
its cached WORLD frames are blended frame by frame with the neighbouring
samples across their overlaps. f0 is replaced by the planned pitch curve (plus
a little of the sample's own micro-variation), then the whole phrase is
synthesised in one pass, which avoids clicks at the joins.
"""

import numpy as np

from planner import FRAME, Plan, Unit, pitch_curve
from tts_common import Prosody
from world_cache import FRAME_MS, WorldCache

MICRO_ST = 0.6                # keep at most this many semitones of the sample's own f0 wobble


def _local_time(u: Unit, r: np.ndarray) -> np.ndarray:
    """Output time since unit start → time since the sample's oto offset."""
    natural = u.natural
    out_len = max(u.end - u.start, 1e-3)
    fixed = float(np.clip(max(u.entry.consonant, u.entry.preutter) - u.local_start, 0.0, natural))
    if not u.stretch or out_len <= fixed or natural - fixed < 0.01:
        local = np.minimum(r, natural)
    else:
        ratio = (natural - fixed) / (out_len - fixed)
        local = np.where(r < fixed, r, fixed + (r - fixed) * ratio)
    return u.local_start + np.clip(local, 0.0, natural)


def render(plan: Plan, cache: WorldCache, base_hz: float, prosody: Prosody,
           fft_size: int | None = None) -> np.ndarray:
    import pyworld as pw
    fs = cache.fs
    n = int(np.ceil(plan.duration / FRAME)) + 1
    t = np.arange(n) * FRAME
    target = base_hz * 2.0 ** (pitch_curve(plan, prosody, n) / 12.0)

    sp_dims = cache.sp_dims
    acc_sp = np.zeros((n, sp_dims))
    acc_ap = None
    acc_w = np.zeros(n)
    acc_v = np.zeros(n)
    acc_e = np.zeros(n)
    acc_micro = np.zeros(n)
    acc_mw = np.zeros(n)

    units = sorted(plan.units, key=lambda u: u.start)
    for i, u in enumerate(units):
        feats = cache.get(u.entry.wav)
        i0, i1 = int(np.floor(u.start / FRAME)), int(np.ceil(u.end / FRAME))
        i0, i1 = max(i0, 0), min(i1, n)
        if i1 <= i0:
            continue
        tt = t[i0:i1]
        local = _local_time(u, tt - u.start)
        src = (u.entry.offset + local) / (FRAME_MS / 1000)
        idx = np.clip(np.round(src).astype(int), 0, len(feats.f0) - 1)

        # crossfade weights: fade in over this sample's overlap, out over the next one's
        ovl_in = u.entry.overlap
        nxt = next((v for v in units[i + 1:] if v.phrase == u.phrase), None)
        ovl_out = (u.end - nxt.start) if nxt is not None else 0.0
        ovl_out = ovl_out if ovl_out > 0.005 else 0.008     # gap or no overlap: short fade
        w = np.ones(len(tt))
        if not u.first_in_phrase and ovl_in > 0.005:
            w *= np.clip((tt - u.start) / ovl_in, 0.0, 1.0)
        w *= np.clip((u.end - tt) / ovl_out, 0.0, 1.0)

        f0 = feats.f0[idx]
        voiced = f0 > 0
        acc_sp[i0:i1] += w[:, None] * feats.sp[idx]
        ap = feats.ap[idx]
        if acc_ap is None:
            acc_ap = np.zeros((n, ap.shape[1]))
        acc_ap[i0:i1] += w[:, None] * ap
        energy = feats.rms[np.clip(idx, 0, len(feats.rms) - 1)]
        acc_v[i0:i1] += w * energy * voiced          # loud frames decide voicing (stop closures stay unvoiced)
        acc_e[i0:i1] += w * energy
        acc_w[i0:i1] += w
        if voiced.sum() >= 3:
            med = np.median(f0[voiced])
            micro = np.clip(12 * np.log2(np.where(voiced, f0, med) / med), -MICRO_ST, MICRO_ST)
            acc_micro[i0:i1] += w * voiced * micro
            acc_mw[i0:i1] += w * voiced

    present = acc_w > 1e-4
    safe_w = np.where(present, acc_w, 1.0)
    coded_sp = acc_sp / safe_w[:, None]
    coded_ap = (acc_ap if acc_ap is not None else np.zeros((n, 1))) / safe_w[:, None]
    voiced = present & (acc_v / np.maximum(acc_e, 1e-9) > 0.5)
    micro = np.where(acc_mw > 1e-4, acc_micro / np.maximum(acc_mw, 1e-4), 0.0)
    f0 = np.where(voiced, target * 2.0 ** (micro / 12.0), 0.0)

    fft_size = fft_size or pw.get_cheaptrick_fft_size(fs)
    sp = pw.decode_spectral_envelope(np.ascontiguousarray(coded_sp), fs, fft_size)
    ap = pw.decode_aperiodicity(np.ascontiguousarray(coded_ap), fs, fft_size)
    gain = np.clip(acc_w, 0.0, 1.0) ** 2           # power gain where samples fade to silence
    sp *= gain[:, None]
    sp[~present] = 1e-16
    ap[~present] = 1.0
    wav = pw.synthesize(np.ascontiguousarray(f0), sp, np.ascontiguousarray(ap), fs, FRAME_MS)
    return _normalise(wav.astype(np.float32), fs)


def _normalise(wav: np.ndarray, fs: int, target_rms_db: float = -20.0) -> np.ndarray:
    if not len(wav):
        return wav
    rms = np.sqrt(np.mean(wav ** 2) + 1e-12)
    wav = wav * (10 ** (target_rms_db / 20) / rms)
    peak = np.max(np.abs(wav))
    if peak > 0.95:
        wav *= 0.95 / peak
    fade = min(int(0.01 * fs), len(wav) // 2)
    if fade:
        wav[:fade] *= np.linspace(0, 1, fade)
        wav[-fade:] *= np.linspace(1, 0, fade)
    return wav
