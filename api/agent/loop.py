"""
Agent loop — the orchestration layer that drives an LLM through
tool calls until it has enough to answer.

One method, ``AgentLoop.run``, takes a user message + ToolContext
and returns an ``AgentResult`` with the answer + citations + per-
tool trace. The loop:

  1. Sends user message + tool catalogue to the LLM.
  2. LLM either:
       (a) emits text only → "implicit done"; loop exits.
       (b) emits one or more tool_use blocks → loop dispatches them
           in parallel, feeds results back as tool_result messages,
           and continues.
  3. Three hard budgets bound the loop:
       * ``max_iterations``  — number of LLM turns
       * ``max_tool_calls``  — total tool calls across all turns
       * ``max_wall_time_s`` — wall clock
     When any is hit, force a synthesis turn (``tool_choice="none"``)
     so the LLM produces a final answer from whatever it has, then
     return with ``stop_reason`` flagging which budget triggered.

Citations: at the end, ``ctx.citation_pool`` holds every chunk any
tool returned. We attach it as the result's ``citations`` field
sorted by score desc — the frontend renders them under the answer
the same way the old fixed pipeline did.

Why no streaming yet: this commit is the synchronous backbone.
SSE event streaming (``agent.turn_start`` / ``tool.call_start`` /
``answer.delta`` / ``done``) is step 4 of the rewrite — we'll
adapt this loop to yield events rather than hold them in
``ctx.tool_calls_log`` for after-the-fact retrieval.

Why no live LLM calls in tests: the LLMClient protocol is small
(one ``chat`` method) so a stub LLM in ``tests/test_agent_loop.py``
returns canned responses in sequence. Every loop branch — direct
answer, single tool, parallel tools, multi-turn, each budget cap,
tool error recovery — is exercised against the stub.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .dispatch import ToolContext, dispatch
from .llm import LLMClient, LLMResponse, ToolCall
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_REGISTRY

log = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Per-instance config for the agent loop.

    Defaults match the v1 sizing we discussed:
      * 6 LLM iterations (each can have many tool calls)
      * 8 total tool calls per query
      * 30s wall-clock cap
      * temperature 0 — agentic loops want determinism, not
        creativity
    """

    model: str = "anthropic/claude-sonnet-4-5"
    api_key: str | None = None
    api_base: str | None = None

    max_iterations: int = 6
    max_tool_calls: int = 8
    max_wall_time_s: float = 30.0

    temperature: float = 0.0
    max_tokens: int = 4096

    # Cap on parallel tool execution within a single turn.
    # If the LLM emits 6 tool_use blocks, we run min(6, parallel_workers)
    # at a time. Tools are ~all I/O bound (DB / vector / web), so the
    # ceiling is set by sane DB connection use, not CPU.
    parallel_workers: int = 4


