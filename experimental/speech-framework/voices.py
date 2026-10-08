"""
voices.py  -  the voice registry: every named voice the robot can speak with

A voice says, per language, which engine speaks and with which assets:

  tts.voices.<name>:                     (config.yaml / config.local.yaml)
    description: ...
    fallback: chatterbox                 # voice used for languages this one lacks
    tone_tags: true                      # ask the LLM for <tone> plans (UTAU uses them fully)
    consent: {by: ..., date: ...}        # whose voice this is and their OK to clone it
    languages:
      en: { engine: utau, bank: ~/voicebanks/... , base_pitch: 0 }
      ja: { engine: multilingual, voice: ../utilities/voice-refs/x/clip.wav, exaggeration: 0.5 }
      es: { engine: multilingual, voice_from_bank: ~/voicebanks/... }   # clone a UTAU singer

Folders are voices too. Every sub-folder of a `tts.voice_dirs` entry
(default ../utilities/voice-refs) becomes a voice named after the folder:
  - a folder with an audio clip     → Chatterbox clone of that clip (tts.clone_defaults)
  - a folder with an oto.ini        → UTAU voicebank
  - an optional voice.yaml in the folder overrides any of the keys above, plus
      clip: file.wav                 which clip to clone (default: the first one)
      engine: nano | multilingual    engine for every language it can speak
      clips: {en: a.wav, ja: b.wav}  a different clip per language

Pick one with --voice NAME or tts.voice. Paths are relative to the file that
defines them (config paths: the speech-framework folder); ~ is expanded.
"""

import copy
import glob
import os
from dataclasses import dataclass, field

import yaml

from settings import resolve_path

AUDIO_EXTS  = (".wav", ".mp3", ".flac", ".ogg", ".m4a")
ENGINES     = ("nano", "multilingual", "utau")
ENGINE_LANGUAGES = {"nano": {"en"}}         # engines limited to some languages
ASSET_KEYS  = ("voice", "bank")             # route keys that are file paths


@dataclass
class Route:
    engine: str
    asset: str | None = None                # clip (Chatterbox) or voicebank folder (UTAU)
    params: dict = field(default_factory=dict)


@dataclass
class Voice:
    name: str
    languages: dict[str, Route]
    description: str = ""
    fallback: str | None = None
    tone_tags: bool = False
    consent: dict | None = None
    source: str = "config"
    cloned: bool = False                    # built from a recording of a real person

    def summary(self) -> str:
        langs = ", ".join(f"{lang}:{r.engine}" for lang, r in self.languages.items())
        return f"{self.name:<18} {langs:<44} {self.description}"


def _expand(path: str | None, base_dir: str | None) -> str | None:
    if not path:
        return None
    path = os.path.expanduser(str(path))
    if os.path.isabs(path):
        return path
    if base_dir:
        return os.path.normpath(os.path.join(base_dir, path))
    return resolve_path(path)


def _route(spec: dict, base_dir: str | None) -> Route:
    spec = dict(spec or {})
    engine = spec.pop("engine", "multilingual")
    if engine not in ENGINES:
        raise ValueError(f"unknown TTS engine {engine!r} (expected one of {ENGINES})")
    asset = None
    for key in ASSET_KEYS:
        if key in spec:
            asset = _expand(spec.pop(key), base_dir) or asset
    bank = _expand(spec.pop("voice_from_bank", None), base_dir)
    if bank:                                     # Chatterbox clone of a UTAU singer (bank_clip.py)
        if os.path.isdir(bank):
            from bank_clip import ensure_clip
            asset = ensure_clip(bank)
        else:
            asset = bank                         # missing: check() reports it
    return Route(engine, asset, spec)


def _voice(name: str, spec: dict, base_dir: str | None, source: str) -> Voice:
    langs = {lang: _route(r, base_dir) for lang, r in (spec.get("languages") or {}).items()}
    return Voice(
        name=name,
        languages=langs,
        description=spec.get("description", ""),
        fallback=spec.get("fallback"),
        tone_tags=bool(spec.get("tone_tags", any(r.engine == "utau" for r in langs.values()))),
        consent=spec.get("consent"),
        source=source,
        cloned=bool(spec.get("cloned", False)),
    )


def _find_oto(folder: str) -> str | None:
    for depth in ("oto.ini", "*/oto.ini", "*/*/oto.ini"):
        hits = sorted(glob.glob(os.path.join(folder, depth)))
        if hits:
            return os.path.dirname(hits[0])
    return None


