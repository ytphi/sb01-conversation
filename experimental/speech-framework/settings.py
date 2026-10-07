"""
settings.py  -  load config.yaml (+ optional config.local.yaml / --config overrides)
"""

import os

import yaml

from _paths import FRAMEWORK_DIR

DEFAULT_CONFIG = os.path.join(FRAMEWORK_DIR, "config.yaml")
LOCAL_CONFIG   = os.path.join(FRAMEWORK_DIR, "config.local.yaml")


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(extra_path: str | None = None) -> dict:
    cfg = _read(DEFAULT_CONFIG)
    for path in (LOCAL_CONFIG, extra_path):
        if path and os.path.isfile(path):
            cfg = _deep_merge(cfg, _read(path))
    return cfg


def resolve_path(path: str | None) -> str | None:
    """Config paths are relative to the speech-framework folder."""
    if not path:
        return None
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(FRAMEWORK_DIR, path))
