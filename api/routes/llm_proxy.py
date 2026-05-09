"""
LLM proxy — OpenAI-compat + Anthropic-compat surfaces.

The in-container agent runtime calls THIS backend instead of
OpenAI / Anthropic / etc. directly. Three reasons:

  1. *Server-side keys.* Provider API keys live in our process
     environment (litellm reads ``OPENAI_API_KEY`` /
     ``ANTHROPIC_API_KEY`` / ``GEMINI_API_KEY`` / etc.
     automatically). The container never sees them; it only
     knows about a session token to OUR backend.

  2. *Multi-provider routing.* litellm dispatches by model name —
     the agent can request ``gpt-4o``, ``claude-sonnet-4-...``,
     ``gemini-pro``, ``deepseek/...``, a local OpenAI-compatible
     endpoint, etc. — each lands at the right provider with the
     right key.

  3. *Usage attribution.* Every call carries an authenticated
     ``user_id`` (via the standard auth dep).

Two endpoint families:

    POST /api/v1/llm/v1/chat/completions       (OpenAI shape)
    POST /api/v1/llm/anthropic/v1/messages     (Anthropic shape)

The OpenAI surface is the legacy path used by every OpenAI-SDK
client. The Anthropic surface lands in v0.5.0 to support the
Claude Agent SDK — it sets ``ANTHROPIC_BASE_URL`` and POSTs
``/v1/messages`` per Anthropic convention. Internally both routes
funnel into ``litellm`` which translates wire format ↔ provider
SDK ↔ wire format, so a request shaped as Anthropic Messages can
end up calling DeepSeek / OpenAI / SiliconFlow without the agent
knowing or caring.

Client config:

    # OpenAI-SDK clients
    OPENAI_BASE_URL=http://backend:8000/api/v1/llm/v1
    OPENAI_API_KEY=<our-session-bearer>

    # Claude Agent SDK / anthropic-python clients
    ANTHROPIC_BASE_URL=http://backend:8000/api/v1/llm/anthropic
    ANTHROPIC_API_KEY=<our-session-bearer>

Both endpoints append ``/v1/<resource>`` themselves on the client
side; the URL prefixes above are the bare proxy roots.

Streaming:

    {"stream": true} → SSE (``text/event-stream``) with one
    ``data: {chunk-json}\\n\\n`` per delta. The OpenAI route
    terminates with ``data: [DONE]\\n\\n`` per OpenAI convention;
    the Anthropic route emits ``message_stop`` events per
    Anthropic Messages SSE convention. Same wire format the
    upstream provider emits, so standard clients consume it
    without special-casing.

Errors translate to HTTP:
    * litellm-side raises (rate limit / auth / provider down) →
      ``502 Upstream LLM provider error: <Type>`` (non-stream) or
      an inline ``error`` SSE event followed by stream close.
    * litellm not installed → ``503``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state
from ..state import AppState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/llm/v1", tags=["llm-proxy"])


def _resolve_api_key(state: AppState | None, model: str) -> tuple[str | None, str | None]:
    """Resolve (api_key, api_base) from configured generator.

    The agent's bundled CLI doesn't know our provider keys — it speaks
    the wire protocol of OpenAI / Anthropic and expects the proxy to
    translate. litellm normally reads keys from env vars (``DEEPSEEK_
    API_KEY`` / ``OPENAI_API_KEY`` / ...), but our deployment keeps
    keys in the settings DB / yaml under ``answering.generator``.

    Strategy: read the configured generator's api_key + api_base and
    forward them on every call. The model name in the body chooses
    the provider (litellm dispatches by prefix); the configured key
    is what unlocks it. If the operator wants per-provider routing
    they can set env vars and the configured key acts as the
    fallback (litellm prefers explicit kwargs over env).

    Returns (None, None) if state isn't ready or no generator is
    configured — litellm's env-var path then takes over.
    """
    if state is None:
        return (None, None)
    gen = getattr(getattr(state.cfg, "answering", None), "generator", None)
    if gen is None:
        return (None, None)
    api_key: str | None = None
    if getattr(gen, "api_key", None):
        api_key = gen.api_key
    elif getattr(gen, "api_key_env", None):
        import os
        api_key = os.environ.get(gen.api_key_env)
    api_base = getattr(gen, "api_base", None) or None
    return (api_key, api_base)


class _ChatCompletionsBody(BaseModel):
    """Minimum-required guard. All other OpenAI fields
    (temperature / tools / tool_choice / max_tokens / stop /
    response_format / extra_body / ...) pass through unchanged
    via ``extra="allow"`` — keeping us forward-compatible with
    whatever new parameter the SDK / OpenAI ship next month.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(..., min_length=1)
    messages: list[dict] = Field(..., min_length=1)
    stream: bool = False