def _bank_language(bank: str) -> str:
    """Japanese if the oto.ini aliases contain kana, else English."""
    raw = open(os.path.join(bank, "oto.ini"), "rb").read(20000)
    for enc in ("utf-8", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return "en"
    return "ja" if any(0x3040 <= ord(c) <= 0x30FF for c in text) else "en"


def _folder_voice(folder: str, clone_defaults: dict, default_fallback: str | None) -> Voice | None:
    name = os.path.basename(folder.rstrip(os.sep))
    meta_path = os.path.join(folder, "voice.yaml")
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
    meta = dict(meta)
    name = meta.pop("name", name)

    if meta.get("languages"):                       # fully specified in voice.yaml
        meta.setdefault("fallback", default_fallback)
        return _voice(name, meta, folder, meta_path)

    bank = _expand(meta.pop("bank", None), folder) or _find_oto(folder)
    if meta.get("engine") == "utau" or (bank and not meta.get("clip")):
        if not bank:
            print(f"[voices] {name}: engine utau but no oto.ini found in {folder}")
            return None
        lang = meta.pop("language", None) or _bank_language(bank)
        params = {k: meta.pop(k) for k in list(meta) if k not in
                  ("description", "fallback", "tone_tags", "consent", "engine")}
        spec = {**meta, "languages": {lang: {"engine": "utau", "bank": bank, **params}}}
        spec.setdefault("fallback", default_fallback)
        return _voice(name, spec, folder, meta_path if os.path.isfile(meta_path) else folder)

    clips = sorted(p for p in glob.glob(os.path.join(folder, "*"))
                   if p.lower().endswith(AUDIO_EXTS))
    clip = _expand(meta.pop("clip", None), folder) or (clips[0] if clips else None)
    per_lang = {lang: _expand(c, folder) for lang, c in (meta.pop("clips", None) or {}).items()}
    if not clip and not per_lang:
        return None
    engine = meta.pop("engine", None)
    params = meta.pop("params", {}) or {}
    languages = {}
    for lang, defaults in clone_defaults.items():
        route = {**copy.deepcopy(defaults or {}), **params}
        if engine and lang in ENGINE_LANGUAGES.get(engine, {lang}):
            route["engine"] = engine             # e.g. nano for en, the defaults elsewhere
        route["voice"] = per_lang.get(lang) or clip
        languages[lang] = route
    spec = {**meta, "languages": languages, "cloned": True}
    spec.setdefault("fallback", default_fallback)
    return _voice(name, spec, folder, meta_path if os.path.isfile(meta_path) else folder)


class VoiceRegistry:
    def __init__(self, tts_cfg: dict):
        self.voices: dict[str, Voice] = {}
        self.default = tts_cfg.get("voice")

        for name, spec in (tts_cfg.get("voices") or {}).items():
            self.voices[name] = _voice(name, spec, None, "config.yaml")

        if tts_cfg.get("languages") and "legacy" not in self.voices:
            # old engine-per-language table (tts.languages) still works as a voice
            self.voices["legacy"] = _voice("legacy", {"languages": tts_cfg["languages"],
                                                      "description": "tts.languages table"},
                                           None, "config.yaml")
            self.default = self.default or "legacy"

        clone_defaults = tts_cfg.get("clone_defaults") or {"en": {"engine": "multilingual"}}
        for d in tts_cfg.get("voice_dirs") or ["../utilities/voice-refs"]:
            root = _expand(d, None)
            if not root or not os.path.isdir(root):
                continue
            for folder in sorted(glob.glob(os.path.join(root, "*", ""))):
                if os.path.basename(folder.rstrip(os.sep)).startswith((".", "_")):
                    continue
                try:
                    voice = _folder_voice(folder, clone_defaults, tts_cfg.get("clone_fallback"))
                except Exception as exc:
                    print(f"[voices] skipping {folder}: {exc}")
                    continue
                if voice and voice.name not in self.voices:
                    self.voices[voice.name] = voice

        if not self.default and self.voices:
            self.default = next(iter(self.voices))

    def names(self) -> list[str]:
        return list(self.voices)

    def get(self, name: str | None = None) -> Voice:
        name = name or self.default
        if name not in self.voices:
            raise ValueError(f"unknown voice {name!r}; available: {', '.join(self.voices)}")
        voice = self.voices[name]
        if voice.cloned and not voice.consent:
            print(f"[voices] note: {name!r} clones a real recording but has no consent recorded "
                  f"(add consent: to its voice.yaml)")
        return voice

    def route(self, voice: Voice, lang: str) -> tuple[Voice, Route] | None:
        """The route for lang: the voice's own, else its fallback chain's."""
        seen = set()
        v = voice
        while v and v.name not in seen:
            seen.add(v.name)
            if lang in v.languages:
                return v, v.languages[lang]
            v = self.voices.get(v.fallback) if v.fallback else None
        return None

    def check(self) -> list[str]:
        """Human-readable problems: missing clips or banks, broken fallbacks."""
        problems = []
        for v in self.voices.values():
            if v.fallback and v.fallback not in self.voices:
                problems.append(f"{v.name}: fallback {v.fallback!r} is not a voice")
            for lang, r in v.languages.items():
                if r.asset and not os.path.exists(r.asset):
                    problems.append(f"{v.name}/{lang}: missing {r.asset}")
        return problems
