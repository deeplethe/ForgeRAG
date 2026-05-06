"""
LLM client abstraction for the agent loop.

Wraps litellm so the agent code talks to ONE small protocol — easy
to mock in tests, easy to swap providers (Anthropic / OpenAI /
DeepSeek / etc.) by changing the model string.

Surface:

    LLMClient (Protocol)            — what the agent loop talks to
    LiteLLMClient                   — production impl, uses litellm
    LLMResponse                     — normalised result shape
    ToolCall                        — one parsed tool_use from the LLM

The OpenAI-style tools format is the lingua franca here — litellm
translates to/from Anthropic's native tool_use blocks under the hood.
We give litellm tools as ``[{"type": "function", "function": {...}}]``
and read its normalised ``message.tool_calls`` back.

Test stubbing: the protocol is small (one ``chat`` method) so a
test can substitute a deterministic stub that returns canned
``LLMResponse`` objects in sequence. See ``tests/test_agent_loop.py``
for the pattern.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """One tool_use block parsed from an LLM response.

    ``arguments`` is the parsed dict (NOT the raw JSON string the
    provider returns). Parsing happens in ``LiteLLMClient.chat``
    so the agent loop doesn't repeat the same try/except.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Normalised LLM turn result.

    * ``text`` is the LLM's natural-language content. Empty string
      when the LLM only emitted tool_use (Anthropic returns no text
      block in that case; OpenAI returns ``content=null``).
    * ``tool_calls`` is the parsed list of ToolCall — empty when
      the LLM is "done" (the implicit done signal — see
      ``api/agent/loop.py``).
    * ``stop_reason`` mirrors the provider's finish_reason verbatim
      so callers can distinguish "stop" / "tool_calls" /
      "max_tokens" / "length" / etc.
    * ``tokens_in`` / ``tokens_out`` come from the provider's usage
      block. Zero when usage data is unavailable.
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "stop"
    tokens_in: int = 0
    tokens_out: int = 0


class LLMClient(Protocol):
    """One method protocol — keep the test surface tiny."""

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse: ...


