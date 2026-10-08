"""
g2p_en.py  -  English text → syllables in the X-SAMPA symbols of a CVVC English bank

CMUdict (pip install cmudict) gives ARPAbet phones with stress; words it lacks
go through a small spelling-rule fallback, and all-caps words up to 5 letters
are spelled out (CSUSB → C S U S B). Numbers are read as words.

Output is a flat list of Syllable and Pause items; the planner turns it into
voicebank aliases. Symbols follow the Kasane Teto English bank:
  vowels  i I eI E { A aI aU V O OI oU U u @ 3
  conson. p b t d k g f v T D s z S Z h tS dZ m n N l r w j
"""

import re
from dataclasses import dataclass, field

ARPA_VOWELS = {
    "AA": "A", "AE": "{", "AH": "V", "AO": "O", "AW": "aU", "AY": "aI", "EH": "E",
    "ER": "3", "EY": "eI", "IH": "I", "IY": "i", "OW": "oU", "OY": "OI", "UH": "U", "UW": "u",
}
ARPA_CONSONANTS = {
    "B": "b", "CH": "tS", "D": "d", "DH": "D", "F": "f", "G": "g", "HH": "h", "JH": "dZ",
    "K": "k", "L": "l", "M": "m", "N": "n", "NG": "N", "P": "p", "R": "r", "S": "s",
    "SH": "S", "T": "t", "TH": "T", "V": "v", "W": "w", "Y": "j", "Z": "z", "ZH": "Z",
}
VOWELS = set(ARPA_VOWELS.values()) | {"@"}

# Words CMUdict lacks or gets wrong for this robot (ARPAbet)
LEXICON = {
    "teto": "T EH1 T OW0", "sb": "EH1 S B IY1", "csusb": "S IY1 EH1 S Y UW1 EH1 S B IY1",
    "unitree": "Y UW1 N IH0 T R IY2", "chatterbox": "CH AE1 T ER0 B AA2 K S", "utau": "UW1 T AW2",
}

# Unstressed in running speech even when CMUdict marks them stressed
FUNCTION_WORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with", "by",
    "from", "as", "is", "are", "was", "were", "be", "been", "am", "it", "its", "i", "you",
    "he", "she", "we", "they", "me", "him", "her", "us", "them", "my", "your", "our", "their",
    "this", "that", "do", "does", "did", "have", "has", "had", "can", "will", "would", "could",
    "should", "so", "if", "than", "then", "there", "not", "just", "i'm", "it's", "you're",
}

# Onsets English allows (maximal onset principle when splitting consonant runs)
LEGAL_ONSETS = {
    ("p", "l"), ("p", "r"), ("b", "l"), ("b", "r"), ("t", "r"), ("t", "w"), ("d", "r"),
    ("d", "w"), ("k", "l"), ("k", "r"), ("k", "w"), ("g", "l"), ("g", "r"), ("g", "w"),
    ("f", "l"), ("f", "r"), ("T", "r"), ("T", "w"), ("S", "r"), ("s", "p"), ("s", "t"),
    ("s", "k"), ("s", "m"), ("s", "n"), ("s", "l"), ("s", "w"), ("s", "p", "r"),
    ("s", "t", "r"), ("s", "k", "r"), ("s", "p", "l"), ("s", "k", "l"), ("s", "k", "w"),
    ("p", "j"), ("b", "j"), ("t", "j"), ("d", "j"), ("k", "j"), ("g", "j"), ("f", "j"),
    ("v", "j"), ("m", "j"), ("n", "j"), ("h", "j"), ("l", "j"),
}


@dataclass
class Syllable:
    onset: list[str]
    vowel: str
    coda: list[str]
    stress: int = 0           # 0 unstressed, 1 primary, 2 secondary
    word: str = ""
    word_index: int = 0
    word_final: bool = False
    emph: bool = False


@dataclass
class Pause:
    ms: int
    kind: str = "comma"       # comma | sentence | question | exclaim | explicit


