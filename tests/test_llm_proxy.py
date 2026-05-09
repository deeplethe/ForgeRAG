"""
Tests for ``/api/v1/llm/v1/chat/completions`` — the OpenAI-compatible
LLM proxy that the in-container Hermes Agent runtime hits in lieu of
calling provider APIs directly.

Coverage:
    * non-streaming: kwargs pass through to litellm.completion;
      response is the OpenAI-shaped dict
    * streaming: one ``data: {chunk}`` per delta + trailing ``[DONE]``;
      correct media type + cache-disabling headers
    * extra body fields (temperature, tools, max_tokens, ...) pass
      through unchanged via ``extra="allow"``
    * upstream provider errors → 502 (non-stream) or inline error
      event (stream); stream still ends with ``[DONE]``
    * schema validation: missing ``model`` / empty ``messages`` → 422
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_principal
from api.routes import llm_proxy as llm_proxy_routes


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeModelResponse:
    """Mimics litellm's pydantic ModelResponse just enough to be
    serialised by the route via ``model_dump()``."""

    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return dict(self._payload)


class _FakeChunk:
    """Mimics a streaming ChatCompletionChunk. The route calls
    ``model_dump_json()`` on it and wraps in ``data: ...``."""

    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def model_dump_json(self) -> str:
        return json.dumps(self._payload)


class _FakeLiteLLM:
    """Stub for the ``litellm`` module. The route does
    ``import litellm`` then calls ``litellm.completion(**kwargs)``.

    ``script`` is a callable that receives the kwargs dict and
    returns either a fake response (non-stream) or an iterable of
    fake chunks (stream). It also lets a test raise from inside.
    """

    def __init__(self, script):
        self.script = script
        self.calls: list[dict[str, Any]] = []

    def completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.script(kwargs)


def _principal():
    return SimpleNamespace(
        user_id="u_alice",
        username="alice",
        role="user",
        via="cookie",
    )


# ---------------------------------------------------------------------------
# App + client fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal FastAPI app exposing ONLY the proxy route.

    No AppState, no auth machinery — the principal dep is overridden
    to return a fixed test user. Keeps fixture cost ~0ms; test runs
    are pure route + litellm-stub timing.
    """
    a = FastAPI()
    a.include_router(llm_proxy_routes.router)
    a.dependency_overrides[get_principal] = _principal
    return a


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_litellm(monkeypatch):
    """Install a fake ``litellm`` module so ``import litellm`` inside
    the route resolves to our stub. Each test sets the ``script``
    attribute to control behaviour."""
    holder: dict[str, _FakeLiteLLM] = {}

    def install(script):
        fake = _FakeLiteLLM(script)
        monkeypatch.setitem(sys.modules, "litellm", fake)
        holder["fake"] = fake
        return fake

    return install


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------


def test_chat_completions_non_streaming_passes_through_kwargs(client, fake_litellm):
    payload = {
        "id": "chatcmpl-test-001",
        "object": "chat.completion",
        "created": 1735000000,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi back"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    fake = fake_litellm(lambda _kwargs: _FakeModelResponse(payload))

    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.3,
        "max_tokens": 100,
    }
    r = client.post("/api/v1/llm/v1/chat/completions", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == payload

    # litellm got every field including the extras
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["model"] == "gpt-4o"
    assert call["messages"] == [{"role": "user", "content": "hi"}]
    assert call["temperature"] == 0.3
    assert call["max_tokens"] == 100
    # ``stream`` defaults to False — present in dump unless excluded
    assert call.get("stream") is False


def test_chat_completions_extra_fields_pass_through(client, fake_litellm):
    """OpenAI ships new fields (response_format / tools / parallel_tool_calls
    / ...) regularly. The route must forward whatever the caller sends."""
    fake = fake_litellm(lambda _k: _FakeModelResponse({"choices": []}))

    body = {
        "model": "claude-3-5-sonnet-20241022",
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "response_format": {"type": "json_object"},
        "stop": ["\n\n"],
    }
    r = client.post("/api/v1/llm/v1/chat/completions", json=body)
    assert r.status_code == 200

    call = fake.calls[0]
    assert call["tools"] == body["tools"]
    assert call["tool_choice"] == "auto"
    assert call["parallel_tool_calls"] is True
    assert call["response_format"] == {"type": "json_object"}
    assert call["stop"] == ["\n\n"]


def test_chat_completions_provider_error_returns_502(client, fake_litellm):
    def raises(_kwargs):
        raise RuntimeError("provider down — secret-leaky message")

    fake_litellm(raises)
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}]}
    r = client.post("/api/v1/llm/v1/chat/completions", json=body)
    assert r.status_code == 502
    detail = r.json()["detail"]
    # Exception type name surfaces for debugging; raw message does NOT
    # (avoids leaking provider-side hints to the agent).
    assert "RuntimeError" in detail
    assert "secret-leaky" not in detail


