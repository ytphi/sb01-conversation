"""
planner.py  -  syllables / morae → a timed list of voicebank samples + a pitch curve

Timing follows UTAU: every sample has an anchor (the output time its
preutterance point plays at). Syllable nuclei (CV, VV, "- CV") anchor on the
syllable's vowel onset; VC / CC transition samples are chained backwards from
the next nucleus so they end inside its overlap; phrase-final "V -" samples
anchor on the end of the last vowel. Samples are trimmed at the front when the
syllable is too short for their lead-in.

Pitch is planned in semitones relative to the voice's speaking pitch:
  declination over each phrase + a bump on stressed (or emphasised) syllables
  + a phrase-final contour: fall (statement), rise / question, flat,
  or a small continuation rise before a comma.
A Prosody plan from the LLM sets the contour, pitch offset, range, speed and
emphasis; anything it leaves out uses these rules.
"""

from dataclasses import dataclass, field

import numpy as np

from g2p_en import Pause, Syllable
from g2p_ja import Mora, vowel_of
from oto import OtoEntry, Voicebank
from tts_common import Prosody

FRAME = 0.005
LEAD  = 0.06                  # silence before the first sample
MIN_VOWEL = 0.045             # a transition sample may not start sooner than this after a nucleus
MORA_S = 0.15                 # Japanese mora length at speed 1 (s); 2% kana error in Whisper read-back
KEEP_BEFORE_ONSET = 0.03      # vowel kept in front of a VC sample's consonant when it is trimmed

# English timing at speed 1 (seconds): vowel nucleus by stress, plus time per
# consonant between two vowels. Tuned by Whisper read-back
# (Oct 7: 4.5% word error over 12 test sentences at these values; at 0.17/0.14/0.10/0.04
# it was 25%, the CVVC samples need room for their consonants.)
TIMING = {"stress1": 0.22, "stress2": 0.17, "stress0": 0.13, "diphthong": 0.03,
          "consonant": 0.07, "final": 1.45, "emph": 1.3}
DIPHTHONGS = {"aI", "aU", "eI", "oU", "OI"}


@dataclass
class Unit:
    entry: OtoEntry
    start: float                         # output time of the (possibly trimmed) sample start
    local_start: float = 0.0             # seconds trimmed off the sample's start
    end: float | None = None             # output end; None = until the next unit's overlap
    stretch: bool = True
    phrase: int = 0
    first_in_phrase: bool = False

    @property
    def natural(self) -> float:
        return max(self.entry.length - self.local_start, 0.0)


@dataclass
class Beat:
    """One syllable / mora for the pitch planner."""
    t: float
    dur: float
    accent: float                        # semitones of bump (before range scaling)
    phrase: int


@dataclass
class PhraseInfo:
    t0: float
    t1: float
    kind: str                            # comma | sentence | question | exclaim | end


@dataclass
class Plan:
    units: list[Unit] = field(default_factory=list)
    beats: list[Beat] = field(default_factory=list)
    phrases: list[PhraseInfo] = field(default_factory=list)
    duration: float = 0.0


def _place_chain_backwards(units: list[OtoEntry], next_start: float, next_ovl: float,
                           floor: float, phrase: int) -> list[Unit]:
    out = []
    for e in reversed(units):
        end = next_start + max(next_ovl, 0.01)
        start = end - e.length
        ls = 0.0
        if start < floor:
            # UTAU auto-fit: drop lead-in vowel, but always keep the consonant onset
            # (the preutterance point) and a little vowel before it
            ls = min(floor - start, max(e.preutter - KEEP_BEFORE_ONSET, 0.0))
            start += ls
        if e.length - ls < 0.03:
            continue
        out.append(Unit(e, start, ls, end, stretch=False, phrase=phrase))
        next_start, next_ovl = start, e.overlap
    return list(reversed(out))


def _nucleus(entry: OtoEntry, t: float, floor: float, phrase: int, first: bool = False) -> Unit:
    start = t - entry.preutter
    ls = 0.0
    if start < floor:
        ls, start = min(floor - start, max(entry.preutter - 0.01, 0.0)), max(start, floor)
        start = t - (entry.preutter - ls)
    return Unit(entry, start, ls, None, True, phrase, first)


