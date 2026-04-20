"""
Merge step: combine vector + tree candidates, expand context.

Order of operations:

    1. RRF fusion of all path lists into a deduped dict
    2. Descendant expansion (PageIndex-style: heading hit → section body)
    3. Sibling expansion    (add co-leaf chunks at discounted score)
    4. Cross-ref expansion  (follow chunk.cross_ref_chunk_ids)
    5. Global budget cap

All expansion rules are non-destructive: original merged entries
are never deleted. Rehydration (loading Chunk objects from the
relational store) happens here so that sibling/cross-ref/descendant
rules can inspect actual chunk fields.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable

from config import MergeConfig
from parser.schema import Chunk
from persistence.serde import row_to_chunk
from persistence.store import Store as RelationalStore

from .types import MergedChunk, ScoredChunk

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RRF
# ---------------------------------------------------------------------------


def rrf_merge(
    candidate_lists: Iterable[list[ScoredChunk]],
    *,
    k: int = 60,
) -> dict[str, MergedChunk]:
    """
    Reciprocal Rank Fusion.

    Each input list is assumed to be sorted from best to worst. A
    chunk appearing at rank r (0-indexed) in a list contributes
    1 / (k + r + 1) to its merged score, summed across all lists.
    """
    merged: dict[str, MergedChunk] = {}
    for candidates in candidate_lists:
        for rank, c in enumerate(candidates):
            m = merged.get(c.chunk_id)
            if m is None:
                m = MergedChunk(chunk_id=c.chunk_id, rrf_score=0.0)
                merged[c.chunk_id] = m
            m.rrf_score += 1.0 / (k + rank + 1)
            m.sources.add(c.source)
            m.original_scores[c.source] = c.score
    return merged


# ---------------------------------------------------------------------------
# Rehydration
# ---------------------------------------------------------------------------


def rehydrate(
    merged: dict[str, MergedChunk],
    relational: RelationalStore,
) -> None:
    """Load Chunk objects for every merged entry (in place).

    Chunks that no longer exist in the relational store (deleted or
    re-ingested between retrieval and rehydration) are removed from
    ``merged`` so downstream expansion / citation logic never sees a
    ``None`` chunk.
    """
    missing = [cid for cid, m in merged.items() if m.chunk is None]
    if not missing:
        return
    rows = relational.get_chunks_by_ids(missing)
    for row in rows:
        c = row_to_chunk(row)
        if c.chunk_id in merged:
            merged[c.chunk_id].chunk = c
    # Drop entries that still have no chunk (DB didn't have them).
    still_null = [cid for cid in missing if cid in merged and merged[cid].chunk is None]
    if still_null:
        log.warning("rehydrate: %d chunk(s) missing from store, dropping: %s", len(still_null), still_null[:10])
        for cid in still_null:
            del merged[cid]


# ---------------------------------------------------------------------------
# Descendant expansion (PageIndex-style)
# ---------------------------------------------------------------------------


def expand_descendants(
    merged: dict[str, MergedChunk],
    relational: RelationalStore,
    cfg: MergeConfig,
) -> None:
    """
    PageIndex-style downward expansion.

    When a matched chunk is "thin" (below descendant_min_token_threshold,
    typically a heading or title) AND its tree node has children, pull
    content chunks from descendant nodes. This turns a "title match"
    into a "section content match" — the LLM gets the body text that
    the heading describes, not just the heading itself.

    Requires loading the doc tree from the relational store. Trees are
    cached per doc_id within this call.
    """
    if not cfg.descendant_expansion_enabled or not merged:
        return

    rehydrate(merged, relational)

    # Identify candidates: thin chunks that might be headings
    candidates: list[tuple[str, MergedChunk]] = []
    for cid, m in merged.items():
        if m.chunk is None:
            continue
        if m.chunk.token_count >= cfg.descendant_min_token_threshold:
            continue  # not thin enough to be a heading
        candidates.append((cid, m))

    if not candidates:
        return

    # Load trees (cached by doc_id)
    tree_cache: dict[str, dict] = {}  # doc_id -> tree_json dict
    for _, m in candidates:
        c = m.chunk
        if c.doc_id not in tree_cache:
            doc_row = relational.get_document(c.doc_id)
            if doc_row:
                tree_json = relational.load_tree(c.doc_id, doc_row["active_parse_version"])
                tree_cache[c.doc_id] = tree_json or {}

    additions: dict[str, MergedChunk] = {}
    global_added = 0
    GLOBAL_MAX = 50

    for original_id, m in candidates:
        if global_added >= GLOBAL_MAX:
            break

        c = m.chunk
        tree_json = tree_cache.get(c.doc_id, {})
        if not tree_json:
            continue

        nodes = tree_json.get("nodes", {})
        node = nodes.get(c.node_id)
        if not node:
            continue

        # Collect descendant node_ids (BFS)
        children = node.get("children", [])
        if not children:
            continue  # leaf node, no descendants to expand

        descendant_nids: list[str] = []
        queue = deque(children)
        while queue:
            nid = queue.popleft()
            descendant_nids.append(nid)
            child_node = nodes.get(nid, {})
            queue.extend(child_node.get("children", []))

        if not descendant_nids:
            continue

        # Fetch chunks for descendant nodes
        desc_rows = relational.get_chunks_by_node_ids(descendant_nids)
        added = 0
        for row in desc_rows:
            desc_cid = row["chunk_id"]
            if desc_cid in merged or desc_cid in additions:
                continue
            additions[desc_cid] = MergedChunk(
                chunk_id=desc_cid,
                rrf_score=m.rrf_score * cfg.descendant_score_discount,
                sources={"expansion:descendant"},
                parent_of=original_id,
                chunk=row_to_chunk(row),
            )
            added += 1
            global_added += 1
            if added >= cfg.descendant_max_chunks:
                break
            if global_added >= GLOBAL_MAX:
                break

    if additions:
        merged.update(additions)
        log.debug("descendant expansion added %d chunks", len(additions))


# ---------------------------------------------------------------------------
# Sibling expansion
# ---------------------------------------------------------------------------


def expand_siblings(
    merged: dict[str, MergedChunk],
    relational: RelationalStore,
    cfg: MergeConfig,
) -> None:
    if not cfg.sibling_expansion_enabled or not merged:
        return

    # Make sure merged entries have Chunk objects so we can see node_id
    rehydrate(merged, relational)

    # Collect node_ids whose siblings we might want
    node_ids = {m.chunk.node_id for m in merged.values() if m.chunk is not None}
    if not node_ids:
        return

    # Load ALL chunks for those nodes in one query
    sibling_rows = relational.get_chunks_by_node_ids(list(node_ids))
    by_node: dict[str, list[Chunk]] = {}
    for row in sibling_rows:
        c = row_to_chunk(row)
        by_node.setdefault(c.node_id, []).append(c)

    # Pre-sort each node's chunks by chunk_id for determinism
    for chunks in by_node.values():
        chunks.sort(key=lambda x: x.chunk_id)

    additions: dict[str, MergedChunk] = {}
    for original_id, m in list(merged.items()):
        if m.chunk is None:
            continue
        siblings = by_node.get(m.chunk.node_id, [])
        if len(siblings) > cfg.sibling_max_node_size:
            continue  # node too large; don't explode
        added = 0
        for sib in siblings:
            if added >= cfg.sibling_max_per_hit:
                break
            if sib.chunk_id == original_id:
                continue
            if sib.chunk_id in merged or sib.chunk_id in additions:
                continue
            additions[sib.chunk_id] = MergedChunk(
                chunk_id=sib.chunk_id,
                rrf_score=m.rrf_score * cfg.sibling_score_discount,
                sources={"expansion:sibling"},
                parent_of=original_id,
                chunk=sib,
            )
            added += 1
    if additions:
        merged.update(additions)
        log.debug("sibling expansion added %d chunks", len(additions))


# ---------------------------------------------------------------------------
# Cross-reference expansion
# ---------------------------------------------------------------------------


def expand_crossrefs(
    merged: dict[str, MergedChunk],
    relational: RelationalStore,
    cfg: MergeConfig,
) -> None:
    if not cfg.crossref_expansion_enabled or not merged:
        return

    rehydrate(merged, relational)

    # Gather all referenced chunk_ids from current candidates
    wanted: dict[str, str] = {}  # ref_chunk_id -> parent_chunk_id
    for cid, m in merged.items():
        if m.chunk is None:
            continue
        for i, ref in enumerate(m.chunk.cross_ref_chunk_ids):
            if i >= cfg.crossref_max_per_hit:
                break
            if ref not in merged and ref not in wanted:
                wanted[ref] = cid
    if not wanted:
        return

    rows = relational.get_chunks_by_ids(list(wanted.keys()))
    by_id: dict[str, Chunk] = {r["chunk_id"]: row_to_chunk(r) for r in rows}

    for ref_id, parent_id in wanted.items():
        target_chunk = by_id.get(ref_id)
        if target_chunk is None:
            continue
        parent = merged[parent_id]
        merged[ref_id] = MergedChunk(
            chunk_id=ref_id,
            rrf_score=parent.rrf_score * cfg.crossref_score_discount,
            sources={"expansion:crossref"},
            parent_of=parent_id,
            chunk=target_chunk,
        )
    log.debug("crossref expansion added %d chunks", len(wanted))


# ---------------------------------------------------------------------------
# Final sort + cap
# ---------------------------------------------------------------------------


def finalize_merged(
    merged: dict[str, MergedChunk],
    *,
    base_top_k: int,
    cfg: MergeConfig,
) -> list[MergedChunk]:
    """Sort by (rrf_score desc, source_count desc) and apply budget."""
    items = sorted(
        merged.values(),
        key=lambda m: (-m.rrf_score, -len(m.sources)),
    )
    cap = min(
        cfg.candidate_limit,
        int(base_top_k * cfg.global_budget_multiplier),
    )
    return items[:cap]
