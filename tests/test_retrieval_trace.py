"""Tests for retrieval.trace.RetrievalTrace.

Focused on the two known arithmetic pitfalls:

  A. A phase that just *records* work done upstream reports ~0ms
     duration even though the LLM call took real time. (Fixed by
     post-setting ``duration_ms`` after ``end_phase``.)

  B. ``total_llm_ms`` previously used each phase's wall-clock duration
     whenever it had at least one LLM call, which massively over-counted
     phases that were mostly non-LLM (e.g. KG graph traversal with a
     small entity-extraction LLM call at the start).
"""

from __future__ import annotations

from retrieval.trace import RetrievalTrace


class TestPhaseDurationOverride:
    """Reproduces Bug A: ~0ms phase with upstream LLM latency."""

    def test_override_duration_after_end_phase(self):
        """Post-setting duration_ms on the last phase should stick."""
        trace = RetrievalTrace("q")
        trace.begin_phase("query_understanding")
        trace.record_llm_call(model="m", purpose="qu", latency_ms=1774)
        trace.end_phase()
        # Simulate the precomputed_plan branch override.
        trace.phases[-1]["duration_ms"] = 1774

        d = trace.to_dict()
        qu = next(p for p in d["phases"] if p["name"] == "query_understanding")
        assert qu["duration_ms"] == 1774


class TestTotalLlmMs:
    """Regression coverage for Bug B."""

    def test_llm_time_uses_llm_latency_not_phase_wallclock(self):
        """A phase dominated by non-LLM IO must not inflate total_llm_ms.

        Mirrors a real KG path trace: 10.6s wall-clock, but only 1.2s of
        that is actual LLM work.
        """
        trace = RetrievalTrace("q")
        trace.begin_phase("kg_path")
        trace.record_llm_call(model="m", purpose="kg_entity_extraction", latency_ms=1227)
        trace.end_phase()
        # Post-set timings like the parallel pipeline does.
        trace.phases[-1]["duration_ms"] = 10668
        trace.phases[-1]["started_at_ms"] = 0

        d = trace.to_dict()
        # Must NOT be 10668 (the old buggy behavior).
        assert d["total_llm_ms"] == 1227

    def test_multiple_parallel_llm_calls_capped_by_phase(self):
        """Phase with N parallel LLM calls: time is bounded by phase dur.

        Mirrors tree_path firing 3 concurrent tree_nav LLM calls; the
        per-call latencies sum to more than the phase took, but the
        reported LLM time must not exceed the phase's wall clock.
        """
        trace = RetrievalTrace("q")
        trace.begin_phase("tree_path")
        trace.record_llm_call(model="m", purpose="tree_nav:a", latency_ms=11815)
        trace.record_llm_call(model="m", purpose="tree_nav:b", latency_ms=18259)
        trace.record_llm_call(model="m", purpose="tree_nav:c", latency_ms=49413)
        trace.end_phase()
        trace.phases[-1]["duration_ms"] = 49465
        trace.phases[-1]["started_at_ms"] = 0

        d = trace.to_dict()
        # Sum is 79487, but capped by phase duration 49465.
        assert d["total_llm_ms"] == 49465

    def test_overlapping_parallel_phases_deduped(self):
        """Two phases running in parallel must not double-count LLM time.

        Pipeline launches KG (LLM at t=0) and another phase (LLM inside
        an overlapping window). The union-of-intervals merge must
        collapse them rather than summing.
        """
        trace = RetrievalTrace("q")

        trace.begin_phase("kg_path")
        trace.record_llm_call(model="m", purpose="kg", latency_ms=1000)
        trace.end_phase()
        trace.phases[-1]["duration_ms"] = 1000
        trace.phases[-1]["started_at_ms"] = 0

        trace.begin_phase("other_path")
        trace.record_llm_call(model="m", purpose="other", latency_ms=800)
        trace.end_phase()
        trace.phases[-1]["duration_ms"] = 800
        trace.phases[-1]["started_at_ms"] = 200  # overlaps kg_path's [0, 1000]

        d = trace.to_dict()
        # Union of [0,1000] and [200,1000] = [0,1000], so 1000ms.
        # (Not 1800 = naive sum, and not 2000 = old buggy e=start+dur math.)
        assert d["total_llm_ms"] == 1000

    def test_phase_with_no_llm_contributes_zero(self):
        """A phase without LLM calls should contribute 0 to total_llm_ms."""
        trace = RetrievalTrace("q")
        trace.begin_phase("rrf_merge")
        trace.end_phase()
        trace.phases[-1]["duration_ms"] = 5000
        trace.phases[-1]["started_at_ms"] = 0

        assert trace.to_dict()["total_llm_ms"] == 0

    def test_zero_latency_llm_call_ignored(self):
        """LLM calls with latency=0 (e.g. cached) must not create empty intervals."""
        trace = RetrievalTrace("q")
        trace.begin_phase("kg_path")
        trace.record_llm_call(model="m", purpose="cached", latency_ms=0)
        trace.end_phase()
        trace.phases[-1]["duration_ms"] = 500
        trace.phases[-1]["started_at_ms"] = 0

        assert trace.to_dict()["total_llm_ms"] == 0


class TestRecordLlmCallExtraKwargs:
    """record_llm_call must accept arbitrary extra kwargs for diagnostics.

    Before the **extra passthrough, pipeline.py's `trace.record_llm_call(**lc)`
    would raise TypeError whenever a worker (e.g. tree_path) attached new
    diagnostic fields to its llm_call dict. This test locks down the
    passthrough contract so future diagnostic additions don't break it.
    """

    def test_extra_kwargs_passthrough(self):
        trace = RetrievalTrace("q")
        trace.begin_phase("tree_path")
        trace.record_llm_call(
            model="m",
            purpose="tree_nav:abc",
            latency_ms=1234,
            outline_chars=5000,
            tree_node_count=42,
            returned_nodes=5,
        )
        trace.end_phase()

        lc = trace.phases[-1]["llm_calls"][0]
        assert lc["outline_chars"] == 5000
        assert lc["tree_node_count"] == 42
        assert lc["returned_nodes"] == 5

    def test_none_extras_dropped(self):
        """None-valued extras should NOT clutter the record."""
        trace = RetrievalTrace("q")
        trace.begin_phase("tree_path")
        trace.record_llm_call(
            model="m",
            purpose="tree_nav:abc",
            latency_ms=100,
            outline_chars=None,
            response_chars=200,
        )
        trace.end_phase()

        lc = trace.phases[-1]["llm_calls"][0]
        assert "outline_chars" not in lc
        assert lc["response_chars"] == 200

    def test_pipeline_style_splat_does_not_raise(self):
        """The pipeline code path does `trace.record_llm_call(**dict)` — regression for
        the TypeError that would occur if extra fields weren't accepted."""
        trace = RetrievalTrace("q")
        trace.begin_phase("tree_path")
        lc_dict = {
            "model": "m",
            "purpose": "tree_nav:x",
            "latency_ms": 500,
            "output_preview": "['n_1']",
            "tree_node_count": 10,
            "outline_chars": 800,
            "prompt_chars": 1200,
            "response_chars": 250,
        }
        trace.record_llm_call(**lc_dict)  # must not raise
        trace.end_phase()

        recorded = trace.phases[-1]["llm_calls"][0]
        assert recorded["tree_node_count"] == 10
        assert recorded["outline_chars"] == 800
