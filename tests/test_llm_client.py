from __future__ import annotations

import types

from ncd.llm.llm_client import LLMClient


def _response(text: str):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))]
    )


def _build_client(create_fn):
    client = LLMClient(model_name="gpt-test")
    client._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create_fn),
        )
    )
    return client


def test_llm_client_caches_response_format_rejection() -> None:
    calls = []

    def create_fn(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            raise RuntimeError("response_format is not supported for this model")
        return _response('{"ok": true}')

    client = _build_client(create_fn)

    assert client.extract_json("sys", "user-1") == {"ok": True}
    assert client.extract_json("sys", "user-2") == {"ok": True}

    assert len(calls) == 3
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert "response_format" not in calls[2]


def test_llm_client_caches_temperature_rejection() -> None:
    calls = []

    def create_fn(**kwargs):
        calls.append(kwargs)
        if "temperature" in kwargs:
            raise RuntimeError("temperature unsupported value for this model")
        return _response("ok")

    client = _build_client(create_fn)

    assert client.generate_text("sys", "user-1") == "ok"
    assert client.generate_text("sys", "user-2") == "ok"

    assert len(calls) == 3
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]
    assert "temperature" not in calls[2]


def test_llm_client_truncates_prompt_on_context_limit() -> None:
    calls = []

    def create_fn(**kwargs):
        calls.append(kwargs)
        user_text = kwargs["messages"][1]["content"]
        if len(user_text) > 4500:
            raise RuntimeError("maximum context length exceeded")
        return _response("ok")

    client = _build_client(create_fn)
    long_prompt = "A" * 6000

    assert client.generate_text("sys", long_prompt) == "ok"
    assert len(calls) >= 2
    assert "[TRUNCATED FOR CONTEXT LIMIT:" in calls[-1]["messages"][1]["content"]


def test_llm_client_disable_response_format_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_DISABLE_RESPONSE_FORMAT", "1")
    calls = []

    def create_fn(**kwargs):
        calls.append(kwargs)
        return _response('{"ok": true}')

    client = _build_client(create_fn)
    assert client.extract_json("sys", "user") == {"ok": True}
    assert len(calls) == 1
    assert "response_format" not in calls[0]