@router.post("/chat/completions")
async def chat_completions(
    body: _ChatCompletionsBody,
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> Any:
    try:
        import litellm
    except ImportError as e:  # pragma: no cover - import-time guard
        raise HTTPException(
            status_code=503,
            detail="LLM proxy unavailable: litellm not installed",
        ) from e

    # All known + extra fields → kwargs to litellm.completion.
    # ``exclude_none=True`` drops unset optional fields so we don't
    # accidentally pass ``temperature=None`` to a provider that
    # interprets None as 0.
    kwargs = body.model_dump(exclude_none=True)
    # Inject configured api_key / api_base so litellm doesn't need
    # provider-specific env vars (DEEPSEEK_API_KEY / OPENAI_API_KEY
    # / etc.) — operator-supplied request fields still win.
    cfg_key, cfg_base = _resolve_api_key(state, body.model)
    if cfg_key and "api_key" not in kwargs:
        kwargs["api_key"] = cfg_key
    if cfg_base and "api_base" not in kwargs:
        kwargs["api_base"] = cfg_base

    log.info(
        "llm_proxy: user=%s model=%s stream=%s msgs=%d",
        principal.user_id,
        body.model,
        body.stream,
        len(body.messages),
    )

    if body.stream:
        return StreamingResponse(
            _stream_chunks(litellm, kwargs),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # nginx by default buffers response bodies, which
                # serializes the entire stream before flushing — kills
                # the user-perceived latency advantage of streaming.
                # This header is the documented opt-out.
                "X-Accel-Buffering": "no",
            },
        )

    try:
        resp = litellm.completion(**kwargs)
    except Exception as e:
        log.exception(
            "llm_proxy: completion failed user=%s model=%s",
            principal.user_id,
            body.model,
        )
        # Don't leak provider error details to the caller — the
        # exception type alone is enough for the agent to decide
        # whether to retry or surface to the user.
        raise HTTPException(
            status_code=502,
            detail=f"Upstream LLM provider error: {type(e).__name__}",
        ) from e

    # ``ModelResponse.model_dump()`` returns the OpenAI-shaped dict
    # (id / object / created / model / choices / usage). FastAPI
    # serialises this to JSON for us.
    if hasattr(resp, "model_dump"):
        return resp.model_dump()
    if isinstance(resp, dict):
        return resp
    # Defensive: very old litellm shapes might not have model_dump.
    # Best-effort conversion via __dict__.
    return getattr(resp, "__dict__", {"raw": str(resp)})


def _stream_chunks(litellm_mod, kwargs: dict[str, Any]):
    """Generator yielding SSE-formatted chunks from a litellm
    streaming completion.

    OpenAI's wire format:

        data: {chunk-json}\\n\\n
        ...
        data: [DONE]\\n\\n

    Each chunk is the ``ChatCompletionChunk`` shape — litellm
    normalises whatever the underlying provider sends. We don't
    parse chunks here; just relay them as JSON.
    """
    # Init can fail (auth / network / model-not-found) — surface
    # that as an inline ``error`` event so the client knows the
    # stream ended unsuccessfully without leaving it half-open.
    try:
        stream = litellm_mod.completion(**kwargs)
    except Exception as e:
        log.exception("llm_proxy: stream init failed")
        err_payload = json.dumps(
            {
                "error": {
                    "message": (
                        f"Upstream LLM provider error: {type(e).__name__}"
                    ),
                    "type": type(e).__name__,
                },
            }
        )
        yield f"data: {err_payload}\n\n"
        yield "data: [DONE]\n\n"
        return

    try:
        for chunk in stream:
            try:
                if hasattr(chunk, "model_dump_json"):
                    payload = chunk.model_dump_json()
                elif isinstance(chunk, dict):
                    payload = json.dumps(chunk)
                else:
                    payload = json.dumps(getattr(chunk, "__dict__", {}))
            except Exception:
                # Single bad chunk shouldn't kill the stream — log
                # and skip. The agent will see a slightly truncated
                # response, which is recoverable.
                log.exception("llm_proxy: chunk serialise failed")
                continue
            yield f"data: {payload}\n\n"
    except Exception:
        log.exception("llm_proxy: stream iteration failed")
    finally:
        # Always emit DONE so the client closes cleanly even after
        # mid-stream failures.
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Anthropic-compatible surface — POST /api/v1/llm/anthropic/v1/messages
# ---------------------------------------------------------------------------
#
# The Claude Agent SDK reads ``ANTHROPIC_BASE_URL`` and sends a
# canonical Anthropic Messages API request to ``<base>/v1/messages``.
# This route accepts that shape and funnels it through litellm so
# the request lands at WHATEVER provider OpenCraig is configured for
# — Anthropic itself, OpenAI, DeepSeek, SiliconFlow, Bedrock,
# Vertex, Ollama, etc. The agent stays "Claude-native" on the wire
# while OpenCraig's BYOK story stays multi-provider.

anthropic_router = APIRouter(
    prefix="/api/v1/llm/anthropic/v1", tags=["llm-proxy-anthropic"]
)


class _AnthropicMessagesBody(BaseModel):
    """Anthropic Messages API request body. Same forward-compat
    posture as the OpenAI route — strict on the small set of
    fields we explicitly handle, ``extra=allow`` on everything
    else so newer Claude / litellm parameters flow through
    untouched."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(..., min_length=1)
    messages: list[dict] = Field(..., min_length=1)
    max_tokens: int = Field(..., ge=1)
    stream: bool = False


@anthropic_router.post("/messages")
async def anthropic_messages(
    body: _AnthropicMessagesBody,
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> Any:
    try:
        import litellm
    except ImportError as e:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail="LLM proxy unavailable: litellm not installed",
        ) from e

    kwargs = body.model_dump(exclude_none=True)
    cfg_key, cfg_base = _resolve_api_key(state, body.model)
    if cfg_key and "api_key" not in kwargs:
        kwargs["api_key"] = cfg_key
    if cfg_base and "api_base" not in kwargs:
        kwargs["api_base"] = cfg_base

    log.info(
        "llm_proxy.anthropic: user=%s model=%s stream=%s msgs=%d",
        principal.user_id,
        body.model,
        body.stream,
        len(body.messages),
    )

    # litellm.anthropic.messages.acreate is the official adapter
    # that accepts Anthropic Messages format and routes to any
    # provider via the model prefix. For native Anthropic models
    # (e.g. ``claude-sonnet-4-...``) this is essentially a
    # passthrough; for ``openai/gpt-4o`` / ``deepseek/...`` /
    # ``bedrock/...`` it translates wire formats transparently.
    if body.stream:
        return StreamingResponse(
            _stream_anthropic_chunks(litellm, kwargs),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        resp = await litellm.anthropic.messages.acreate(**kwargs)
    except Exception as e:
        log.exception(
            "llm_proxy.anthropic: messages.acreate failed user=%s model=%s",
            principal.user_id,
            body.model,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Upstream LLM provider error: {type(e).__name__}",
        ) from e

    if hasattr(resp, "model_dump"):
        return resp.model_dump()
    if isinstance(resp, dict):
        return resp
    return getattr(resp, "__dict__", {"raw": str(resp)})


async def _stream_anthropic_chunks(litellm_mod, kwargs: dict[str, Any]):
    """SSE generator for the Anthropic Messages streaming format.

    Anthropic SSE wire shape (one event per ``data:`` block):
        event: message_start
        data: {...}

        event: content_block_start
        data: {...}

        event: content_block_delta
        data: {...}

        event: message_stop
        data: {...}

    litellm's anthropic.messages.acreate(stream=True) yields chunks
    that are already in this shape — we just relay them with the
    ``data:`` envelope so a standard Anthropic SSE consumer (which
    is what the Claude Agent SDK's bundled CLI is) consumes them
    without special-casing.

    On init failure we emit one inline ``error`` event then close
    so the client knows the stream ended unsuccessfully.
    """
    kwargs = dict(kwargs)
    kwargs["stream"] = True
    try:
        stream = await litellm_mod.anthropic.messages.acreate(**kwargs)
    except Exception as e:
        log.exception("llm_proxy.anthropic: stream init failed")
        err_payload = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": type(e).__name__,
                    "message": (
                        f"Upstream LLM provider error: {type(e).__name__}"
                    ),
                },
            }
        )
        yield f"event: error\ndata: {err_payload}\n\n"
        return

    try:
        async for chunk in stream:
            try:
                if hasattr(chunk, "model_dump_json"):
                    payload = chunk.model_dump_json()
                elif isinstance(chunk, dict):
                    payload = json.dumps(chunk)
                else:
                    payload = json.dumps(getattr(chunk, "__dict__", {}))
            except Exception:
                log.exception("llm_proxy.anthropic: chunk serialise failed")
                continue
            yield f"data: {payload}\n\n"
    except Exception:
        log.exception("llm_proxy.anthropic: stream iteration failed")
