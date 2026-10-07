"""
_paths.py  -  make the experimental/utilities modules importable

The utilities folders use hyphenated names (chatterbox-nano, G1-builtin, ...)
so they can't be Python packages; instead their directories go on sys.path.
Import this module first from any entry point.
"""

import os
import sys

FRAMEWORK_DIR    = os.path.dirname(os.path.abspath(__file__))
EXPERIMENTAL_DIR = os.path.dirname(FRAMEWORK_DIR)
REPO_ROOT        = os.path.dirname(EXPERIMENTAL_DIR)
UTILITIES_DIR    = os.path.join(EXPERIMENTAL_DIR, "utilities")

_DIRS = [
    FRAMEWORK_DIR,
    os.path.join(UTILITIES_DIR, "llm"),
    os.path.join(UTILITIES_DIR, "tts"),
    os.path.join(UTILITIES_DIR, "tts", "chatterbox-nano"),
    os.path.join(UTILITIES_DIR, "tts", "chatterbox-ml3"),
    os.path.join(UTILITIES_DIR, "tts", "G1-builtin"),
    os.path.join(UTILITIES_DIR, "stt"),
    os.path.join(UTILITIES_DIR, "stt", "faster-whisper"),
    os.path.join(UTILITIES_DIR, "stt", "G1-builtin"),
]

for _d in _DIRS:
    if _d not in sys.path:
        sys.path.insert(0, _d)


def load_env():
    """Load KEY=VALUE lines from .env / env files without overriding real env vars.

    Searched in order: experimental/speech-framework, experimental, repo root.
    """
    for directory in (FRAMEWORK_DIR, EXPERIMENTAL_DIR, REPO_ROOT):
        for name in (".env", "env"):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip().removeprefix("export ").strip()
                        os.environ.setdefault(key, value.strip().strip('"').strip("'"))
