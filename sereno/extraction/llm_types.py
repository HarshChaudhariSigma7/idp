"""Provider-agnostic types shared by every LLM client (`llm.py`'s `AnthropicLLM`,
`gemini_llm.py`'s `GeminiLLM`, and `FakeLLM`), split out to avoid a circular import between them."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol

# Approximate list prices, USD per million tokens (input, output), for cost metrics only; verify
# against the current provider pricing page before relying on these for billing.
PRICES = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-4-8": (5.0, 25.0),
          "claude-opus-5-5": (4.0, 20.0), "claude-haiku-4-5": (1.0, 5.0),
          "gemini-3.1-pro": (2.0, 12.0), "gemini-3-pro": (2.0, 12.0), "gemini-3.8-flash": (0.75, 3.75),
          "gemini-3.7-flash": (0.75, 3.75), "gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-pro": (1.25, 10.0),
          "gemini-3.1-flash-lite": (0.25, 1.50), "gemini-3.5-flash-lite": (0.25, 1.50)}


class LLMError(Exception):
    pass


class LLMRefusal(LLMError):
    pass


class LLMTruncated(LLMError):
    pass


@dataclass
class LLMResult:
    data: dict
    model: str
    served_model: str | None
    request_id: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    stop_reason: str | None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        name = self.served_model or self.model
        pin, pout = PRICES.get(name, PRICES.get(self.model, (5.0, 25.0)))
        read = 0.05 if name.startswith("claude-opus-5-5") else 0.1  # cache-read multiplier
        return (self.input_tokens * pin + self.cache_write_tokens * pin * 1.25 + self.cache_read_tokens * pin * read
                + self.output_tokens * pout) / 1e6


class LLMClient(Protocol):
    def structured(self, *, pass_name: str, model: str, system: str, content: list[dict], schema: dict,
                   max_tokens: int = 64000, effort: str | None = None) -> LLMResult: ...


def image_block(png_or_jpeg: bytes, media_type: str = "image/jpeg") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                       "data": base64.standard_b64encode(png_or_jpeg).decode()}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}