@dataclass
class AgentResult:
    """What ``AgentLoop.run`` returns.

    Stop-reason values:
      * "done"           — LLM finished naturally (no tool_use last turn)
      * "max_iterations" — hit ``cfg.max_iterations``
      * "max_tool_calls" — hit ``cfg.max_tool_calls``
      * "max_wall_time"  — hit ``cfg.max_wall_time_s``
      * "error"          — LLM call failed catastrophically
    """

    answer: str
    citations: list[dict] = field(default_factory=list)
    tool_calls_log: list[dict] = field(default_factory=list)
    stop_reason: str = "done"
    iterations: int = 0
    tool_calls_count: int = 0
    total_latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class AgentLoop:
    """Bounded LLM-driven retrieval loop.

    Construct once per process (or per-request — it's stateless;
    state lives in ToolContext). ``run`` is safe to call concurrently
    from different threads provided each call passes its own
    ToolContext.
    """

    def __init__(self, cfg: AgentConfig, llm: LLMClient):
        self.cfg = cfg
        self.llm = llm

    def run(
        self,
        user_message: str,
        ctx: ToolContext,
        *,
        history: list[dict] | None = None,
    ) -> AgentResult:
        """Run one chat turn end-to-end.

        ``history`` is an optional list of prior conversation
        messages in OpenAI message format (``[{"role", "content"}]``).
        Tool-result messages from earlier turns can be omitted —
        only the user/assistant text turns are needed for context;
        the citation pool is per-query, not per-conversation.
        """
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # Tool catalogue sent every turn — Anthropic / OpenAI both
        # cache the tool block when prompt caching is on, so this
        # isn't paid per turn after the first.
        tools = [spec.to_openai_tool() for spec in TOOL_REGISTRY.values()]

        iterations = 0
        tool_calls_count = 0
        tokens_in = 0
        tokens_out = 0
        t0 = time.time()

        while iterations < self.cfg.max_iterations:
            elapsed = time.time() - t0

            # Pre-flight budget check. Hit → synthesis turn.
            budget_hit = self._check_budget(tool_calls_count, elapsed)
            if budget_hit is not None:
                synth = self._synthesise(messages)
                tokens_in += synth.tokens_in
                tokens_out += synth.tokens_out
                return self._result(
                    answer=synth.text,
                    ctx=ctx,
                    stop_reason=budget_hit,
                    iterations=iterations,
                    tool_calls_count=tool_calls_count,
                    t0=t0,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )

            # Normal LLM turn.
            try:
                resp = self.llm.chat(
                    messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_tokens,
                )
            except Exception:
                log.exception("LLM chat failed in agent loop")
                return self._result(
                    answer="",
                    ctx=ctx,
                    stop_reason="error",
                    iterations=iterations,
                    tool_calls_count=tool_calls_count,
                    t0=t0,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            iterations += 1
            tokens_in += resp.tokens_in
            tokens_out += resp.tokens_out

            # Implicit done: LLM emitted text only.
            if not resp.tool_calls:
                return self._result(
                    answer=resp.text,
                    ctx=ctx,
                    stop_reason="done",
                    iterations=iterations,
                    tool_calls_count=tool_calls_count,
                    t0=t0,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )

            # Cap how many tool_use blocks we execute this turn so
            # the LLM can't bypass the budget by emitting 100 in a
            # single message. Excess get silently dropped — the LLM
            # will see only the executed ones in tool_result and
            # re-plan.
            requested = resp.tool_calls
            remaining = self.cfg.max_tool_calls - tool_calls_count
            if len(requested) > remaining:
                requested = requested[:remaining]

            results = self._execute_parallel(requested, ctx)
            tool_calls_count += len(requested)

            # Append assistant message + tool_result messages so the
            # next turn sees the conversation correctly.
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.text,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in requested
                    ],
                }
            )
            for tc in requested:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(results[tc.id]),
                    }
                )

        # Hit max_iterations — synthesise.
        synth = self._synthesise(messages)
        tokens_in += synth.tokens_in
        tokens_out += synth.tokens_out
        return self._result(
            answer=synth.text,
            ctx=ctx,
            stop_reason="max_iterations",
            iterations=iterations,
            tool_calls_count=tool_calls_count,
            t0=t0,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_budget(
        self, tool_calls_count: int, elapsed: float
    ) -> str | None:
        if tool_calls_count >= self.cfg.max_tool_calls:
            return "max_tool_calls"
        if elapsed >= self.cfg.max_wall_time_s:
            return "max_wall_time"
        return None

    def _synthesise(self, messages: list[dict]) -> LLMResponse:
        """Forced final turn — no tools allowed.

        Used when budgets cap the loop. ``tool_choice="none"`` tells
        the LLM "give me an answer based on what you already have,
        don't ask for more". Robust to LLM failures: returns an
        empty-text response so the agent loop still terminates.
        """
        try:
            return self.llm.chat(
                messages,
                tools=None,
                tool_choice="none",
                temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_tokens,
            )
        except Exception:
            log.exception("LLM synthesis turn failed")
            return LLMResponse(text="", stop_reason="error")

    def _execute_parallel(
        self, requested: list[ToolCall], ctx: ToolContext
    ) -> dict[str, dict]:
        """Run tool calls concurrently; return ``{call_id: result}``.

        Order in the results dict mirrors call order so the next
        turn's tool_result messages can be appended deterministically.
        """
        results: dict[str, dict] = {}
        if not requested:
            return results
        workers = max(1, min(len(requested), self.cfg.parallel_workers))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(dispatch, tc.name, tc.arguments, ctx): tc
                for tc in requested
            }
            for f, tc in futures.items():
                try:
                    results[tc.id] = f.result()
                except Exception as e:
                    # Should be unreachable — dispatch catches inside
                    # — but belt-and-suspenders.
                    log.exception("dispatch raised in agent loop")
                    results[tc.id] = {
                        "error": f"dispatch failed: {type(e).__name__}",
                        "tool": tc.name,
                    }
        return results

    def _result(
        self,
        *,
        answer: str,
        ctx: ToolContext,
        stop_reason: str,
        iterations: int,
        tool_calls_count: int,
        t0: float,
        tokens_in: int,
        tokens_out: int,
    ) -> AgentResult:
        return AgentResult(
            answer=answer or "",
            citations=_collect_citations(ctx),
            tool_calls_log=ctx.tool_calls_log,
            stop_reason=stop_reason,
            iterations=iterations,
            tool_calls_count=tool_calls_count,
            total_latency_ms=int((time.time() - t0) * 1000),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


def _collect_citations(ctx: ToolContext) -> list[dict]:
    """Snapshot the citation pool into a JSON-safe list, sorted
    by score desc (highest signal first).

    ``sources`` is a set inside the pool (so cross-tool merging
    is O(1)); we serialise to a sorted list so the frontend gets
    deterministic output.
    """
    out: list[dict] = []
    for rec in ctx.citation_pool.values():
        item = dict(rec)
        if isinstance(item.get("sources"), set):
            item["sources"] = sorted(item["sources"])
        out.append(item)
    out.sort(key=lambda r: -(r.get("score") or 0.0))
    return out
