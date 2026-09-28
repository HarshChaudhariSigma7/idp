"""Gemini API client for structured extraction, behind the same `LLMClient` protocol as
`AnthropicLLM`. Strict JSON-schema output only; never free text.

Uses `response_json_schema` (full JSON Schema, including `$ref`/`$defs`/`additionalProperties`)
rather than the older OpenAPI-subset `response_schema` field, so the union-free schemas in
`doc_specs.py` pass through unmodified. Content blocks arrive in Claude's `{"type": "image"/"text"}`
shape (the pipeline builds them once via `image_block`/`text_block`); this module converts them.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from sereno.config import get_settings
from sereno.extraction.llm_types import LLMError, LLMRefusal, LLMResult, LLMTruncated

log = logging.getLogger("sereno.llm.gemini")

# Effort levels used elsewhere in the pipeline ("high"/"xhigh" for hard documents) don't map
# 1:1 onto Gemini's thinking controls; this is our best-effort translation.
_THINKING_LEVEL = {"low": "low", "high": "high", "xhigh": "high"}


def _content_to_parts(content: list[dict]) -> list[types.Part]:
    parts = []
    for block in content:
        if block["type"] == "text":
            parts.append(types.Part.from_text(text=block["text"]))
        elif block["type"] == "image":
            src = block["source"]
            parts.append(types.Part.from_bytes(data=base64.standard_b64decode(src["data"]),
                                               mime_type=src["media_type"]))
    return parts


class GeminiLLM:
    def __init__(self):
        s = get_settings()
        self.settings = s
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.client = genai.Client(api_key=api_key)

    def _send_with_retry(self, model: str, content: list[dict], config: "types.GenerateContentConfig"):
        # 503 (model overloaded) and 429 (rate limited) are transient and common under real load;
        # the SDK does not retry them for us, so back off and retry a handful of times before
        # surfacing an error (the retry count matches the Anthropic client's llm_max_retries).
        parts = _content_to_parts(content)
        last: Exception | None = None
        for attempt in range(self.settings.llm_max_retries + 1):
            try:
                return self.client.models.generate_content(
                    model=model, contents=[types.Content(role="user", parts=parts)], config=config)
            except genai_errors.ServerError as e:
                last = e
                if getattr(e, "code", None) not in (503, 500) or attempt == self.settings.llm_max_retries:
                    raise LLMError(f"Gemini API server error: {e}") from e
            except genai_errors.ClientError as e:
                last = e
                if getattr(e, "code", None) != 429 or attempt == self.settings.llm_max_retries:
                    raise LLMError(_explain(e)) from e
            wait = 2 ** attempt
            log.warning("Gemini API transient error (attempt %d/%d), retrying in %ds: %s",
                       attempt + 1, self.settings.llm_max_retries + 1, wait, last)
            time.sleep(wait)
        raise LLMError(f"Gemini API error after retries: {last}")  # pragma: no cover - unreachable

    def structured(self, *, pass_name: str, model: str, system: str, content: list[dict], schema: dict,
                   max_tokens: int = 64000, effort: str | None = None) -> LLMResult:
        cfg_kwargs = dict(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
            max_output_tokens=max_tokens,
        )
        level = _THINKING_LEVEL.get(effort or self.settings.extraction_effort)
        if level:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level)
        try:
            config = types.GenerateContentConfig(**cfg_kwargs)
        except TypeError:  # older SDK: thinking_level or response_json_schema not yet supported
            cfg_kwargs.pop("thinking_config", None)
            try:
                config = types.GenerateContentConfig(**cfg_kwargs)
            except TypeError:
                cfg_kwargs["response_schema"] = cfg_kwargs.pop("response_json_schema")
                config = types.GenerateContentConfig(**cfg_kwargs)
        t0 = time.monotonic()
        resp = self._send_with_retry(model, content, config)
        latency = int((time.monotonic() - t0) * 1000)
        cand = resp.candidates[0] if resp.candidates else None
        finish = getattr(cand, "finish_reason", None)
        finish = getattr(finish, "name", finish)
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST"):
            raise LLMRefusal(f"model declined to process this document ({finish})")
        if finish == "MAX_TOKENS":
            raise LLMTruncated("model output was cut off (document too long for one pass)")
        text = getattr(resp, "text", None)
        if not text:
            raise LLMError("model returned no structured output")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError("model returned invalid JSON") from e
        usage = resp.usage_metadata
        return LLMResult(data=data, model=model, served_model=getattr(resp, "model_version", model) or model,
                         request_id=getattr(resp, "response_id", None),
                         input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                         output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                         latency_ms=latency, stop_reason=str(finish) if finish else "STOP")


def _explain(e: "genai_errors.ClientError") -> str:
    code = getattr(e, "code", None)
    if code == 401 or code == 403:
        return "Gemini API key was rejected (check GEMINI_API_KEY)"
    if code == 404:
        return f"model not available to this API key: {e}"
    if code == 429:
        return "rate limited after retries"
    return f"Gemini API error {code}: {e}"


def gemini_ready() -> bool:
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return True
    return (Path.home() / ".config" / "gcloud" / "application_default_credentials.json").exists()
