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
    agent.thought      { turn, text }
        Natural-language reasoning the LLM emitted alongside tool
        calls this turn. Only fired when content is non-empty AND
        the turn is dispatching tools (otherwise the text becomes
        the answer and rides ``answer`` / ``answer.delta``).
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

from .dispatch import ToolContext, dispatch, enrich_citations, tools_for
from .llm import LLMClient, LLMResponse, ToolCall
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_REGISTRY

log = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Per-instance config for the agent loop.

    Defaults sized for thorough document research:
      * 10 LLM iterations (each can have many tool calls)
      * 24 total tool calls per query — leaves headroom for
        2-3 searches + 6-8 read_chunks + a graph_explore + rerank.
        Earlier 8-call cap forced premature synthesis on questions
        that wanted to read more than a few passages.
      * 60s wall-clock cap (up from 30s) — matches the more
        generous tool budget; the synthesis fallback still fires
        on slow networks.
      * temperature 0 — agentic loops want determinism, not
        creativity
    """

    model: str = "anthropic/claude-sonnet-4-5"
    api_key: str | None = None
    api_base: str | None = None

    max_iterations: int = 10
    max_tool_calls: int = 24
    max_wall_time_s: float = 60.0

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


def _format_user_error(e: BaseException) -> str:
    """Render an exception as a one-line, user-actionable string.

    Strips provider-internal noise (full URLs, litellm wrapper
    paths, double-prefixes like "litellm.AuthenticationError:") so
    what lands in the chat bubble reads as "AuthenticationError:
    The api_key client option must be set..." rather than a
    100-char wall of internal class names.

    The exception type name stays in front of the colon — for
    auth / rate-limit / timeout errors that's the most useful
    signal at a glance.
    """
    msg = str(e) or e.__class__.__name__
    # litellm sometimes prefixes "litellm.<ExceptionName>:" or
    # "<ExceptionName>: <provider>Exception - ..." in the message
    # body. Trim the duplicated class label.
    for prefix in (f"{e.__class__.__name__}:", "litellm."):
        if msg.startswith(prefix):
            msg = msg[len(prefix):].strip()
    # Cap length so a multi-paragraph stack-style detail doesn't
    # dominate the chat. 400 chars covers the actionable line
    # ("missing OPENAI_API_KEY") with room to spare.
    if len(msg) > 400:
        msg = msg[:400] + "…"
    return f"{e.__class__.__name__}: {msg}"


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
        system_prompt: str | None = None,
    ) -> Iterator[dict]:
        """Generator yielding loop events. Final yield has
        ``type: "done"`` and carries the summary.

        ``system_prompt`` overrides the default ``SYSTEM_PROMPT``
        when supplied; Phase 1.6 uses this to inject a project-
        context block when the conversation is bound to a project
        (see ``api/agent/prompts.py::build_system_prompt``). When
        omitted, the agent gets the unmodified base prompt — the
        plain-Q&A path stays bit-identical.

        See module docstring for the event vocabulary.
        """
        messages: list[dict] = [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT}
        ]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # ``tools_for(ctx)`` filters project-aware tools (python_exec /
        # bash_exec / import_from_library) out of the offered list when
        # the conversation isn't bound to a project — cleaner than
        # leaving them visible and erroring on call.
        tools = [spec.to_openai_tool() for spec in tools_for(ctx)]

        iterations = 0
        tool_calls_count = 0
        tokens_in = 0
        tokens_out = 0
        answer_text = ""
        stop_reason = "done"
        # Surfaced to the client on the final ``done`` event when
        # the loop bails out due to LLM / synthesis failure (auth
        # error, rate-limit, network, etc). Empty string means
        # "no error to report" so the JSON shape stays stable.
        error_message = ""
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
                # Stream the synthesis turn — its output IS the
                # final answer, so deltas flow straight to the UI.
                synth = self._stream_synthesis(messages)
                synth_text = ""
                synth_resp: LLMResponse | None = None
                for kind, payload in synth:
                    if kind == "delta":
                        synth_text += payload
                        yield {"type": "answer.delta", "text": payload}
                    elif kind == "done":
                        synth_resp = payload
                if synth_resp is not None:
                    tokens_in += synth_resp.tokens_in
                    tokens_out += synth_resp.tokens_out
                answer_text = (synth_resp.text if synth_resp else None) or synth_text
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

            # Normal LLM turn — STREAMING with DSML-leak protection.
            #
            # Streaming is on by default so direct-answer turns flow
            # token-by-token to the UI. The catch: some providers
            # (notably DeepSeek-V4-Pro under stream=True with tools)
            # emit tool_use as raw ``<|DSML|...>`` markup in the
            # content stream instead of in ``delta.tool_calls``.
            # Naively yielding those deltas to the user shows
            # garbled internal markup; treating the turn as a direct
            # answer skips tool dispatch entirely.
            #
            # Defence: ``_stream_main_turn`` head-buffers the first
            # ~32 chars of content. If a known DSML/tool sentinel
            # appears → swallow the rest of the stream silently and
            # fall back to a non-streaming ``chat()`` call to get a
            # clean ``tool_calls`` list. If the head looks like
            # normal prose → flush it and stream live. Net cost:
            # DSML turns pay one extra (cached-friendly) call;
            # direct-answer turns stream cleanly.
            yield {"type": "agent.turn_start", "turn": iterations + 1}
            resp: LLMResponse | None = None
            stream_text_yielded = ""
            try:
                for kind, payload in self._stream_main_turn(messages, tools):
                    if kind == "delta":
                        stream_text_yielded += payload
                        yield {"type": "answer.delta", "text": payload}
                    elif kind == "done":
                        resp = payload
            except Exception as e:
                log.exception("LLM chat failed in agent loop")
                resp = None
                # Capture the exception so the final ``done`` event
                # can surface it to the user. Format: "ExceptionType:
                # message" — the type name is often the most useful
                # part (e.g. ``AuthenticationError`` flags missing
                # API key) while the message carries the provider's
                # detail. _format_user_error trims provider-internal
                # noise (full URLs / litellm wrapper paths).
                error_message = _format_user_error(e)

            if resp is None or resp.stop_reason == "error":
                yield {
                    "type": "agent.turn_end",
                    "turn": iterations + 1,
                    "tools_called": 0,
                    "decision": "error",
                }
                stop_reason = "error"
                answer_text = ""
                # If the inner stream surfaced a stop_reason="error"
                # but threw no exception (rare — e.g. provider
                # streamed an empty response), fall back to a
                # generic message so the UI still shows something
                # actionable instead of a silent empty bubble.
                if not error_message:
                    error_message = "The model returned no usable response."
                break

            iterations += 1
            tokens_in += resp.tokens_in
            tokens_out += resp.tokens_out

            # Implicit done: LLM emitted text only.
            if not resp.tool_calls:
                answer_text = resp.text or stream_text_yielded
                yield {
                    "type": "agent.turn_end",
                    "turn": iterations,
                    "tools_called": 0,
                    "decision": "direct_answer",
                }
                if answer_text:
                    # Final aggregated answer — non-streaming
                    # consumers (run() / eval / benchmark) read this.
                    # Streaming UI clients already accumulated the
                    # same text from the deltas above; they treat
                    # this as a no-op overwrite.
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

            # If the LLM included a natural-language preface alongside
            # the tool calls (e.g. "Let me search for X first...")
            # AND the streaming layer didn't already deliver it as
            # deltas, surface it as ``agent.thought`` so the
            # frontend can render the reasoning chain
            # chronologically:
            #   thought 1 → tool A → tool B → thought 2 → tool C → answer
            #
            # The ``not stream_text_yielded`` guard avoids duplicate
            # text on clean-streaming providers — there the preface
            # already streamed via ``answer.delta`` and the frontend
            # promotes streamText into a thought entry when
            # ``tool.call_start`` fires. The DSML-fallback path
            # IS the case this event covers: deltas were suppressed,
            # and ``agent.thought`` is the only channel for the
            # model's reasoning text to reach the chain UI.
            if resp.text and resp.text.strip() and not stream_text_yielded:
                yield {
                    "type": "agent.thought",
                    "turn": iterations,
                    "text": resp.text,
                }

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
            synth_text = ""
            synth_resp: LLMResponse | None = None
            for kind, payload in self._stream_synthesis(messages):
                if kind == "delta":
                    synth_text += payload
                    yield {"type": "answer.delta", "text": payload}
                elif kind == "done":
                    synth_resp = payload
            if synth_resp is not None:
                tokens_in += synth_resp.tokens_in
                tokens_out += synth_resp.tokens_out
            answer_text = (synth_resp.text if synth_resp else None) or synth_text
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
        # ``error`` is empty on success; non-empty on
        # stop_reason="error" so the UI can render a red bubble
        # instead of a silent empty assistant message.
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
            "error": error_message,
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
        """Non-streaming forced final turn — used by tests / eval.

        ``tool_choice="none"`` tells the LLM "give me an answer
        based on what you already have, don't ask for more".
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

    # Substrings that mark provider-internal tool-call markup
    # leaking into the content stream. DeepSeek-V4-Pro emits
    # ``<||DSML||tool_calls>``, ``<||DSML||invoke name="…">``,
    # ``<||DSML||parameter name="…">`` etc. as PLAIN TEXT when
    # stream=True is paired with tools — instead of populating
    # ``delta.tool_calls``. The double-pipe variant is observed in
    # practice; older docs reference single-pipe ``<|DSML|`` so we
    # match both.
    #
    # We also catch generic XML-ish tool-call markers (``<|tool_use|>``,
    # ``<|invoke …>``, etc.) so any provider that leaks similar
    # markup falls into the same protection path.
    #
    # ``DSML`` and ``tool_calls`` alone are unlikely to appear in
    # natural answer prose (especially as the very first non-space
    # tokens), so we use them as primary detection — single-pipe vs
    # double-pipe doesn't matter for matching.
    _DSML_SENTINELS: tuple[str, ...] = (
        "DSML",                # any-pipes ``<|DSML``, ``<||DSML``, etc.
        "<|tool_calls",
        "<||tool_calls",
        "<|tool_use",
        "<||tool_use",
        "<|invoke",
        "<||invoke",
        "<|function",
        "<||function",
        "invoke name=",        # bare-tag form some providers leak
        "tool_calls>",
    )
    # Head limit kept tight so a clean direct-answer turn starts
    # streaming after just ~16 chars instead of waiting for 64. The
    # DSML sentinels we look for (``DSML``, ``<|tool``, ``<|invoke``)
    # are all 4-8 chars; 16 chars in the head is enough to catch a
    # turn that opens with markup. Mid-stream lookback (32 chars)
    # picks up sentinels split across deltas. Net result: much
    # snappier perceived latency on slow providers — for a 40-second
    # DeepSeek call, we used to delay the first delta until 64 chars
    # had buffered (often the entire short response).
    _DSML_HEAD_LIMIT: int = 16
    _DSML_LOOKBACK: int = 32

    def _filter_dsml_stream(self, stream):
        """Wrap a ``chat_stream`` generator with DSML-leak detection.

        Yields the same ``(kind, payload)`` tuples as the source
        stream, plus filters out content deltas once a DSML sentinel
        is detected — anywhere in the stream, not just the head.

        Why mid-stream detection: a normal-sounding preface ("Let me
        synthesise the findings…") can flush past the head buffer as
        clean prose, then the model switches into DSML mode for the
        actual tool emission. A pure head-only check yields the
        clean preface AND THEN the DSML markup straight to the user.
        Lookback window catches sentinels that span delta boundaries.

        Adds an extra trailing event:
            ("dsml_detected", bool)
        right before the source's ``done`` event. The caller uses it
        to decide whether to fall back to non-streaming chat().
        """
        head = ""                # content buffered before classification
        head_decided = False     # True once head is clean | dsml
        suppress = False         # True after DSML detected anywhere
        lookback = ""            # last N chars of yielded content for cross-delta detection
        final_resp: LLMResponse | None = None

        for kind, payload in stream:
            if kind == "delta":
                if suppress:
                    # Already in DSML mode — drop everything.
                    continue

                if not head_decided:
                    head += payload
                    if any(s in head for s in self._DSML_SENTINELS):
                        suppress = True
                        head_decided = True
                        continue
                    if len(head) >= self._DSML_HEAD_LIMIT:
                        yield ("delta", head)
                        lookback = head[-self._DSML_LOOKBACK :]
                        head = ""
                        head_decided = True
                    continue

                # Past the head: keep a small lookback so a sentinel
                # split across deltas (e.g. ``<|`` ends one delta,
                # ``DSML…`` starts the next) is still caught.
                window = lookback + payload
                if any(s in window for s in self._DSML_SENTINELS):
                    suppress = True
                    continue

                yield ("delta", payload)
                lookback = (lookback + payload)[-self._DSML_LOOKBACK :]
            elif kind == "done":
                final_resp = payload
            else:
                # Forward unknown event kinds verbatim.
                yield (kind, payload)

        # Flush a short clean response that finished before the head
        # limit — only if no DSML was detected.
        if not head_decided and head and not suppress:
            yield ("delta", head)

        yield ("dsml_detected", suppress)
        if final_resp is not None:
            yield ("done", final_resp)
        else:
            yield ("done", LLMResponse(text="", stop_reason="error"))

    def _stream_synthesis(self, messages: list[dict]):
        """Streaming forced final turn. Yields ``("delta", text)``
        and finally ``("done", LLMResponse)``.

        Used by the agent loop when a budget cap hits — the
        synthesis output IS the final answer, so streaming the
        deltas straight to the UI gives the user real-time
        feedback while the model writes the answer.

        DSML protection applies here too: even with
        ``tool_choice="none"`` and ``tools=None``, DeepSeek-V4-Pro
        sometimes serialises a "would-have-been-a-tool-call" into
        DSML content markup. Without the filter that markup lands
        in the answer body verbatim — see the user screenshot
        showing ``<||DSML||tool_calls>`` etc. as the answer.
        """
        dsml_hit = False
        try:
            raw_stream = self.llm.chat_stream(
                messages,
                tools=None,
                tool_choice="none",
                temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_tokens,
            )
            for kind, payload in self._filter_dsml_stream(raw_stream):
                if kind == "dsml_detected":
                    dsml_hit = bool(payload)
                    continue
                yield (kind, payload)
        except Exception:
            log.exception("LLM synthesis stream failed")
            yield ("done", LLMResponse(text="", stop_reason="error"))
            return

        if dsml_hit:
            # Stream produced DSML garbage — re-issue as
            # non-streaming. ``tool_choice="none"`` blocks tool calls
            # at the API level, which (empirically) suppresses the
            # DSML rendering path on DeepSeek MOST of the time. But
            # not always — DeepSeek-V4-Pro sometimes emits DSML in
            # content even with tool_choice=none. Without a scrub
            # the markup lands verbatim in the user's answer body
            # (the "<||DSML||tool_calls>…" we kept seeing).
            try:
                resp = self.llm.chat(
                    messages,
                    tools=None,
                    tool_choice="none",
                    temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_tokens,
                )
                clean_text = _scrub_dsml(resp.text or "")
                if not clean_text.strip():
                    # Fallback ALSO returned DSML-only — give the
                    # user something readable instead of a blank
                    # message or raw markup.
                    clean_text = (
                        "_(I had trouble composing a final answer "
                        "from the retrieved passages. The model "
                        "kept emitting tool-call markup despite "
                        "the synthesis instruction. Try rephrasing "
                        "the question.)_"
                    )
                if clean_text:
                    yield ("delta", clean_text)
                yield ("done", LLMResponse(
                    text=clean_text,
                    tool_calls=[],
                    stop_reason=resp.stop_reason,
                    tokens_in=resp.tokens_in,
                    tokens_out=resp.tokens_out,
                ))
            except Exception:
                log.exception("synthesis DSML fallback chat() failed")

    def _stream_main_turn(self, messages: list[dict], tools: list[dict]):
        """Stream a normal agent turn (auto tool_choice) with
        DSML-leak protection via ``_filter_dsml_stream``.

        Yields the same shape as ``chat_stream``:
            ("delta", text) — clean content for the UI
            ("done",  LLMResponse) — final assembled response

        Strategy:
          * Run the stream through ``_filter_dsml_stream``. Clean
            deltas flow to the caller; DSML markup is suppressed.
          * If the filter reports ``dsml_detected=True``, fall
            back to a non-streaming ``chat()`` call to get a clean
            ``tool_calls`` list. The user sees no streamed deltas
            for this turn (chain UI shows the phase indicator
            until tools dispatch).

        The fallback ``chat()`` happens silently — no extra UI
        events. From the caller's POV the turn produced one
        LLMResponse, possibly with no streamed deltas (DSML case)
        but with proper tool_calls.
        """
        dsml_hit = False
        final_resp: LLMResponse | None = None
        try:
            raw_stream = self.llm.chat_stream(
                messages,
                tools=tools,
                tool_choice="auto",
                temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_tokens,
            )
            for kind, payload in self._filter_dsml_stream(raw_stream):
                if kind == "dsml_detected":
                    dsml_hit = bool(payload)
                    continue
                if kind == "done":
                    final_resp = payload
                    continue
                yield (kind, payload)
        except Exception:
            log.exception("LLM main-turn stream raised; falling back to non-streaming chat")
            dsml_hit = True
            final_resp = None

        if dsml_hit or final_resp is None or final_resp.stop_reason == "error":
            # DSML detected (or stream failed). Re-issue as a
            # non-streaming call to get a clean ``tool_calls`` list.
            try:
                resp = self.llm.chat(
                    messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_tokens,
                )
                # Scrub DSML out of the fallback's TEXT field as
                # well — if tool_calls came back empty AND the text
                # contains DSML, the loop will treat the text as a
                # direct answer and ship it to the user. We've seen
                # DeepSeek leak DSML into both stream AND fallback
                # paths on the same turn.
                clean_text = _scrub_dsml(resp.text or "")
                resp = LLMResponse(
                    text=clean_text,
                    tool_calls=resp.tool_calls,
                    stop_reason=resp.stop_reason,
                    tokens_in=resp.tokens_in,
                    tokens_out=resp.tokens_out,
                )
                yield ("done", resp)
                return
            except Exception:
                log.exception("DSML fallback chat() also failed")
                yield ("done", LLMResponse(text="", stop_reason="error"))
                return

        yield ("done", final_resp)

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


