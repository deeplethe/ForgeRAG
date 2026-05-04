"""
Async-ish node summary generator.

After tree_builder produces a DocTree, this module walks each node,
concatenates its block texts, and asks an LLM to generate a 1-2
sentence summary. The summary is stored on TreeNode.summary and
persisted to the tree_json JSONB.

Why summaries matter for retrieval:
    The LLM tree navigator sees the tree outline (titles + summaries)
    WITHOUT full text. A bare title like "3.2 Results" is ambiguous;
    a summary like "Comparison of Green function accuracy across 3
    mesh resolutions" lets the LLM reason much more precisely.

This pass is OPTIONAL and should run after ingestion (it's slow:
one LLM call per node). Use it as a post-processing step or as
part of the ingestion pipeline when --enrich is set.

Usage:
    from parser.summary import enrich_tree_summaries
    enrich_tree_summaries(tree, doc, cfg, generator_fn)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from parser.schema import DocTree, ParsedDocument, TreeNode

log = logging.getLogger(__name__)


def enrich_tree_summaries(
    tree: DocTree,
    doc: ParsedDocument,
    *,
    generate_fn: Callable[[str], str],
    max_text_chars: int = 4000,
    skip_if_exists: bool = True,
    max_failures: int = 2,
    max_workers: int = 4,
) -> tuple[int, int]:
    """
    Walk every node in the tree and generate a summary via
    `generate_fn(text) -> summary_string` **in parallel**.

    Returns (summarized_count, failure_count).
    """
    blocks_index = doc.blocks_by_id()

    # ── Collect tasks ──
    tasks: list[tuple[TreeNode, str]] = []
    for node in tree.walk_preorder():
        if skip_if_exists and node.summary:
            continue
        if not node.block_ids:
            text = _collect_descendant_text(tree, node, blocks_index, max_text_chars)
        else:
            text = _collect_node_text(node, blocks_index, max_text_chars)
        if not text.strip():
            continue
        prompt = _build_summary_prompt(node.title, text)
        tasks.append((node, prompt))

    if not tasks:
        return 0, 0

    log.info("summary enrichment: %d nodes to process (workers=%d)", len(tasks), max_workers)

    # ── Execute in parallel ──
    count = 0
    failures = 0

    def _process_one(node: TreeNode, prompt: str) -> tuple[TreeNode, str | None]:
        try:
            summary = generate_fn(prompt).strip()
            return node, summary
        except Exception as e:
            log.warning("summary failed for node %s: %s", node.node_id, e)
            return node, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_process_one, nd, pr): nd for nd, pr in tasks}
        consecutive_failures = 0
        for fut in as_completed(futures):
            node, summary = fut.result()
            if summary:
                node.summary = summary
                count += 1
                consecutive_failures = 0
                log.debug("summarized node %s: %s", node.node_id, summary[:80])
            else:
                failures += 1
                consecutive_failures += 1
                if consecutive_failures >= max_failures:
                    log.warning(
                        "aborting summary enrichment after %d consecutive failures (likely API issue)",
                        max_failures,
                    )
                    for f in futures:
                        f.cancel()
                    break

    log.info("summary enrichment done: %d summarized, %d failed", count, failures)
    return count, failures


def _collect_node_text(node, blocks_index: dict, max_chars: int) -> str:
    parts: list[str] = []
    total = 0
    for bid in node.block_ids:
        b = blocks_index.get(bid)
        if b is None or b.excluded:
            continue
        text = b.text.strip()
        if not text:
            continue
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n".join(parts)[:max_chars]


def _collect_descendant_text(tree: DocTree, node, blocks_index: dict, max_chars: int) -> str:
    parts: list[str] = []
    total = 0
    for desc in tree.walk_preorder(start=node.node_id):
        for bid in desc.block_ids:
            b = blocks_index.get(bid)
            if b is None or b.excluded:
                continue
            text = b.text.strip()
            if text:
                parts.append(text)
                total += len(text)
                if total >= max_chars:
                    return "\n".join(parts)[:max_chars]
    return "\n".join(parts)[:max_chars]


def _build_summary_prompt(title: str, text: str) -> str:
    return (
        f'You are given a section of a document titled "{title}". '
        f"Generate a concise 1-2 sentence description of the main "
        f"points covered in this section. Focus on what specific "
        f"topics, methods, or results are discussed.\n\n"
        f"Section text:\n{text}\n\n"
        f"Description:"
    )


# ---------------------------------------------------------------------------
# Convenience: make a generate_fn from our GeneratorConfig
# ---------------------------------------------------------------------------


def cheap_node_summary(
    node: TreeNode,
    blocks_index: dict,
    max_chars: int = 200,
) -> str:
    """Zero-cost summary: extract first sentence from block text.

    Used as a fallback when LLM summaries are not available.
    """
    texts: list[str] = []
    total = 0
    for bid in node.block_ids:
        b = blocks_index.get(bid)
        if b is None or b.excluded:
            continue
        text = b.text.strip()
        if not text:
            continue
        texts.append(text)
        total += len(text)
        if total >= max_chars * 2:
            break

    if not texts:
        return ""
    full = " ".join(texts)
    # Take first sentence (split on Chinese/English period)
    for sep in ("。", ". ", "\n"):
        idx = full.find(sep)
        if 0 < idx < max_chars:
            return full[: idx + len(sep)].strip()
    return full[:max_chars].strip()


def batch_enrich_tree_summaries(
    tree: DocTree,
    doc: ParsedDocument,
    *,
    generate_fn: Callable[[str], str],
    max_text_chars: int = 4000,
    batch_size: int = 8,
    skip_if_exists: bool = True,
) -> tuple[int, int]:
    """
    Batch-mode summary enrichment: groups multiple nodes per LLM call.

    Bottom-up approach:
        1. Summarize leaf nodes first (using block text)
        2. Summarize parent nodes using child summaries
    Each LLM call handles up to `batch_size` nodes at once.

    Returns (summarized_count, failure_count).
    """
    blocks_index = doc.blocks_by_id()

    # Collect nodes by level (deepest first for bottom-up)
    nodes_by_level: dict[int, list[TreeNode]] = {}
    for node in tree.walk_preorder():
        if skip_if_exists and node.summary:
            continue
        nodes_by_level.setdefault(node.level, []).append(node)

    max_level = max(nodes_by_level.keys()) if nodes_by_level else 0
    count = 0
    failures = 0

    # Process bottom-up: leaves first, then parents
    for level in range(max_level, -1, -1):
        nodes = nodes_by_level.get(level, [])
        if not nodes:
            continue

        # Split into batches
        for i in range(0, len(nodes), batch_size):
            batch = nodes[i : i + batch_size]
            batch_texts: list[tuple[TreeNode, str]] = []

            for node in batch:
                # For leaf nodes or nodes with blocks: use block text
                if node.block_ids:
                    text = _collect_node_text(node, blocks_index, max_text_chars // batch_size)
                # For parent nodes: use child summaries
                elif node.children:
                    child_summaries = []
                    for cid in node.children:
                        child = tree.nodes.get(cid)
                        if child and child.summary:
                            child_summaries.append(f"- {child.title}: {child.summary}")
                    text = "\n".join(child_summaries)
                else:
                    text = _collect_descendant_text(tree, node, blocks_index, max_text_chars // batch_size)

                if text.strip():
                    batch_texts.append((node, text))

            if not batch_texts:
                continue

            # Build batch prompt
            prompt = _build_batch_summary_prompt(batch_texts)
            try:
                raw = generate_fn(prompt)
                summaries = _parse_batch_summary_response(raw, len(batch_texts))
                for j, (node, _) in enumerate(batch_texts):
                    if j < len(summaries) and summaries[j]:
                        node.summary = summaries[j]
                        count += 1
            except Exception as e:
                log.warning("batch summary failed for %d nodes: %s", len(batch_texts), e)
                failures += len(batch_texts)
                # Fallback: use cheap summary for this batch
                for node, _ in batch_texts:
                    if not node.summary:
                        node.summary = cheap_node_summary(node, blocks_index)
                        if node.summary:
                            count += 1

    log.info("batch summary enrichment: %d summarized, %d failed", count, failures)
    return count, failures


def _build_batch_summary_prompt(batch: list[tuple[TreeNode, str]]) -> str:
    """Build a single prompt that asks for summaries of multiple nodes."""
    parts = [
        "Generate a concise 1-2 sentence summary for EACH of the following "
        "document sections. Focus on specific topics, methods, or results.\n\n"
        "Return a JSON array of strings, one summary per section, in order.\n"
    ]
    for i, (node, text) in enumerate(batch):
        parts.append(f'[Section {i}] Title: "{node.title}"\nContent:\n{text}\n')
    parts.append('\nReturn ONLY a JSON array: ["summary_0", "summary_1", ...]')
    return "\n".join(parts)


def _parse_batch_summary_response(raw: str, expected: int) -> list[str]:
    """Parse a JSON array of summary strings from LLM response."""
    import json
    import re

    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE).strip()

    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError("Expected JSON array")
    return [str(s).strip() for s in data]


def make_summary_fn(
    *,
    model: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    api_base: str | None = None,
) -> Callable[[str], str]:
    """
    Build a simple prompt -> response callable backed by litellm.
    """
    from config.auth import resolve_api_key as _resolve

    key = _resolve(api_key=api_key, api_key_env=api_key_env)

    def _generate(prompt: str) -> str:
        from opencraig.llm_cache import cached_completion

        kwargs: dict[str, Any] = dict(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
            timeout=60,
        )
        if key:
            kwargs["api_key"] = key
        if api_base:
            kwargs["api_base"] = api_base
        resp = cached_completion(**kwargs)
        return resp.choices[0].message.content or ""

    return _generate