class LiteLLMClient:
    """Production impl. Lazy-imports litellm so this module is
    importable in environments without it (used for tests, scripts,
    type checks)."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout: float = 45.0,
    ):
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.timeout = timeout
        self._litellm = None

    def _ensure(self):
        if self._litellm is not None:
            return self._litellm
        try:
            import litellm
        except ImportError as e:
            raise RuntimeError("LiteLLMClient requires litellm") from e
        self._litellm = litellm
        return litellm

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        litellm = self._ensure()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": self.timeout,
            # Disable extended thinking / CoT for agentic loops.
            # Two reasons:
            #   1. The agent ITSELF is the thinking layer — it
            #      iterates with tool calls. An extra thinking
            #      block before each tool decision burns tokens
            #      with no gain.
            #   2. Some thinking models (notably DeepSeek R1) emit
            #      thinking content that confuses tool_use parsers
            #      when paired with a small max_tokens cap.
            # Same flag as retrieval/query_understanding.py uses
            # for the same reason. LiteLLM passes ``extra_body``
            # through to providers that understand it (Anthropic);
            # others ignore it.
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if tools:
            kwargs["tools"] = tools
            # ``tool_choice="none"`` forbids tool calls (forced
            # synthesis turn). ``"auto"`` lets the model decide —
            # the standard agent loop case.
            kwargs["tool_choice"] = tool_choice
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base

        resp = litellm.completion(**kwargs)
        return _parse_response(resp)

    def chat_stream(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ):
        """Streaming variant of ``chat`` — yields ``("delta", text)``
        for each content chunk and finally ``("done", LLMResponse)``
        with the assembled full response.

        Used by the agent loop ONLY for the final-answer turn: when
        the LLM is producing the user-facing text, stream it so the
        chat UI can render token-by-token. Tool-decision turns stay
        non-streaming (``chat``) — they need the full ``tool_calls``
        list before the loop can dispatch.
        """
        litellm = self._ensure()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": self.timeout,
            "stream": True,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base

        full_text = ""
        finish_reason = "stop"
        tokens_in = 0
        tokens_out = 0
        # Tool-call accumulation across delta chunks. OpenAI-style
        # streams send partial tool_call fragments keyed by ``index``;
        # the function name arrives in the first chunk for that
        # index, ``arguments`` is built up across subsequent chunks
        # as a JSON string. We assemble per-index then parse the
        # final JSON when the stream ends.
        tc_acc: dict[int, dict[str, Any]] = {}
        try:
            stream = litellm.completion(**kwargs)
            for chunk in stream:
                try:
                    choice0 = chunk.choices[0]
                    delta = choice0.delta
                except (AttributeError, IndexError):
                    continue
                content = getattr(delta, "content", None)
                if content:
                    full_text += content
                    yield ("delta", content)
                # Accumulate tool_calls partials.
                raw_tcs = getattr(delta, "tool_calls", None) or []
                for raw_tc in raw_tcs:
                    idx = getattr(raw_tc, "index", None)
                    if idx is None:
                        continue
                    slot = tc_acc.setdefault(
                        idx, {"id": None, "name": None, "args_str": ""}
                    )
                    if getattr(raw_tc, "id", None):
                        slot["id"] = raw_tc.id
                    fn = getattr(raw_tc, "function", None)
                    if fn:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        args = getattr(fn, "arguments", None)
                        if args:
                            slot["args_str"] += args
                fr = getattr(choice0, "finish_reason", None)
                if fr:
                    finish_reason = fr
                # Some providers stream usage on the FINAL chunk only.
                usage = getattr(chunk, "usage", None)
                if usage:
                    tokens_in = int(getattr(usage, "prompt_tokens", 0) or tokens_in)
                    tokens_out = int(getattr(usage, "completion_tokens", 0) or tokens_out)
        except Exception:  # noqa: BLE001
            log.exception("LiteLLM streaming failed")
            yield (
                "done",
                LLMResponse(text=full_text, stop_reason="error",
                            tokens_in=tokens_in, tokens_out=tokens_out),
            )
            return

        # Materialise accumulated tool calls.
        tool_calls: list[ToolCall] = []
        for idx in sorted(tc_acc.keys()):
            slot = tc_acc[idx]
            if not slot.get("id") or not slot.get("name"):
                continue
            try:
                args = json.loads(slot["args_str"] or "{}")
            except json.JSONDecodeError:
                log.warning("malformed tool_call args in stream: %r", slot["args_str"])
                args = {}
            tool_calls.append(
                ToolCall(id=slot["id"], name=slot["name"], arguments=args)
            )

        yield (
            "done",
            LLMResponse(
                text=full_text,
                tool_calls=tool_calls,
                stop_reason=finish_reason,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            ),
        )


def _parse_response(resp: Any) -> LLMResponse:
    """Normalise the litellm response into our LLMResponse shape.

    Robust to providers that omit ``usage`` or return ``content=None``;
    the agent loop wants a stable struct to read.
    """
    try:
        choice = resp.choices[0]
        msg = choice.message
    except (AttributeError, IndexError) as e:
        log.warning("LLM response missing choices: %s", e)
        return LLMResponse(stop_reason="error")

    text = getattr(msg, "content", None) or ""

    tool_calls: list[ToolCall] = []
    raw_tcs = getattr(msg, "tool_calls", None) or []
    for tc in raw_tcs:
        # litellm normalises to OpenAI-style:
        #   {id, type: "function", function: {name, arguments: <json string>}}
        try:
            tc_id = tc.id
            fn = tc.function
            name = fn.name
            args_raw = fn.arguments or "{}"
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
        except (AttributeError, json.JSONDecodeError) as e:
            log.warning("malformed tool_call from LLM: %s", e)
            continue
        tool_calls.append(ToolCall(id=tc_id, name=name, arguments=args))

    stop_reason = getattr(choice, "finish_reason", None) or "stop"

    usage = getattr(resp, "usage", None)
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0

    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )
