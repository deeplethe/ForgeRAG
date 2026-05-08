"""
OpenAI-compatible LLM proxy.

The in-container Hermes Agent runtime calls THIS endpoint instead
of OpenAI / Anthropic / etc. directly. Three reasons:

  1. *Server-side keys.* Provider API keys live in our process
     environment (litellm reads ``OPENAI_API_KEY`` /
     ``ANTHROPIC_API_KEY`` / ``GEMINI_API_KEY`` / etc.
     automatically). The container never sees them; it only
     knows about a session token to OUR backend.

  2. *Multi-provider routing.* litellm dispatches by model name —
     Hermes can request ``gpt-4o``, ``claude-3-5-sonnet-...``,
     ``gemini-pro``, a local OpenAI-compatible endpoint, etc.
     and each lands at the right provider with the right key.

  3. *Usage attribution.* Every call carries an authenticated
     ``user_id`` (via the standard auth dep). A follow-up commit
     wires this to a usage table; today we just log.

Endpoint:

    POST /api/v1/llm/v1/chat/completions

The trailing ``/v1`` matches the OpenAI URL convention so a vanilla
OpenAI-compatible client can be pointed at us with just an env-var
flip — Hermes (or any other client) sets

    OPENAI_BASE_URL=http://backend:8000/api/v1/llm/v1
    OPENAI_API_KEY=<our-session-bearer>

and ``client.chat.completions.create(...)`` Just Works because the
client appends ``/chat/completions`` itself.

Streaming:

    {"stream": true} → SSE (``text/event-stream``) with one
    ``data: {chunk-json}\\n\\n`` per delta and a final
    ``data: [DONE]\\n\\n``. Same wire format OpenAI emits, so
    standard clients consume it without special-casing.

Errors are translated to HTTP:
    * litellm-side raises (rate limit / auth / provider down) →
      ``502 Upstream LLM provider error: <Type>`` (non-stream) or
      ``data: {"error": ...}`` followed by ``data: [DONE]`` (stream).
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
from ..deps import get_principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/llm/v1", tags=["llm-proxy"])


class _ChatCompletionsBody(BaseModel):
    """Minimum-required guard. All other OpenAI fields
    (temperature / tools / tool_choice / max_tokens / stop /
    response_format / extra_body / ...) pass through unchanged
    via ``extra="allow"`` — keeping us forward-compatible with
    whatever new parameter Hermes / OpenAI ship next month.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(..., min_length=1)
    messages: list[dict] = Field(..., min_length=1)
    stream: bool = False


@router.post("/chat/completions")
async def chat_completions(
    body: _ChatCompletionsBody,
    principal: AuthenticatedPrincipal = Depends(get_principal),
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