def _split_phrases(items):
    phrases, cur = [], []
    for it in items:
        if isinstance(it, Pause):
            if cur:
                phrases.append((cur, it))
                cur = []
            elif phrases:                # back-to-back pauses: lengthen the previous one
                prev = phrases[-1][1]
                phrases[-1] = (phrases[-1][0], Pause(prev.ms + it.ms, it.kind))
        else:
            cur.append(it)
    if cur:
        phrases.append((cur, None))
    return phrases


# ── English (CVVC, X-SAMPA aliases) ─────────────────────────────────────────

def _syl_duration(s: Syllable, final: bool) -> float:
    d = TIMING[f"stress{s.stress}"] if s.stress in (0, 1, 2) else TIMING["stress0"]
    if s.vowel in DIPHTHONGS:
        d += TIMING["diphthong"]
    if s.emph:
        d *= TIMING["emph"]
    if final:
        d *= TIMING["final"]
    return d


def _accent(s: Syllable) -> float:
    a = {1: 1.8, 2: 0.8}.get(s.stress, 0.0)
    return a + (2.5 if s.emph and s.stress else 0.0)


def _cv(bank: Voicebank, onset: list[str], vowel: str, start: bool):
    """Pre-units + nucleus entry for an onset cluster and vowel."""
    pre: list[OtoEntry] = []
    head = "- " if start else ""
    if not onset:
        return pre, bank.pick(f"- {vowel}" if start else vowel, vowel, f"- {vowel}")
    cons = list(onset)
    if cons[-1] == "j" and vowel == "u" and len(cons) >= 2 and f"{cons[-2]}ju" in bank:
        cv = f"{cons[-2]}ju"
        cons = cons[:-1]
    else:
        cv = f"{cons[-1]}{vowel}"
    if len(cons) > 1:
        cluster = "".join(cons)
        e = bank.pick(head + cluster, cluster) or bank.pick(head + cons[0], cons[0])
        if e:
            pre.append(e)
    if start and len(cons) == 1:
        nuc = bank.pick(f"- {cv}", cv)
    else:
        nuc = bank.pick(cv, f"- {cv}")
    if nuc is None:                                    # unknown CV: say the vowel alone
        nuc = bank.pick(vowel, f"- {vowel}")
    return pre, nuc


