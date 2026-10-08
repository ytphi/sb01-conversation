"""
g2p_ja.py  -  Japanese text → morae (hiragana) for a CV voicebank

pykakasi (pip install pykakasi) reads kanji into hiragana. Small kana join the
mora before them (きゃ), っ becomes a short closure, ー lengthens the previous
mora, and 、。！？ become pauses. Standalone particles は / へ are read わ / え.
Latin letters are skipped (the CV bank has no English).
"""

import re
from dataclasses import dataclass

from g2p_en import Pause

SMALL = set("ゃゅょぁぃぅぇぉゎ")
_PUNCT = {"、": ("comma", 180), "，": ("comma", 180), ",": ("comma", 180),
          "。": ("sentence", 350), ".": ("sentence", 350),
          "！": ("exclaim", 350), "!": ("exclaim", 350),
          "？": ("question", 350), "?": ("question", 350), "…": ("comma", 300)}


@dataclass
class Mora:
    kana: str
    long: int = 0             # number of ー after it
    geminate: bool = False    # followed by っ
    word_index: int = 0
    emph: bool = False


_kks = None


def _kakasi():
    global _kks
    if _kks is None:
        import pykakasi
        _kks = pykakasi.kakasi()
    return _kks


def _to_hira(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in text)


def text_to_items(text: str, emphasis: list[str] | None = None) -> list[Mora | Pause]:
    emph = set(emphasis or [])
    items: list[Mora | Pause] = []
    for w, tok in enumerate(_kakasi().convert(text)):
        orig, hira = tok["orig"], tok["hira"]
        if orig == "今日は" and len(text.strip(" 。、！？!?.")) > 3:
            hira = "きょうわ"                          # pykakasi reads 今日は as こんにちは
        elif orig in ("は", "へ") and items and isinstance(items[-1], Mora):
            hira = "わ" if orig == "は" else "え"
        elif re.search(r"(にちは|んばんは|では|には)$", hira) and orig.endswith("は"):
            hira = hira[:-1] + "わ"                    # こんにちは, こんばんは, では, には
        is_emph = orig in emph or hira in emph
        for ch in _to_hira(hira):
            if ch in _PUNCT:
                kind, ms = _PUNCT[ch]
                items.append(Pause(ms, kind))
            elif ch == "ー":
                if items and isinstance(items[-1], Mora):
                    items[-1].long += 1
            elif ch == "っ":
                if items and isinstance(items[-1], Mora):
                    items[-1].geminate = True
            elif ch in SMALL and items and isinstance(items[-1], Mora) and len(items[-1].kana) == 1:
                items[-1].kana += ch
            elif 0x3041 <= ord(ch) <= 0x3096 or ch == "ゔ":
                items.append(Mora("ヴ" if ch == "ゔ" else ch, word_index=w, emph=is_emph))
            elif re.match(r"\s", ch) and items and not isinstance(items[-1], Pause):
                continue
    return items


def vowel_of(kana: str) -> str:
    """The vowel a mora ends on (for lengthening and fallbacks)."""
    last = kana[-1]
    for vowel, chars in (("あ", "あかさたなはまやらわがざだばぱぁゃゎ"),
                         ("い", "いきしちにひみりぎじぢびぴぃ"),
                         ("う", "うくすつぬふむゆるぐずづぶぷぅゅ"),
                         ("え", "えけせてねへめれげぜでべぺぇ"),
                         ("お", "おこそとのほもよろをごぞどぼぽぉょ")):
        if last in chars:
            return vowel
    return "ん" if last == "ん" else "あ"
