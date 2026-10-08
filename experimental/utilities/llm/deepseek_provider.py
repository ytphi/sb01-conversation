"""DeepSeek provider (OpenAI-compatible API). pip install openai"""

from collections.abc import Iterator

from llm_providers import LLMProvider, Message


class DeepSeekProvider(LLMProvider):
    name = "deepseek"
    default_model = "deepseek-chat"
    api_key_envs = ("DEEPSEEK_API_KEY",)

    def __init__(self, base_url: str = "https://api.deepseek.com", **kwargs):
        super().__init__(**kwargs)
        from openai import OpenAI
        self._client = OpenAI(api_key=self.api_key, base_url=base_url)

    def _messages(self, system: str, messages: list[Message]) -> list[dict]:
        return ([{"role": "system", "content": system}]
                + [{"role": m.role, "content": m.content} for m in messages])

    def chat(self, system: str, messages: list[Message]) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=self._messages(system, messages),
        )
        return (resp.choices[0].message.content or "").strip()

    def stream(self, system: str, messages: list[Message]) -> Iterator[str]:
        for chunk in self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=self._messages(system, messages),
            stream=True,
        ):
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
