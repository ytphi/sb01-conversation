"""Anthropic Claude provider. pip install anthropic"""

from collections.abc import Iterator

from llm_providers import LLMProvider, Message


class ClaudeProvider(LLMProvider):
    name = "claude"
    default_model = "claude-sonnet-5-5"
    api_key_envs = ("ANTHROPIC_API_KEY",)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        import anthropic
        self._client = anthropic.Anthropic(api_key=self.api_key)

    def chat(self, system: str, messages: list[Message]) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def stream(self, system: str, messages: list[Message]) -> Iterator[str]:
        with self._client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": m.role, "content": m.content} for m in messages],
        ) as stream:
            yield from stream.text_stream
