"""DeepSeek provider (OpenAI-compatible API). pip install openai"""

from llm_providers import LLMProvider, Message


class DeepSeekProvider(LLMProvider):
    name = "deepseek"
    default_model = "deepseek-chat"
    api_key_envs = ("DEEPSEEK_API_KEY",)

    def __init__(self, base_url: str = "https://api.deepseek.com", **kwargs):
        super().__init__(**kwargs)
        from openai import OpenAI
        self._client = OpenAI(api_key=self.api_key, base_url=base_url)

    def chat(self, system: str, messages: list[Message]) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "system", "content": system}]
                     + [{"role": m.role, "content": m.content} for m in messages],
        )
        return (resp.choices[0].message.content or "").strip()
