"""Google Gemini provider (default). pip install google-genai"""

from collections.abc import Iterator

from llm_providers import LLMProvider, Message


class GeminiProvider(LLMProvider):
    name = "gemini"
    default_model = "gemini-3.8-flash"
    api_key_envs = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(self, thinking_level: str | None = "low", **kwargs):
        super().__init__(**kwargs)
        from google import genai
        from google.genai import types
        self._types  = types
        self._client = genai.Client(api_key=self.api_key)
        # Gemini 3.x uses thinking_level (low/medium/high). Low keeps voice latency down.
        self.thinking_level = thinking_level

    def _request(self, system: str, messages: list[Message]):
        t = self._types
        contents = [
            t.Content(role="model" if m.role == "assistant" else "user",
                      parts=[t.Part(text=m.content)])
            for m in messages
        ]
        config = t.GenerateContentConfig(
            system_instruction=system,
            # note: on Gemini this budget includes thinking tokens
            max_output_tokens=self.max_tokens,
            thinking_config=(t.ThinkingConfig(thinking_level=self.thinking_level)
                             if self.thinking_level else None),
            # no tools yet; also silences the SDK's AFC warning on every streamed call
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
        )
        return dict(model=self.model, contents=contents, config=config)

    def chat(self, system: str, messages: list[Message]) -> str:
        resp = self._client.models.generate_content(**self._request(system, messages))
        return (resp.text or "").strip()

    def stream(self, system: str, messages: list[Message]) -> Iterator[str]:
        for chunk in self._client.models.generate_content_stream(**self._request(system, messages)):
            if chunk.text:
                yield chunk.text