def test_chat_completions_supports_dict_response(client, fake_litellm):
    """Some litellm code paths return a plain dict instead of a
    ModelResponse — handle both."""
    fake_litellm(lambda _k: {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]})
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}]}
    r = client.post("/api/v1/llm/v1/chat/completions", json=body)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "ok"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_chat_completions_streaming_emits_data_chunks_and_done(client, fake_litellm):
    chunks = [
        _FakeChunk({"id": "1", "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": "Hel"}}]}),
        _FakeChunk({"id": "1", "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": "lo"}}]}),
        _FakeChunk({"id": "1", "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
    ]
    fake_litellm(lambda _k: iter(chunks))

    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    with client.stream(
        "POST", "/api/v1/llm/v1/chat/completions", json=body,
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers.get("cache-control") == "no-cache"
        assert r.headers.get("x-accel-buffering") == "no"
        full = b"".join(r.iter_bytes()).decode("utf-8")

    # Three chunk events + the terminator
    parts = [p for p in full.split("\n\n") if p.startswith("data:")]
    assert len(parts) == 4
    assert parts[-1] == "data: [DONE]"
    # First chunk parses back to the expected delta
    first = json.loads(parts[0][len("data: "):])
    assert first["choices"][0]["delta"]["content"] == "Hel"


def test_chat_completions_streaming_provider_error_emits_error_event(client, fake_litellm):
    """If litellm.completion raises during stream init, the SSE
    body emits one ``error`` chunk then ``[DONE]`` so the client
    sees a clean shutdown rather than a hung connection."""
    def raises(_kwargs):
        raise ConnectionError("upstream broken pipe")

    fake_litellm(raises)
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "x"}],
        "stream": True,
    }
    with client.stream(
        "POST", "/api/v1/llm/v1/chat/completions", json=body,
    ) as r:
        # Stream init failures still return 200 — the error rides
        # inside the SSE body so existing OpenAI-compat clients
        # don't trip on a non-200 streaming response.
        assert r.status_code == 200
        full = b"".join(r.iter_bytes()).decode("utf-8")
    parts = [p for p in full.split("\n\n") if p.startswith("data:")]
    assert len(parts) == 2
    err = json.loads(parts[0][len("data: "):])
    assert err["error"]["type"] == "ConnectionError"
    # Raw provider message NOT leaked (same rule as non-stream 502)
    assert "broken pipe" not in err["error"]["message"]
    assert parts[1] == "data: [DONE]"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_chat_completions_missing_model_returns_422(client):
    r = client.post(
        "/api/v1/llm/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 422


def test_chat_completions_empty_messages_returns_422(client):
    r = client.post(
        "/api/v1/llm/v1/chat/completions",
        json={"model": "gpt-4o", "messages": []},
    )
    assert r.status_code == 422


def test_chat_completions_empty_model_returns_422(client):
    r = client.post(
        "/api/v1/llm/v1/chat/completions",
        json={"model": "", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Anthropic-compat surface — /api/v1/llm/anthropic/v1/messages
# ---------------------------------------------------------------------------


class _FakeAnthropicResponse:
    """Mimics litellm's Anthropic Messages response shape."""

    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return dict(self._payload)


class _FakeAnthropicMessages:
    """Stub for litellm.anthropic.messages — its acreate is async."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self._script = None

    def configure(self, script):
        """``script`` returns either a fake response (non-stream) or
        an async iterable of fake chunks (stream). May also raise."""
        self._script = script

    async def acreate(self, **kwargs):
        self.calls.append(kwargs)
        return self._script(kwargs)


class _FakeLiteLLMAnthropic:
    """Stub for the ``litellm`` module exposing only the Anthropic
    surface. The route does ``import litellm`` then calls
    ``litellm.anthropic.messages.acreate(...)``."""

    def __init__(self):
        self.messages = _FakeAnthropicMessages()
        self.anthropic = SimpleNamespace(messages=self.messages)


@pytest.fixture
def app_with_anthropic():
    a = FastAPI()
    a.include_router(llm_proxy_routes.anthropic_router)
    a.dependency_overrides[get_principal] = _principal
    return a


@pytest.fixture
def anthropic_client(app_with_anthropic):
    with TestClient(app_with_anthropic) as c:
        yield c


@pytest.fixture
def fake_anthropic(monkeypatch):
    fake = _FakeLiteLLMAnthropic()
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return fake


def test_anthropic_messages_non_streaming_passes_through(anthropic_client, fake_anthropic):
    payload = {
        "id": "msg_test_001",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-20250514",
        "content": [{"type": "text", "text": "Hello back"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }
    fake_anthropic.messages.configure(
        lambda _kw: _FakeAnthropicResponse(payload)
    )
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 1024,
        "temperature": 0.4,
    }
    r = anthropic_client.post(
        "/api/v1/llm/anthropic/v1/messages", json=body,
    )
    assert r.status_code == 200, r.text
    assert r.json() == payload

    # Forward-compat: extra fields (temperature) thread through
    assert len(fake_anthropic.messages.calls) == 1
    call = fake_anthropic.messages.calls[0]
    assert call["model"] == "claude-sonnet-4-20250514"
    assert call["temperature"] == 0.4
    assert call["max_tokens"] == 1024


def test_anthropic_messages_routes_to_non_anthropic_provider(anthropic_client, fake_anthropic):
    """The whole point of the Anthropic-compat surface: an SDK
    request shaped as Anthropic Messages can route to ANY model
    via the litellm prefix syntax. Confirms the kwargs make it to
    litellm unchanged."""
    fake_anthropic.messages.configure(
        lambda _kw: _FakeAnthropicResponse({"type": "message", "content": []})
    )
    body = {
        "model": "deepseek/deepseek-chat",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 512,
    }
    r = anthropic_client.post(
        "/api/v1/llm/anthropic/v1/messages", json=body,
    )
    assert r.status_code == 200
    assert fake_anthropic.messages.calls[0]["model"] == "deepseek/deepseek-chat"


def test_anthropic_messages_provider_error_returns_502(anthropic_client, fake_anthropic):
    def raises(_kw):
        raise RuntimeError("upstream secret detail")
    fake_anthropic.messages.configure(raises)
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 512,
    }
    r = anthropic_client.post(
        "/api/v1/llm/anthropic/v1/messages", json=body,
    )
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "RuntimeError" in detail
    # Provider raw message NOT leaked
    assert "secret detail" not in detail


def test_anthropic_messages_missing_max_tokens_returns_422(anthropic_client):
    """Anthropic API requires max_tokens — ours mirrors that."""
    r = anthropic_client.post(
        "/api/v1/llm/anthropic/v1/messages",
        json={
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 422


def test_anthropic_messages_empty_messages_returns_422(anthropic_client):
    r = anthropic_client.post(
        "/api/v1/llm/anthropic/v1/messages",
        json={
            "model": "claude-sonnet-4-20250514",
            "messages": [],
            "max_tokens": 100,
        },
    )
    assert r.status_code == 422


def test_anthropic_messages_streaming_emits_anthropic_sse(anthropic_client, fake_anthropic):
    """Anthropic SSE wire format: each chunk gets ``data: {...}\\n\\n``.
    No ``data: [DONE]`` terminator (Anthropic's protocol uses
    explicit ``message_stop`` events as the close signal)."""
    chunk_payloads = [
        {"type": "message_start",
         "message": {"id": "msg_x", "type": "message", "role": "assistant"}},
        {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "message_stop"},
    ]

    class _AsyncIter:
        def __init__(self, items):
            self._items = list(items)
        def __aiter__(self): return self
        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return _FakeChunk(self._items.pop(0))

    fake_anthropic.messages.configure(lambda _kw: _AsyncIter(chunk_payloads))

    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 512,
        "stream": True,
    }
    with anthropic_client.stream(
        "POST", "/api/v1/llm/anthropic/v1/messages", json=body,
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers.get("cache-control") == "no-cache"
        full = b"".join(r.iter_bytes()).decode("utf-8")

    parts = [p for p in full.split("\n\n") if p.startswith("data:")]
    assert len(parts) == 4
    first = json.loads(parts[0][len("data: "):])
    assert first["type"] == "message_start"
    last = json.loads(parts[-1][len("data: "):])
    assert last["type"] == "message_stop"


def test_anthropic_messages_streaming_init_error_emits_error_event(anthropic_client, fake_anthropic):
    def raises(_kw):
        raise ConnectionError("dial tcp: connection refused")
    fake_anthropic.messages.configure(raises)

    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 512,
        "stream": True,
    }
    with anthropic_client.stream(
        "POST", "/api/v1/llm/anthropic/v1/messages", json=body,
    ) as r:
        assert r.status_code == 200
        full = b"".join(r.iter_bytes()).decode("utf-8")

    # An ``event: error`` block precedes the data line on init failure
    assert "event: error" in full
    assert "ConnectionError" in full
    # Provider raw message NOT leaked
    assert "connection refused" not in full
