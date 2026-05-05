"""
Agent loop — the orchestration layer that drives an LLM through
tool calls until it has enough to answer.

Two public entry points:

    AgentLoop.stream(...) → Iterator[Event]
        Generator yielding events as the loop runs. Each event is a
        plain dict serialisable by ``json.dumps`` — directly
        consumable by an SSE / WebSocket layer for live frontend
        feedback ("Turn 1 · search_bm25 · 120ms"). Final event has
        ``type: "done"`` and carries the assembled summary.

    AgentLoop.run(...)     → AgentResult
        Synchronous one-shot — drains the stream and returns the
        final AgentResult. Used by tests and any non-streaming
        consumer (eval / benchmark, server-side scheduled tasks).

Event vocabulary (sent to the frontend):

    agent.turn_start   { turn, synthesis_only?: bool }
    tool.call_start    { id, tool, params }
    tool.call_end      { id, tool, latency_ms, result_summary }
    agent.turn_end     { turn, tools_called, decision }
        decision ∈ {"tools", "direct_answer", "synthesis"}
    answer             { text }
        Final natural-language answer from the LLM. v1 sends this
        as one block — token-streaming (answer.delta) lands in a
        later commit when we wire litellm's stream=True.
    done               { stop_reason, citations, iterations,
                         tool_calls_count, total_latency_ms,
                         tokens_in, tokens_out }
        Always the last event. ``stop_reason`` flags how the loop
        terminated (done / max_iterations / max_tool_calls /
        max_wall_time / error).

Tools execute in parallel within a single turn (``parallel_workers``
threads). ``tool.call_end`` events are emitted as each future
completes via ``as_completed`` — so a fast BM25 result lands in
the UI before the slower vector hit, matching real perceived
latency.

Three hard budgets bound the loop:
    * ``max_iterations``  — number of LLM turns
    * ``max_tool_calls``  — total tool calls across all turns
    * ``max_wall_time_s`` — wall clock
When any is hit, force a synthesis turn (``tool_choice="none"``)
so the LLM produces a final answer from whatever it has, then
emit ``done`` with the appropriate stop_reason.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    # Tools are ~all I/O bound (DB / vector / web), so the ceiling
    # is set by sane DB connection use, not CPU.
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
    state lives in ToolContext). ``stream`` and ``run`` are safe to
    call concurrently from different threads provided each call
    passes its own ToolContext.
    """

    def __init__(self, cfg: AgentConfig, llm: LLMClient):
        self.cfg = cfg
        self.llm = llm

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stream(
        self,
        user_message: str,
        ctx: ToolContext,
        *,
        history: list[dict] | None = None,
    ) -> Iterator[dict]:
        """Generator yielding loop events. Final yield has
        ``type: "done"`` and carries the summary.

        See module docstring for the event vocabulary.
        """
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        tools = [spec.to_openai_tool() for spec in TOOL_REGISTRY.values()]

        iterations = 0
        tool_calls_count = 0
        tokens_in = 0
        tokens_out = 0
        answer_text = ""
        stop_reason = "done"
        t0 = time.time()

        while iterations < self.cfg.max_iterations:
            elapsed = time.time() - t0

            # Pre-flight budget check. Hit → synthesis turn.
            budget_hit = self._check_budget(tool_calls_count, elapsed)
            if budget_hit is not None:
                yield {
                    "type": "agent.turn_start",
                    "turn": iterations + 1,
                    "synthesis_only": True,
                }
                synth = self._synthesise(messages)
                tokens_in += synth.tokens_in
                tokens_out += synth.tokens_out
                answer_text = synth.text or ""
                yield {
                    "type": "agent.turn_end",
                    "turn": iterations + 1,
                    "tools_called": 0,
                    "decision": "synthesis",
                }
                if answer_text:
                    yield {"type": "answer", "text": answer_text}
                stop_reason = budget_hit
                break

            # Normal LLM turn.
            yield {"type": "agent.turn_start", "turn": iterations + 1}
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
                yield {
                    "type": "agent.turn_end",
                    "turn": iterations + 1,
                    "tools_called": 0,
                    "decision": "error",
                }
                stop_reason = "error"
                answer_text = ""
                break

            iterations += 1
            tokens_in += resp.tokens_in
            tokens_out += resp.tokens_out

            # Implicit done: LLM emitted text only.
            if not resp.tool_calls:
                answer_text = resp.text or ""
                yield {
                    "type": "agent.turn_end",
                    "turn": iterations,
                    "tools_called": 0,
                    "decision": "direct_answer",
                }
                if answer_text:
                    yield {"type": "answer", "text": answer_text}
                stop_reason = "done"
                break

            # Cap how many tool_use blocks we execute this turn so
            # the LLM can't bypass the budget by emitting 100 in a
            # single message. Excess get silently dropped.
            requested = resp.tool_calls
            remaining = self.cfg.max_tool_calls - tool_calls_count
            if len(requested) > remaining:
                requested = requested[:remaining]

            # Emit tool.call_start for every requested tool BEFORE
            # dispatching — gives the UI an immediate "X tools queued"
            # visual cue even before any tool finishes.
            for tc in requested:
                yield {
                    "type": "tool.call_start",
                    "id": tc.id,
                    "tool": tc.name,
                    "params": tc.arguments,
                }

            # Run tools in parallel, yield tool.call_end events as
            # each future completes (NOT in submission order — fast
            # BM25 lands in UI before slow vector).
            results: dict[str, dict] = {}
            for evt, tc, result in self._execute_streaming(requested, ctx):
                results[tc.id] = result
                yield evt

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

            yield {
                "type": "agent.turn_end",
                "turn": iterations,
                "tools_called": len(requested),
                "decision": "tools",
            }

        # If we hit the loop without break (max_iterations cap), do
        # a final synthesis turn — same as the budget-hit branch but
        # reached by exhausting the iteration counter.
        else:
            yield {
                "type": "agent.turn_start",
                "turn": iterations + 1,
                "synthesis_only": True,
            }
            synth = self._synthesise(messages)
            tokens_in += synth.tokens_in
            tokens_out += synth.tokens_out
            answer_text = synth.text or ""
            yield {
                "type": "agent.turn_end",
                "turn": iterations + 1,
                "tools_called": 0,
                "decision": "synthesis",
            }
            if answer_text:
                yield {"type": "answer", "text": answer_text}
            stop_reason = "max_iterations"

        # Always emit a single ``done`` event last with the summary.
        yield {
            "type": "done",
            "stop_reason": stop_reason,
            "answer": answer_text,
            "citations": _collect_citations(ctx),
            "iterations": iterations,
            "tool_calls_count": tool_calls_count,
            "total_latency_ms": int((time.time() - t0) * 1000),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }

    def run(
        self,
        user_message: str,
        ctx: ToolContext,
        *,
        history: list[dict] | None = None,
    ) -> AgentResult:
        """Synchronous one-shot. Drains ``stream`` and returns the
        AgentResult assembled from the final ``done`` event.

        Used by tests, eval, benchmark — anything that doesn't need
        live event feedback.
        """
        result: AgentResult = AgentResult(answer="", stop_reason="error")
        for evt in self.stream(user_message, ctx, history=history):
            if evt.get("type") == "done":
                result = AgentResult(
                    answer=evt.get("answer") or "",
                    citations=evt.get("citations") or [],
                    tool_calls_log=ctx.tool_calls_log,
                    stop_reason=evt.get("stop_reason") or "done",
                    iterations=evt.get("iterations") or 0,
                    tool_calls_count=evt.get("tool_calls_count") or 0,
                    total_latency_ms=evt.get("total_latency_ms") or 0,
                    tokens_in=evt.get("tokens_in") or 0,
                    tokens_out=evt.get("tokens_out") or 0,
                )
        return result

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

    def _execute_streaming(
        self, requested: list[ToolCall], ctx: ToolContext
    ) -> Iterator[tuple[dict, ToolCall, dict]]:
        """Run tool calls concurrently; yield (event, ToolCall, result)
        triples in completion order.

        The event is the ``tool.call_end`` dict for the tool whose
        future just resolved. Caller yields the event upstream and
        consumes the result for the next turn's tool_result message.
        """
        if not requested:
            return
        workers = max(1, min(len(requested), self.cfg.parallel_workers))

        def _wrapped(tc: ToolCall) -> tuple[ToolCall, dict, int]:
            t0 = time.time()
            try:
                result = dispatch(tc.name, tc.arguments, ctx)
            except Exception as e:
                # Should be unreachable — dispatch catches inside —
                # but belt-and-suspenders.
                log.exception("dispatch raised in agent loop")
                result = {
                    "error": f"dispatch failed: {type(e).__name__}",
                    "tool": tc.name,
                }
            return tc, result, int((time.time() - t0) * 1000)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_wrapped, tc) for tc in requested]
            for future in as_completed(futures):
                try:
                    tc, result, latency_ms = future.result()
                except Exception:
                    log.exception("agent loop tool future raised")
                    # We don't have the original tc here without the
                    # future-to-tc map; this branch is defensive only.
                    continue
                event = {
                    "type": "tool.call_end",
                    "id": tc.id,
                    "tool": tc.name,
                    "latency_ms": latency_ms,
                    "result_summary": _summarise_result(result),
                }
                yield event, tc, result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarise_result(result: dict) -> dict:
    """Compact summary of a tool result for the ``tool.call_end`` event.

    Mirrors the ``_summarise_result`` in ``dispatch.py`` (kept
    duplicated to avoid pulling private symbols across modules);
    full tool result is what the LLM sees, this summary is for the
    UI / telemetry only.
    """
    if not isinstance(result, dict):
        return {}
    if "error" in result:
        return {"error": result["error"]}
    if "hits" in result and isinstance(result["hits"], list):
        return {"hit_count": len(result["hits"])}
    if "entities" in result and isinstance(result["entities"], list):
        return {
            "entity_count": len(result["entities"]),
            "relation_count": len(result.get("relations") or []),
        }
    if "chunks" in result and isinstance(result["chunks"], list):
        return {"chunk_count": len(result["chunks"])}
    if "chunk_id" in result:
        return {"chunk_id": result["chunk_id"]}
    if "node_id" in result:
        return {"node_id": result["node_id"]}
    return {}


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
