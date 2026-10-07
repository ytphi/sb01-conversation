"""
gesture_cues.py  -  teaching gestures marked in the text of a reply

A reply may carry marks such as "Look at the [point] diagram on the board."
The mark is not spoken. It names a gesture from CUES in scripts/gesture_server.py
and sits just before the word the gesture should arrive on.

  split(text)          -> (text to speak, [(cue name, position in that text)])
  timed(marks, words)  -> [{"name": cue, "time": seconds into the audio}]

`words` is what the speech synthesizer reports while it speaks: the start time
and text of each word. No robot, network or audio code here.
"""

import re

# Keep in step with CUES in scripts/gesture_server.py.
CUE_NAMES = ("yes", "point", "point_right", "one_hand", "other_hand", "small", "big", "ask")
MAX_CUES = 12

_MARK = re.compile(r"\[(" + "|".join(CUE_NAMES) + r")\]\s*", re.IGNORECASE)
_ANY_MARK = re.compile(r"\[[a-z_]{2,20}\]\s*", re.IGNORECASE)


def split(text: str) -> tuple[str, list[tuple[str, int]]]:
    """Take the marks out of `text`. Marks that are not known cues are removed
    too, so a mistaken one is never read aloud."""
    marks, spoken, at = [], [], 0
    for found in _ANY_MARK.finditer(text):
        spoken.append(text[at:found.start()])
        known = _MARK.fullmatch(found.group(0))
        if known and len(marks) < MAX_CUES:
            marks.append((known.group(1).lower(), sum(len(part) for part in spoken)))
        at = found.end()
    spoken.append(text[at:])
    return "".join(spoken).strip(), _shifted(marks, "".join(spoken))


def _shifted(marks, spoken):
    """Positions after leading spaces are stripped from the spoken text."""
    lead = len(spoken) - len(spoken.lstrip())
    return [(name, max(0, position - lead)) for name, position in marks]


def timed(marks: list[tuple[str, int]], words: list[tuple[float, str]], spoken: str) -> list[dict]:
    """When each cue's word is spoken. `words` is [(start seconds, word text)] in
    order. A cue after the last word gets the last word's time; with no word
    times at all there are no cues, so a gesture is never placed by guesswork."""
    if not marks or not words:
        return []
    lowered, at, placed = spoken.lower(), 0, []
    for start, word in words:
        position = lowered.find(word.lower().strip(), at)
        if position < 0:
            position = at                      # synthesizer wrote the word differently: keep the order
        placed.append((position, float(start)))
        at = position + max(1, len(word.strip()))
    cues = []
    for name, position in marks:
        when = next((start for where, start in placed if where >= position), placed[-1][1])
        cues.append({"name": name, "time": round(when, 3)})
    return cues