@dataclass
class Word:
    text: str
    syllables: list[Syllable] = field(default_factory=list)


_cmu = None


def _dict():
    global _cmu
    if _cmu is None:
        import cmudict
        _cmu = cmudict.dict()
    return _cmu


# ── numbers ─────────────────────────────────────────────────────────────────

_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen " \
        "fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def number_words(n: int) -> str:
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + (" " + number_words(rest) if rest else "")
    for size, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand")):
        if n >= size:
            rest = n % size
            return number_words(n // size) + " " + name + (" " + number_words(rest) if rest else "")
    return str(n)


def _expand_numbers(text: str) -> str:
    def say(m):
        s = m.group(0)
        if s.startswith("0") and len(s) > 1 and "." not in s:     # 01 → oh one
            return " ".join("oh" if d == "0" else _ONES[int(d)] for d in s if d.isdigit())
        if "." in s:
            whole, frac = s.split(".", 1)
            return number_words(int(whole)) + " point " + " ".join(_ONES[int(d)] for d in frac)
        n = int(s.replace(",", ""))
        if 1100 <= n <= 2099 and "," not in s and len(s) == 4 and n % 100:   # years: twenty twenty six
            return number_words(n // 100) + " " + (("oh " + _ONES[n % 100]) if n % 100 < 10
                                                  else number_words(n % 100))
        return number_words(n)
    text = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", text)     # sb01 → sb 01
    text = re.sub(r"(\d+)%", r"\1 percent", text)
    text = re.sub(r"\$(\d+(?:,\d{3})*(?:\.\d+)?)", r"\1 dollars", text)
    return re.sub(r"\d+(?:,\d{3})*(?:\.\d+)?", say, text)


# ── letter-to-sound fallback ────────────────────────────────────────────────

_LTS = [
    ("tion", "SH AH0 N"), ("sion", "ZH AH0 N"), ("ough", "AO1"), ("igh", "AY1"), ("eigh", "EY1"),
    ("tch", "CH"), ("dge", "JH"), ("ch", "CH"), ("sh", "SH"), ("th", "TH"), ("ph", "F"),
    ("ck", "K"), ("ng", "NG"), ("qu", "K W"), ("wh", "W"), ("kn", "N"), ("wr", "R"),
    ("ee", "IY1"), ("ea", "IY1"), ("oo", "UW1"), ("ai", "EY1"), ("ay", "EY1"), ("ey", "EY1"),
    ("oa", "OW1"), ("ou", "AW1"), ("ow", "OW1"), ("oi", "OY1"), ("oy", "OY1"), ("au", "AO1"),
    ("aw", "AO1"), ("ar", "AA1 R"), ("er", "ER0"), ("ir", "ER1"), ("ur", "ER1"), ("or", "AO1 R"),
    ("ie", "IY1"), ("ue", "UW1"), ("ew", "UW1"),
    ("a", "AE1"), ("e", "EH1"), ("i", "IH1"), ("o", "AA1"), ("u", "AH1"),
    ("b", "B"), ("c", "K"), ("d", "D"), ("f", "F"), ("g", "G"), ("h", "HH"), ("j", "JH"),
    ("k", "K"), ("l", "L"), ("m", "M"), ("n", "N"), ("p", "P"), ("q", "K"), ("r", "R"),
    ("s", "S"), ("t", "T"), ("v", "V"), ("w", "W"), ("x", "K S"), ("z", "Z"),
]


def letter_to_sound(word: str) -> list[str]:
    w = word.lower()
    if len(w) > 3 and w.endswith("e") and w[-2] not in "aeiou":
        w = w[:-1]                                   # silent final e
    phones, i = [], 0
    while i < len(w):
        if w[i] == "o" and i == len(w) - 1:
            phones.append("OW0")                     # final o: teto, robo
            i += 1
            continue
        if w[i] == "y":
            phones.append("Y" if i == 0 else "IY0" if i == len(w) - 1 else "IH1")
            i += 1
            continue
        if w[i] == "c" and i + 1 < len(w) and w[i + 1] in "eiy":
            phones.append("S")
            i += 1
            continue
        for graph, ph in _LTS:
            if w.startswith(graph, i):
                phones += ph.split()
                i += len(graph)
                break
        else:
            i += 1
    # keep one primary stress (the first), demote the rest
    seen = False
    out = []
    for p in phones:
        if p[-1].isdigit():
            if p[-1] == "1" and seen:
                p = p[:-1] + "0"
            seen = seen or p[-1] == "1"
        out.append(p)
    return out


# ── words → syllables ───────────────────────────────────────────────────────

def word_phones(word: str) -> list[str]:
    key = word.lower().strip("'")
    if key in LEXICON:
        return LEXICON[key].split()
    d = _dict()
    if key in d:
        return d[key][0]
    no_vowel = not re.search(r"[aeiouy]", key)
    if (word.isupper() and 1 < len(word) <= 5) or (no_vowel and len(key) <= 4):
        return [p for letter in key if letter.isalpha() for p in d.get(letter, [[]])[0]]
    if "'" in key and key.replace("'", "") in d:
        return d[key.replace("'", "")][0]
    return letter_to_sound(key.replace("'", ""))


def syllabify(phones: list[str], word: str = "", index: int = 0) -> list[Syllable]:
    """ARPAbet phones (with stress digits) → syllables, maximal legal onsets."""
    vowels = [i for i, p in enumerate(phones) if p[-1].isdigit()]
    if not vowels:
        return []
    sym = [ARPA_VOWELS.get(p[:-1], "@") if p[-1].isdigit() else ARPA_CONSONANTS.get(p, "")
           for p in phones]
    sylls = []
    for k, vi in enumerate(vowels):
        stress = int(phones[vi][-1])
        vowel = sym[vi]
        if phones[vi][:-1] == "AH" and stress == 0:
            vowel = "@"                                # unstressed schwa
        sylls.append(Syllable([], vowel, [], stress, word, index))
    # leading consonants → first onset, trailing → last coda
    sylls[0].onset = [s for s in sym[:vowels[0]] if s]
    sylls[-1].coda = [s for s in sym[vowels[-1] + 1:] if s]
    for k in range(len(vowels) - 1):
        run = [s for s in sym[vowels[k] + 1:vowels[k + 1]] if s]
        split = len(run)
        for cut in range(len(run) + 1):               # longest legal onset wins
            onset = tuple(run[cut:])
            if len(onset) <= 1 or onset in LEGAL_ONSETS:
                split = cut
                break
        sylls[k].coda, sylls[k + 1].onset = run[:split], run[split:]
    sylls[-1].word_final = True
    return sylls


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z']*|[.!?…]+|[,;:—–-]+")


def text_to_items(text: str, emphasis: list[str] | None = None) -> list[Syllable | Pause]:
    emph = {w.lower() for w in (emphasis or [])}
    items: list[Syllable | Pause] = []
    word_index = 0
    for tok in _TOKEN_RE.findall(_expand_numbers(text.replace("&", " and "))):
        if tok[0] in ".!?…":
            kind = "question" if "?" in tok else "exclaim" if "!" in tok else "sentence"
            items.append(Pause(380, kind))
            continue
        if not tok[0].isalpha():
            if tok in ("-",):
                continue
            items.append(Pause(200, "comma"))
            continue
        sylls = syllabify(word_phones(tok), tok, word_index)
        if tok.lower() in FUNCTION_WORDS and len(sylls) == 1:
            sylls[0].stress = 0
            if sylls[0].vowel == "V":
                sylls[0].vowel = "@"
        for s in sylls:
            s.emph = tok.lower().strip("'") in emph
        items += sylls
        word_index += 1
    return items
