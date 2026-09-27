"""AnthropicLLM behaviour that can be tested without network: fallback-beta degradation, refusal
and truncation handling, key-less demo guard."""
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from sereno.extraction import llm


def _msg(data: dict, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(data))],
                           stop_reason=stop, model="claude-opus-5", usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class _Stream:
    def __init__(self, result):
        self.result = result

    def __enter__(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.result


def _client(beta_result, plain_result):
    calls = []

    def beta_stream(**kw):
        calls.append(("beta", kw.get("fallbacks")))
        return _Stream(beta_result)

    def plain_stream(**kw):
        calls.append(("plain", None))
        return _Stream(plain_result)
    c = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=beta_stream)),
                        messages=SimpleNamespace(stream=plain_stream))
    return c, calls


def _bad_request(msg="fallbacks: unknown beta"):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(msg, response=httpx2.Response(400, request=req), body=None)


def _llm(client):
    o = object.__new__(llm.AnthropicLLM)
    o.settings, o.client = llm.get_settings(), client
    return o


def test_fallback_beta_rejection_degrades_once_and_is_remembered():
    llm.AnthropicLLM._fallback_off = False
    client, calls = _client(_bad_request(), _msg({"ok": 1}))
    r = _llm(client).structured(pass_name="primary", model="claude-opus-5", system="s", content=[], schema={})
    assert r.data == {"ok": 1} and calls == [("beta", "default"), ("plain", None)]
    assert llm.AnthropicLLM._fallback_off
    calls.clear()
    _llm(client).structured(pass_name="primary", model="claude-opus-5", system="s", content=[], schema={})
    assert calls == [("plain", None)]  # no repeated 400s for the rest of the run
    llm.AnthropicLLM._fallback_off = False


def test_sonnet_never_uses_fallback_beta():
    client, calls = _client(_bad_request(), _msg({"ok": 1}))
    _llm(client).structured(pass_name="triage", model="claude-sonnet-5", system="s", content=[], schema={})
    assert calls == [("plain", None)]


def test_refusal_and_truncation_are_explicit():
    client, _ = _client(_msg({}, "refusal"), None)
    with pytest.raises(llm.LLMRefusal):
        _llm(client).structured(pass_name="primary", model="claude-opus-5", system="s", content=[], schema={})
    client, _ = _client(_msg({}, "max_tokens"), None)
    with pytest.raises(llm.LLMTruncated):
        _llm(client).structured(pass_name="primary", model="claude-opus-5", system="s", content=[], schema={})


def test_ai_ready_false_without_credentials(monkeypatch, tmp_path):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    llm.set_llm(None)
    assert not llm.ai_ready()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert llm.ai_ready()
