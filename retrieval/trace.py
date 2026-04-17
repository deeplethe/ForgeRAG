"""
Retrieval trace: structured audit log for every query.

Records every step of the dual-path retrieval pipeline with
timestamps, inputs, outputs, and LLM call details. The trace
is a JSON-serializable dict that can be:

    - Returned in the API response (stats.trace)
    - Written to a file for offline analysis
    - Displayed in a debug UI

Structure mirrors the actual execution flow:

    trace = {
      "query": "...",
      "timestamp": "2026-04-10T12:00:00Z",
      "total_ms": 25000,
      "phases": [
        { "name": "query_expansion", ... },
        { "name": "vector_path", ... },
        { "name": "tree_path", ... },
        { "name": "merge", ... },
        { "name": "expansion", ... },
        { "name": "rerank", ... },
        { "name": "generation", ... },
      ]
    }

Each phase has:
    - name, started_at, duration_ms
    - inputs / outputs (counts, ids)
    - llm_calls[] (model, prompt_tokens, completion_tokens, latency_ms)
    - details{} (phase-specific metadata)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


class RetrievalTrace:
    """
    Accumulates trace events during a single retrieval/answer cycle.
    Thread-unsafe by design — one trace per request, no sharing.
    """

    def __init__(self, query: str):
        self.query = query
        self.started_at = time.time()
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.phases: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Phase lifecycle
    # ------------------------------------------------------------------

    def begin_phase(self, name: str, **details: Any) -> RetrievalTrace:
        phase = {
            "name": name,
            "started_at": time.time(),
            "duration_ms": 0,
            "inputs": {},
            "outputs": {},
            "llm_calls": [],
            "details": dict(details),
        }
        self._current = phase
        self.phases.append(phase)
        return self

    def end_phase(self, **outputs: Any) -> RetrievalTrace:
        if self._current is not None:
            self._current["duration_ms"] = int((time.time() - self._current["started_at"]) * 1000)
            self._current["outputs"].update(outputs)
            # Convert started_at from unix to relative ms for readability
            self._current["started_at_ms"] = int((self._current["started_at"] - self.started_at) * 1000)
            del self._current["started_at"]
            self._current = None
        return self

    # ------------------------------------------------------------------
    # Record details within a phase
    # ------------------------------------------------------------------

    def set_inputs(self, **inputs: Any) -> RetrievalTrace:
        if self._current is not None:
            self._current["inputs"].update(inputs)
        return self

    def set_outputs(self, **outputs: Any) -> RetrievalTrace:
        if self._current is not None:
            self._current["outputs"].update(outputs)
        return self

    def add_detail(self, **kv: Any) -> RetrievalTrace:
        if self._current is not None:
            self._current["details"].update(kv)
        return self

    def record_llm_call(
        self,
        *,
        model: str,
        purpose: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        latency_ms: int = 0,
        input_preview: str | None = None,
        output_preview: str | None = None,
        error: str | None = None,
        **extra: Any,
    ) -> RetrievalTrace:
        """Record a single LLM call into the current phase.

        Extra keyword arguments are passed through verbatim (after
        dropping None values), letting callers attach diagnostic fields
        like ``outline_chars`` or ``response_chars`` without having to
        extend this signature every time.
        """
        call = {
            "model": model,
            "purpose": purpose,
            "latency_ms": latency_ms,
        }
        if prompt_tokens is not None:
            call["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            call["completion_tokens"] = completion_tokens
        if input_preview:
            call["input_preview"] = input_preview[:200]
        if output_preview:
            call["output_preview"] = output_preview[:200]
        if error:
            call["error"] = error
        for k, v in extra.items():
            if v is not None:
                call[k] = v
        if self._current is not None:
            self._current["llm_calls"].append(call)
        return self

    def record_chunks(
        self,
        label: str,
        chunk_ids: list[str],
        source: str = "",
    ) -> RetrievalTrace:
        if self._current is not None:
            self._current["details"][label] = {
                "count": len(chunk_ids),
                "source": source,
                "ids": chunk_ids[:20],  # cap to avoid huge traces
            }
        return self

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        total_ms = int((time.time() - self.started_at) * 1000)

        # LLM call count (simple sum — each call is counted once)
        total_llm_calls = sum(len(p.get("llm_calls", [])) for p in self.phases)

        # LLM time per phase = sum of each llm_call's own latency_ms, capped
        # by the phase's wall-clock duration (the cap accounts for LLM calls
        # running in parallel within a phase — e.g. tree_path fires multiple
        # tree_nav LLM calls concurrently, so their wall time is bounded by
        # the phase, not the sum).
        #
        # Then merge phase intervals across phases to avoid double-counting
        # when the phases themselves run in parallel (bm25 + vector + kg +
        # tree in our pipeline).
        intervals: list[tuple[int, int]] = []
        for p in self.phases:
            llm_sum = sum(lc.get("latency_ms", 0) for lc in p.get("llm_calls", []))
            if llm_sum <= 0:
                continue
            phase_dur = p.get("duration_ms", 0)
            phase_llm = min(llm_sum, phase_dur) if phase_dur else llm_sum
            s = p.get("started_at_ms", 0)
            intervals.append((s, s + phase_llm))
        if intervals:
            intervals.sort()
            merged = [intervals[0]]
            for s, e in intervals[1:]:
                if s <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))
            total_llm = sum(e - s for s, e in merged)
        else:
            total_llm = 0

        return {
            "query": self.query,
            "timestamp": self.timestamp,
            "total_ms": total_ms,
            "total_llm_ms": total_llm,
            "total_llm_calls": total_llm_calls,
            "phases": self.phases,
        }
