"""
ContextExpander — descendant / sibling / crossref expansion after RRF.

For each hit, optionally pulls in:
  * descendant chunks (children of the same tree node), score-discounted
  * sibling chunks (same leaf, different chunks), score-discounted
  * crossref chunks (explicit ``cross_ref_targets`` metadata), score-discounted

All three toggles can be independently disabled per-request.

Also owns rehydration (filling chunk ``content`` from the relational
store) and the final budget cap via ``finalize_merged``.
"""

from __future__ import annotations

from ..telemetry import get_tracer
from ..types import ScoredChunk

_tracer = get_tracer()


class ContextExpander:
    """
    Args:
        cfg: ``RetrievalMergeConfig`` — same object as used by RRFFusion's
             ``k``, but here we read the expansion flags + budget. Per-request
             overrides are passed via ``run()`` kwargs so the shared cfg
             stays pristine.
        rel: relational store, used by the expanders to fetch node metadata.
    """

    def __init__(self, cfg, *, rel):
        self.cfg = cfg
        self.rel = rel

    def run(
        self,
        merged: list[ScoredChunk],
        *,
        base_top_k: int,
        descendant: bool | None = None,
        sibling: bool | None = None,
        crossref: bool | None = None,
        candidate_limit: int | None = None,
    ) -> list[ScoredChunk]:
        # Effective merge cfg: shallow-copy so we can tweak expansion flags
        # without touching the shared self.cfg.
        updates: dict = {}
        if descendant is not None:
            updates["descendant_expansion_enabled"] = descendant
        if sibling is not None:
            updates["sibling_expansion_enabled"] = sibling
        if crossref is not None:
            updates["crossref_expansion_enabled"] = crossref
        if candidate_limit is not None:
            updates["candidate_limit"] = candidate_limit
        merge_cfg = self.cfg.model_copy(update=updates) if updates else self.cfg

        with _tracer.start_as_current_span("forgerag.expansion") as span:
            from ..merge import (
                expand_crossrefs,
                expand_descendants,
                expand_siblings,
                finalize_merged,
                rehydrate,
            )

            span.set_attribute("forgerag.descendant_expansion", merge_cfg.descendant_expansion_enabled)
            span.set_attribute("forgerag.sibling_expansion", merge_cfg.sibling_expansion_enabled)
            span.set_attribute("forgerag.crossref_expansion", merge_cfg.crossref_expansion_enabled)
            span.set_attribute("forgerag.candidate_limit", merge_cfg.candidate_limit)

            pre_expand = len(merged)
            expand_descendants(merged, self.rel, merge_cfg)
            post_desc = len(merged)
            expand_siblings(merged, self.rel, merge_cfg)
            post_sib = len(merged)
            expand_crossrefs(merged, self.rel, merge_cfg)
            post_xref = len(merged)
            rehydrate(merged, self.rel)

            finalized = finalize_merged(merged, base_top_k=base_top_k, cfg=merge_cfg)

            span.set_attribute("forgerag.added_by_descendant", post_desc - pre_expand)
            span.set_attribute("forgerag.added_by_sibling", post_sib - post_desc)
            span.set_attribute("forgerag.added_by_crossref", post_xref - post_sib)
            span.set_attribute("forgerag.final_count", len(finalized))
            return finalized
