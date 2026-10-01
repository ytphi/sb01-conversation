"""
llm_providers.py  -  provider-agnostic chat interface (Gemini / DeepSeek / Claude)

Every provider takes the same inputs (system prompt + list of Message) and
returns plain reply text, so the conversation loop never touches a vendor SDK.

Provider SDKs are imported lazily: you only need the package for the
provider you actually use (google-genai, openai, or anthropic).
"""

import importlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

DEFAULT_PROVIDER = "gemini"


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: str


class LLMProvider(ABC):
    name = "base"
    default_model = ""
    api_key_envs: tuple[str, ...] = ()

    def __init__(self, model: str | None = None, max_tokens: int = 256,
                 api_key: str | None = None, **options):
        self.model      = model or self.default_model
        self.max_tokens = max_tokens
        self.api_key    = api_key or self._key_from_env()
        self.options    = options

    def _key_from_env(self) -> str:
        for env in self.api_key_envs:
            if os.environ.get(env):
                return os.environ[env]
        raise RuntimeError(
            f"{self.name}: no API key found. Set one of {', '.join(self.api_key_envs)} "
            "in your .env file."
        )

    @abstractmethod
    def chat(self, system: str, messages: list[Message]) -> str:
        """Return the assistant reply for the given conversation."""


# name → (module, class). Modules live next to this file.
_REGISTRY = {
    "gemini":   ("gemini_provider",   "GeminiProvider"),
    "deepseek": ("deepseek_provider", "DeepSeekProvider"),
    "claude":   ("claude_provider",   "ClaudeProvider"),
}


def available_providers() -> list[str]:
    return list(_REGISTRY)


def create_provider(name: str = DEFAULT_PROVIDER, **kwargs) -> LLMProvider:
    try:
        module_name, class_name = _REGISTRY[name.lower()]
    except KeyError:
        raise ValueError(f"unknown LLM provider {name!r}; choose from {available_providers()}")
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls(**kwargs)
