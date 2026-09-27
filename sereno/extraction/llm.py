"""Claude API client for structured extraction. Strict JSON-schema output only; never free text."""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import anthropic

from sereno.config import get_settings

log = logging.getLogger("sereno.llm")

# Approximate list prices, USD per million tokens (input, output), for cost metrics only.
PRICES = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-opus-4-8": (5.0, 25.0),
          "claude-opus-5-5": (4.0, 20.0), "claude-haiku-4-5": (1.0, 5.0)}


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

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICES.get(self.served_model or self.model, PRICES.get(self.model, (5.0, 25.0)))
        return (self.input_tokens * pin + self.output_tokens * pout) / 1e6


class LLMClient(Protocol):
    def structured(self, *, pass_name: str, model: str, system: str, content: list[dict], schema: dict,
                   max_tokens: int = 64000, effort: str | None = None) -> LLMResult: ...


def image_block(png_or_jpeg: bytes, media_type: str = "image/jpeg") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                       "data": base64.standard_b64encode(png_or_jpeg).decode()}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


class AnthropicLLM:
    def __init__(self):
        s = get_settings()
        self.settings = s
        self.client = anthropic.Anthropic(max_retries=s.llm_max_retries, timeout=s.llm_timeout_s)

    def structured(self, *, pass_name: str, model: str, system: str, content: list[dict], schema: dict,
                   max_tokens: int = 64000, effort: str | None = None) -> LLMResult:
        s = self.settings
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort or s.extraction_effort,
                           "format": {"type": "json_schema", "schema": schema}},
        )
        t0 = time.monotonic()
        try:
            if s.enable_refusal_fallback and model.startswith("claude-opus-5"):
                with self.client.beta.messages.stream(**kwargs, betas=["server-side-fallback-2026-07-01"],
                                                      fallbacks="default") as stream:
                    msg = stream.get_final_message()
            else:
                with self.client.messages.stream(**kwargs) as stream:
                    msg = stream.get_final_message()
        except anthropic.BadRequestError as e:
            raise LLMError(f"bad request: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("rate limited after retries") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("network error reaching the model API") from e
        latency = int((time.monotonic() - t0) * 1000)
        if msg.stop_reason == "refusal":
            raise LLMRefusal("model declined to process this document")
        if msg.stop_reason == "max_tokens":
            raise LLMTruncated("model output was cut off (document too long for one pass)")
        text = next((b.text for b in msg.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise LLMError("model returned no structured output")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError("model returned invalid JSON") from e
        return LLMResult(data=data, model=model, served_model=getattr(msg, "model", None),
                         request_id=getattr(msg, "_request_id", None),
                         input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens,
                         latency_ms=latency, stop_reason=msg.stop_reason)


class FakeLLM:
    """Deterministic stand-in for tests and offline demos. `responder(pass_name, schema, content)`
    returns the dict the model would have produced."""

    def __init__(self, responder: Callable[[str, dict, list], dict]):
        self.responder = responder
        self.calls: list[dict] = []

    def structured(self, *, pass_name: str, model: str, system: str, content: list[dict], schema: dict,
                   max_tokens: int = 64000, effort: str | None = None) -> LLMResult:
        self.calls.append({"pass": pass_name, "model": model, "system": system, "content": content})
        data = self.responder(pass_name, schema, content)
        return LLMResult(data=data, model=model, served_model=model, request_id=f"fake-{len(self.calls)}",
                         input_tokens=1000, output_tokens=500, latency_ms=5, stop_reason="end_turn")


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        if get_settings().llm_backend == "fake":
            raise LLMError("fake backend selected but no FakeLLM installed (set_llm)")
        _client = AnthropicLLM()
    return _client


def set_llm(client: LLMClient | None) -> None:
    global _client
    _client = client
