"""
oto.py  -  read a UTAU voicebank's oto.ini into an alias table

Each oto.ini line is  file.wav=alias,offset,consonant,cutoff,preutterance,overlap  (ms):
  offset        where the usable sample starts in the file
  consonant     fixed region from offset: played at natural speed, never stretched
  cutoff        end of the sample: negative = length from offset, positive = ms cut off the file end
  preutterance  the point (from offset) that lands exactly on the note start
  overlap       how long (from offset) this sample crossfades with the previous one

oto.ini files are usually Shift-JIS (cp932); UTF-8 is accepted too. A blank
alias means "the file name without .wav". The first entry for an alias wins.
Sub-folders with their own oto.ini (pitch or expression sets) are read as well
when `recursive=True`; aliases already defined at the top level take priority.
"""

import os
from dataclasses import dataclass

import soundfile as sf


@dataclass
class OtoEntry:
    alias: str
    wav: str                  # absolute path
    offset: float             # all times in seconds
    consonant: float
    cutoff: float             # raw oto value (s): negative = length, positive = trimmed from file end
    preutter: float
    overlap: float
    _length: float | None = None

    @property
    def length(self) -> float:
        """Usable sample length from offset (s)."""
        if self._length is None:
            if self.cutoff < 0:
                self._length = -self.cutoff
            else:
                self._length = max(sf.info(self.wav).duration - self.offset - self.cutoff, 0.01)
            self._length = max(self._length, self.consonant, self.preutter, 0.01)
        return self._length


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def read_oto(path: str) -> list[OtoEntry]:
    folder = os.path.dirname(os.path.abspath(path))
    with open(path, "rb") as f:
        text = _decode(f.read())
    entries = []
    for line in text.splitlines():
        if "=" not in line:
            continue
        fname, _, rest = line.partition("=")
        fields = rest.split(",")
        if len(fields) < 6:
            continue
        alias = fields[0].strip() or os.path.splitext(fname)[0]
        try:
            offset, consonant, cutoff, pre, ovl = (float(x or 0) / 1000 for x in fields[1:6])
        except ValueError:
            continue
        wav = os.path.join(folder, fname)
        if os.path.isfile(wav):
            entries.append(OtoEntry(alias, wav, offset, consonant, cutoff, pre, ovl))
    return entries


class Voicebank:
    """Alias table for one voicebank folder (the folder that holds oto.ini)."""

    def __init__(self, folder: str, recursive: bool = False):
        self.folder = os.path.abspath(os.path.expanduser(folder))
        oto = os.path.join(self.folder, "oto.ini")
        if not os.path.isfile(oto):
            raise FileNotFoundError(f"no oto.ini in {self.folder}")
        self.aliases: dict[str, OtoEntry] = {}
        self._add(read_oto(oto))
        if recursive:
            for root, _dirs, files in os.walk(self.folder):
                if root != self.folder and "oto.ini" in files:
                    self._add(read_oto(os.path.join(root, "oto.ini")))
        self.files = sorted({e.wav for e in self.aliases.values()})
        self.is_japanese = any(0x3040 <= ord(c) <= 0x30FF for a in self.aliases for c in a)

    def _add(self, entries: list[OtoEntry]):
        for e in entries:
            self.aliases.setdefault(e.alias, e)

    def __contains__(self, alias: str) -> bool:
        return alias in self.aliases

    def get(self, alias: str) -> OtoEntry | None:
        return self.aliases.get(alias)

    def pick(self, *candidates: str) -> OtoEntry | None:
        """First candidate alias that exists in the bank."""
        for alias in candidates:
            if alias and alias in self.aliases:
                return self.aliases[alias]
        return None