def plan_english(items, bank: Voicebank, prosody: Prosody) -> Plan:
    speed = prosody.speed or 1.0
    plan = Plan()
    t_cursor = LEAD
    for p_index, (sylls, pause) in enumerate(_split_phrases(items)):
        prev: Syllable | None = None
        prev_t = None
        phrase_t0 = None
        last_unit_end = t_cursor
        for k, s in enumerate(sylls):
            final = k == len(sylls) - 1
            d = _syl_duration(s, final) / speed
            if prev is None:
                pre, nuc = _cv(bank, s.onset, s.vowel, start=True)
                if nuc is None:
                    continue
                t = t_cursor + nuc.preutter + sum(e.length * 0.8 for e in pre)
                floor = t_cursor
                plan.units += _place_chain_backwards(pre, t - nuc.preutter, nuc.overlap, floor, p_index)
                u = _nucleus(nuc, t, floor, p_index, first=not pre)
                if pre:
                    plan.units[-len(pre)].first_in_phrase = True
                phrase_t0 = u.start
            else:
                run = prev.coda + s.onset
                onset = s.onset if s.onset else (prev.coda[-1:] if prev.coda else [])
                gap = TIMING["consonant"] * len(run) / speed
                t = prev_t + prev_d + gap
                floor = prev_t + MIN_VOWEL
                pre: list[OtoEntry] = []
                if not run:
                    nuc = bank.pick(f"{prev.vowel} {s.vowel}", s.vowel, f"- {s.vowel}")
                elif run == ["h"]:
                    nuc = bank.pick(f"{prev.vowel} h{s.vowel}", f"h{s.vowel}", f"- h{s.vowel}")
                else:
                    vc = bank.pick(f"{prev.vowel} {run[0]}")
                    if vc:
                        pre.append(vc)
                    coda_only = run[:len(run) - len(onset)]
                    if len(coda_only) >= 2:      # "built that": I l, then "l t-" keeps the t
                        cc = bank.pick(f"{coda_only[0]} {''.join(coda_only[1:])}-",
                                       f"{coda_only[0]} {coda_only[1]}-", f"{coda_only[0]} {coda_only[1]}")
                        if cc:
                            pre.append(cc)
                    elif coda_only and onset:
                        cc = bank.get(f"{coda_only[0]} {onset[0]}")
                        if cc:
                            pre.append(cc)
                    cl_pre, nuc = _cv(bank, onset, s.vowel, start=False)
                    pre += cl_pre
                if nuc is None:
                    prev, prev_t, prev_d = s, t, d
                    continue
                plan.units += _place_chain_backwards(pre, t - nuc.preutter, nuc.overlap, floor, p_index)
                u = _nucleus(nuc, t, floor, p_index)
            plan.units.append(u)
            plan.beats.append(Beat(t, d, _accent(s), p_index))
            prev, prev_t, prev_d = s, t, d

        if prev is None:
            continue
        # phrase end: "V -", or "V C-" / "V C" + "C C-" for final consonants
        end_t = prev_t + prev_d
        floor = prev_t + MIN_VOWEL
        tail: list[OtoEntry] = []
        c = prev.coda
        if not c:
            e = bank.pick(f"{prev.vowel} -")
            tail = [e] if e else []
        elif len(c) == 1:
            e = bank.pick(f"{prev.vowel} {c[0]}-", f"{prev.vowel} {c[0]}")
            tail = [e] if e else []
        else:
            e1 = bank.pick(f"{prev.vowel} {c[0]}", f"{prev.vowel} {c[0]}-")
            e2 = bank.pick(f"{c[0]} {''.join(c[1:])}-", f"{c[0]} {c[1]}-", f"{c[-2]} {c[-1]}-")
            tail = [e for e in (e1, e2) if e]
        t_end = end_t
        for i, e in enumerate(tail):
            if i == 0:
                start = end_t - e.preutter
                ls = 0.0
                if start < floor:
                    ls, start = floor - start, floor
            else:
                start, ls = t_end - max(e.overlap, 0.01), 0.0
            u = Unit(e, start, ls, None, stretch=False, phrase=p_index)
            u.end = start + u.natural
            plan.units.append(u)
            t_end = u.end
        last_unit_end = max(t_end, end_t + 0.05)
        kind = pause.kind if pause else "end"
        plan.phrases.append(PhraseInfo(phrase_t0 if phrase_t0 is not None else t_cursor,
                                       end_t, kind))
        gap = (pause.ms / 1000 / speed) if pause else 0.0
        t_cursor = last_unit_end + max(gap - 0.12, 0.04) if pause else last_unit_end
    plan.duration = max([u.end or (u.start + u.natural) for u in plan.units] + [t_cursor]) + 0.08
    _close_ends(plan)
    return plan


# ── Japanese (CV, kana aliases) ─────────────────────────────────────────────

def _mora_entry(bank: Voicebank, kana: str, start: bool) -> OtoEntry | None:
    kata = "".join(chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c for c in kana)
    cands = ([f"- {kana}", kana] if start else [kana, f"- {kana}"]) + [kata, f"- {kata}"]
    e = bank.pick(*cands)
    if e is None and len(kana) == 2:                   # unknown combo: base kana, then the small vowel
        e = bank.pick(kana[0], f"- {kana[0]}")
    if e is None:
        e = bank.pick(vowel_of(kana))
    return e


