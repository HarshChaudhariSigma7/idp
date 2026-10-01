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
    from sereno import config
    monkeypatch.setenv("SERENO_LLM_BACKEND", "anthropic")
    config.get_settings.cache_clear()
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    llm.set_llm(None)
    assert not llm.ai_ready()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert llm.ai_ready()
    config.get_settings.cache_clear()


def test_ai_ready_false_without_gemini_key(monkeypatch, tmp_path):
    from sereno import config
    monkeypatch.setenv("SERENO_LLM_BACKEND", "gemini")
    config.get_settings.cache_clear()
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    llm.set_llm(None)
    assert not llm.ai_ready()
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert llm.ai_ready()
    config.get_settings.cache_clear()


def _conn_error(cause: Exception | None = None):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    e = anthropic.APIConnectionError(message="Connection error.", request=req)
    e.__cause__ = cause
    return e


def test_connection_error_retries_then_falls_back_to_non_streaming(monkeypatch):
    # every streamed attempt fails; the client keeps retrying rather than giving up immediately,
    # and the final attempt uses .create() (no streaming) instead of raising
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    calls = []

    def always_fails_stream(**kw):
        calls.append("stream")
        raise _conn_error(TimeoutError("timed out"))

    def succeeds_create(**kw):
        calls.append("create")
        return _msg({"ok": 1})
    c = SimpleNamespace(messages=SimpleNamespace(stream=always_fails_stream, create=succeeds_create))
    o = _llm(c)
    o.settings = llm.get_settings().model_copy(update={"llm_max_retries": 1})
    r = o.structured(pass_name="primary", model="claude-sonnet-5", system="s", content=[], schema={})
    assert r.data == {"ok": 1} and calls == ["stream", "create"]


def test_tls_record_corruption_switches_client_and_retries_immediately(monkeypatch):
    llm.AnthropicLLM._tls_workaround = False
    attempts = []

    def plain_stream(**kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise _conn_error(Exception("[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac"))
        return _Stream(_msg({"ok": 1}))
    c = SimpleNamespace(messages=SimpleNamespace(stream=plain_stream, create=lambda **kw: _msg({"ok": 1})))
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda **kw: c)  # the rebuilt client after the switch
    try:
        r = _llm(c).structured(pass_name="primary", model="claude-sonnet-5", system="s", content=[], schema={})
        assert r.data == {"ok": 1}
        assert llm.AnthropicLLM._tls_workaround is True
        assert len(attempts) == 2  # failed once, switched, succeeded on the very next try (no backoff wait)
    finally:
        llm.AnthropicLLM._tls_workaround = False
