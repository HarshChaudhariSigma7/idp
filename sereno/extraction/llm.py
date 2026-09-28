"""Claude API client for structured extraction. Strict JSON-schema output only; never free text.
Provider-agnostic types (LLMResult, LLMError, the LLMClient protocol, image_block/text_block,
PRICES) live in llm_types.py and are re-exported here so existing imports keep working; the split
avoids a circular import with gemini_llm.py, which also needs those types."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

import anthropic

from sereno.config import get_settings
from sereno.extraction.llm_types import (LLMClient, LLMError, LLMRefusal, LLMResult, LLMTruncated,
                                         PRICES, image_block, text_block)

__all__ = ["LLMClient", "LLMError", "LLMRefusal", "LLMResult", "LLMTruncated", "PRICES", "image_block",
          "text_block", "AnthropicLLM", "FakeLLM", "get_llm", "ai_ready", "set_llm"]

log = logging.getLogger("sereno.llm")


class AnthropicLLM:
    _fallback_off = False  # set once if the fallback beta is rejected for this account

    def __init__(self):
        s = get_settings()
        self.settings = s
        self.client = anthropic.Anthropic(max_retries=s.llm_max_retries, timeout=s.llm_timeout_s)

    def _send(self, kwargs: dict, use_fallback: bool):
        if use_fallback:
            with self.client.beta.messages.stream(**kwargs, betas=["server-side-fallback-2026-07-01"],
                                                  fallbacks="default") as stream:
                return stream.get_final_message()
        with self.client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

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
        use_fallback = s.enable_refusal_fallback and model.startswith("claude-opus-5") and not AnthropicLLM._fallback_off
        try:
            msg = self._send(kwargs, use_fallback)
        except (anthropic.BadRequestError, anthropic.PermissionDeniedError) as e:
            if not use_fallback:
                raise LLMError(f"bad request: {e.message}") from e
            # The server-side fallback beta may not be enabled for this account/region. Accuracy does
            # not depend on it (refusals already route to manual entry), so drop it and retry once.
            log.warning("refusal-fallback beta rejected (%s); continuing without it", e.message)
            AnthropicLLM._fallback_off = True
            try:
                msg = self._send(kwargs, False)
            except anthropic.APIStatusError as e2:
                raise LLMError(_explain(e2)) from e2
        except anthropic.APIStatusError as e:
            raise LLMError(_explain(e)) from e
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


def _explain(e: "anthropic.APIStatusError") -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "Anthropic API key was rejected (check ANTHROPIC_API_KEY)"
    if isinstance(e, anthropic.NotFoundError):
        return f"model not available to this API key: {e.message}"
    if isinstance(e, anthropic.RateLimitError):
        return "rate limited after retries"
    if isinstance(e, anthropic.BadRequestError):
        return f"bad request: {e.message}"
    return f"API error {e.status_code}: {e.message}"


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
        backend = get_settings().llm_backend
        if backend == "fake":
            raise LLMError("fake backend selected but no FakeLLM installed (set_llm)")
        if backend == "gemini":
            from sereno.extraction.gemini_llm import GeminiLLM
            _client = GeminiLLM()
        else:
            _client = AnthropicLLM()
    return _client


def ai_ready() -> bool:
    """Can documents be read right now? False in a key-less local demo: uploads are refused with a
    plain message instead of failing later in the background."""
    if _client is not None:
        return True
    backend = get_settings().llm_backend
    if backend == "fake":
        return False
    if backend == "gemini":
        from sereno.extraction.gemini_llm import gemini_ready
        return gemini_ready()
    if any(os.getenv(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
                                  "ANTHROPIC_FEDERATION_RULE_ID")):
        return True
    return (Path.home() / ".config" / "anthropic").exists()  # `ant auth login` profile


def set_llm(client: LLMClient | None) -> None:
    global _client
    _client = client