def plan_japanese(items, bank: Voicebank, prosody: Prosody) -> Plan:
    speed = prosody.speed or 1.0
    plan = Plan()
    t_cursor = LEAD
    mora_d = MORA_S / speed
    for p_index, (morae, pause) in enumerate(_split_phrases(items)):
        t = None
        start_next = True
        phrase_t0 = None
        n = len(morae)
        for k, m in enumerate(morae):
            e = _mora_entry(bank, m.kana, start_next)
            if e is None:
                continue
            final = k == n - 1
            d = mora_d * (1 + 0.9 * m.long) * (1.5 if final else 1.0) * (1.2 if m.emph else 1.0)
            if t is None:
                t = t_cursor + e.preutter
            u = _nucleus(e, t, t_cursor if start_next else t - 0.2, p_index, first=start_next)
            u.first_in_phrase = start_next
            if phrase_t0 is None:
                phrase_t0 = u.start
            plan.units.append(u)
            accent = (0.0 if k == 0 else 1.6) + (2.0 if m.emph else 0.0)
            plan.beats.append(Beat(t, d, accent, p_index))
            if m.geminate:                             # っ: close, then restart from silence
                u.end = t + d * 0.9
                t_cursor = u.end + 0.06 / speed
                start_next = True
                t = None
            else:
                start_next = False
                t = t + d
        last = plan.units[-1] if plan.units else None
        end_t = t if t is not None else t_cursor
        if last is not None and last.phrase == p_index and last.end is None:
            last.end = end_t + 0.06
        plan.phrases.append(PhraseInfo(phrase_t0 or t_cursor, end_t, pause.kind if pause else "end"))
        gap = (pause.ms / 1000 / speed) if pause else 0.0
        t_cursor = end_t + 0.06 + gap
    plan.duration = max([u.end or (u.start + u.natural) for u in plan.units] + [t_cursor]) + 0.08
    _close_ends(plan)
    return plan


def _close_ends(plan: Plan):
    """Give every open-ended unit an end: the next unit's start + its overlap."""
    plan.units.sort(key=lambda u: u.start)
    for i, u in enumerate(plan.units):
        if u.end is not None:
            continue
        nxt = next((v for v in plan.units[i + 1:] if v.phrase == u.phrase), None)
        if nxt is not None:
            # a negative overlap leaves a short gap, as in UTAU (stop closures in CV banks)
            u.end = max(nxt.start + nxt.entry.overlap, u.start + 0.03)
        else:
            u.end = u.start + u.natural


# ── pitch ───────────────────────────────────────────────────────────────────

def _raised_cosine(t: np.ndarray, a: float, b: float) -> np.ndarray:
    w = np.zeros_like(t)
    if b <= a:
        return w
    inside = (t >= a) & (t <= b)
    w[inside] = 0.5 - 0.5 * np.cos(2 * np.pi * (t[inside] - a) / (b - a))
    return w


def pitch_curve(plan: Plan, prosody: Prosody, n_frames: int) -> np.ndarray:
    """Semitones relative to the voice's speaking pitch, one value per 5 ms frame."""
    t = np.arange(n_frames) * FRAME
    rng = 1.0 if prosody.range is None else max(prosody.range, 0.0)
    contour_override = prosody.contour
    flat = contour_override == "flat"
    st = np.zeros(n_frames)
    for j, ph in enumerate(plan.phrases):
        beats = [b for b in plan.beats if b.phrase == j]
        if not beats:
            continue
        a, b = beats[0].t - 0.05, beats[-1].t + beats[-1].dur + 0.25
        inside = (t >= a) & (t <= b)
        span = max(b - a, 0.2)
        decl = 0.0 if flat else (1.2 - 2.4 * (t[inside] - a) / span)
        st[inside] += decl * rng
        for beat in beats:
            st += (0.3 if flat else 1.0) * rng * beat.accent * \
                _raised_cosine(t, beat.t - 0.04, beat.t + beat.dur + 0.04)

        is_last = j == len(plan.phrases) - 1
        kind = ph.kind
        contour = (contour_override if (contour_override and (is_last or kind != "comma"))
                   else {"question": "question", "comma": "continue"}.get(kind, "fall"))
        last = beats[-1]
        # rises must peak inside the last vowel to be heard; falls may finish on the tail
        c0 = last.t - 0.02
        c1 = last.t + (0.8 * last.dur if contour in ("rise", "question", "continue") else last.dur + 0.2)
        ramp = np.clip((t - c0) / max(c1 - c0, 0.05), 0, 1)
        shape = {"fall": -3.0, "rise": 4.5, "question": 7.0, "flat": 0.0, "continue": 1.5}[contour]
        if contour == "question":                     # dip before the rise, like "really↗"
            st -= 1.0 * rng * _raised_cosine(t, last.t - 0.15, last.t + 0.1)
        st += shape * rng * ramp * (t >= c0)
    st += prosody.pitch or 0.0
    k = 9                                            # ~45 ms smoothing
    return np.convolve(np.pad(st, (k, k), mode="edge"), np.ones(k) / k, mode="same")[k:-k]