import re as _re

# Match an envelope-style tool-call tag with extreme tolerance:
#
#   `<` (optionally `/` for closing tag) followed by ANY mix of
#   whitespace + pipe characters + slashes (e.g. `<|`, `<||`,
#   `< |  |`, `<| /`), followed by a keyword (DSML / tool_calls /
#   tool_use / invoke / function / parameter), followed by
#   anything until the next `>`.
#
# We've seen DeepSeek emit at least three formats that all need
# stripping:
#   `<|DSML|tool_calls>`           — single pipes, no spaces
#   `<||DSML||tool_calls>`         — double pipes, no spaces
#   `< |  | DSML |  | tool_calls>` — spaces interleaved with pipes
# The regex covers all three (and any variant) by allowing `[\s|/]*`
# between the opening `<` and the keyword.
_DSML_TAG_RE = _re.compile(
    r"<\s*/?\s*[\s|/]*\s*"
    r"(?:DSML|dsml|tool_calls|tool_use|invoke|function|parameter)"
    r"[^>]*>",
    flags=_re.IGNORECASE,
)


def _scrub_dsml(text: str) -> str:
    """Strip DSML tool-call markup from a synthesis fallback's
    response text.

    DeepSeek-V4-Pro sometimes ignores ``tool_choice="none"`` and
    emits ``<||DSML||tool_calls> <||DSML||invoke name="…">…``
    markup as the synthesis ANSWER, not as native tool_calls.
    Without this scrub, that XML lands in the user's chat as the
    final answer body.

    Defense in depth:
      1. Regex strips known envelope tags (any pipe/space variant)
      2. After stripping, if ``DSML`` still appears anywhere in the
         text, the model is leaking in a format we don't recognise
         → return empty so the caller can substitute the friendly
         fallback message rather than show partial markup.
    """
    if not text:
        return text
    cleaned = _DSML_TAG_RE.sub("", text)
    # Collapse runs of blank lines left behind after stripping.
    cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    # Sanity check — if DSML literal still reachable, the format
    # didn't match the regex; refuse to ship anything rather than
    # half-cleaned XML.
    if "DSML" in cleaned or "dsml" in cleaned:
        return ""
    return cleaned


def _collect_citations(ctx: ToolContext) -> list[dict]:
    """Snapshot the citation pool into a JSON-safe list, sorted
    by score desc (highest signal first).

    Runs ``enrich_citations`` first so every entry carries
    ``highlights`` (page_no + bbox per block) + ``file_id`` (PDF
    preview blob) + ``source_file_id`` + ``source_format`` —
    the same shape the legacy ``retrieval/citations.py`` produced
    for the deleted fixed pipeline. Without enrichment the
    frontend gets ``highlights: []`` and clicking a citation
    opens an empty PDF panel (or pdfjs throws InvalidPDF on
    DOCX-sourced docs because file_id pointed to the original
    .docx blob).

    ``sources`` is a set inside the pool (so cross-tool merging
    is O(1)); we serialise to a sorted list so the frontend gets
    deterministic output.
    """
    enrich_citations(ctx)
    out: list[dict] = []
    for rec in ctx.citation_pool.values():
        item = dict(rec)
        if isinstance(item.get("sources"), set):
            item["sources"] = sorted(item["sources"])
        out.append(item)
    out.sort(key=lambda r: -(r.get("score") or 0.0))
    return out
