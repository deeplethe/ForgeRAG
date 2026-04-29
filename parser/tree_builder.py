"""
Document tree builder.

Takes a ParsedDocument and emits a DocTree using one of three
strategies, decided in this order:

    1. TOC-based     -- doc has an embedded table of contents
    2. Heading-based -- no TOC but heading blocks are strong enough
    3. LLM-based     -- sends a condensed block list to an LLM (via
                        litellm) and asks it to infer hierarchical
                        sections. Falls back to flat on error.

Every strategy produces a *rooted* tree: the root node has level=0,
title = filename stem, and covers every page. Orphan blocks that
sit outside any section get attached to the nearest preceding node
in reading order (or to the root if nothing precedes them).

The builder never generates summaries. Summary population is a
separate, async pass because it may invoke an LLM and should not
block ingestion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from config import TreeBuilderConfig
from config.auth import resolve_api_key

from .schema import (
    Block,
    BlockType,
    DocTree,
    ParsedDocument,
    TocEntry,
    TreeNode,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


class TreeBuilder:
    def __init__(self, cfg: TreeBuilderConfig):
        self.cfg = cfg

    def build(self, doc: ParsedDocument) -> DocTree:
        ctx = _BuildContext(doc, cfg=self.cfg)
        tree = self._build_with_quality_competition(doc, ctx)

        # Post-process: subdivide oversized leaf nodes
        self._subdivide_large_nodes(tree, doc)

        log.info(
            "tree_builder doc=%s method=%s nodes=%d quality=%.3f",
            doc.doc_id,
            tree.generation_method,
            len(tree.nodes),
            tree.quality_score,
        )
        return tree

    # ------------------------------------------------------------------
    def _build_with_quality_competition(self, doc: ParsedDocument, ctx: _BuildContext) -> DocTree:
        """Build the best possible tree for LLM tree navigation.

        Strategy:
            - LLM enabled → always use LLM (page-group strategy).
              TOC/headings are fed to LLM as hints, not used as
              standalone strategies — they're too unreliable on real
              documents (MinerU misclassifies headings, TOC page
              numbers are often wrong).
            - LLM not available → flat fallback. Tree navigation will
              be disabled (tree_navigable=False), but chunking still
              needs a tree structure to organize blocks.
        """
        if self.cfg.llm_enabled:
            # Collect structural hints from TOC/headings to feed the LLM
            hints = self._collect_structural_hints(doc)
            tree = ctx.from_page_groups(structural_hints=hints)
            tree.quality_score = _quality_score(tree, doc, self.cfg)
            return tree

        # No LLM — flat fallback for chunking only
        tree = ctx.flat_fallback()
        tree.quality_score = _quality_score(tree, doc, self.cfg)
        return tree

    # ------------------------------------------------------------------
    def _collect_structural_hints(self, doc: ParsedDocument) -> str:
        """Gather TOC and heading signals as text hints for the LLM."""
        hints_parts: list[str] = []

        # TOC hints (flatten nested entries)
        if doc.toc:
            toc_lines: list[str] = []

            def _walk_toc(entries: list[TocEntry]) -> None:
                for entry in entries:
                    if len(toc_lines) >= 30:
                        return
                    indent = "  " * (entry.level - 1)
                    toc_lines.append(f"{indent}{entry.title} (p{entry.page_no})")
                    if entry.children:
                        _walk_toc(entry.children)

            _walk_toc(doc.toc)
            if toc_lines:
                hints_parts.append(
                    "The document has an embedded table of contents "
                    "(may have inaccurate page numbers):\n" + "\n".join(toc_lines)
                )

        # Heading hints
        headings = [b for b in doc.reading_blocks() if b.type == BlockType.HEADING and b.level]
        if headings:
            h_lines = [f"  p{h.page_no} L{h.level}: {h.text[:80]}" for h in headings[:30]]
            hints_parts.append("Detected heading blocks (may include false positives):\n" + "\n".join(h_lines))

        return "\n\n".join(hints_parts)

    # ------------------------------------------------------------------
    def _subdivide_large_nodes(self, tree: DocTree, doc: ParsedDocument) -> None:
        """Split oversized leaf nodes into smaller sub-nodes by position."""
        from .chunker import approx_tokens

        threshold = self.cfg.max_tokens_per_node
        blocks_index = doc.blocks_by_id()
        leaves = [n for n in tree.leaves() if n.node_id != tree.root_id]

        for leaf in leaves:
            if not leaf.block_ids:
                continue
            total_tokens = sum(
                approx_tokens(blocks_index[bid].text)
                for bid in leaf.block_ids
                if bid in blocks_index and not blocks_index[bid].excluded
            )
            if total_tokens <= threshold:
                continue

            # Determine how many sub-nodes to create (2-4)
            num_splits = min(4, max(2, total_tokens // threshold + 1))
            valid_bids = [bid for bid in leaf.block_ids if bid in blocks_index and not blocks_index[bid].excluded]
            if len(valid_bids) < num_splits:
                continue

            # Split block_ids evenly
            chunk_size = max(1, len(valid_bids) // num_splits)
            splits: list[list[str]] = []
            for i in range(num_splits):
                start = i * chunk_size
                end = len(valid_bids) if i == num_splits - 1 else (i + 1) * chunk_size
                splits.append(valid_bids[start:end])

            # Create sub-nodes, reusing leaf's _seq counter pattern
            leaf.block_ids.clear()
            leaf.children.clear()
            for i, split_bids in enumerate(splits):
                if not split_bids:
                    continue
                first_b = blocks_index.get(split_bids[0])
                last_b = blocks_index.get(split_bids[-1])
                sub_id = f"{leaf.node_id}:sub{i}"
                sub_node = TreeNode(
                    node_id=sub_id,
                    doc_id=leaf.doc_id,
                    parse_version=leaf.parse_version,
                    parent_id=leaf.node_id,
                    level=leaf.level + 1,
                    title=f"{leaf.title} (part {i + 1})",
                    page_start=first_b.page_no if first_b else leaf.page_start,
                    page_end=last_b.page_no if last_b else leaf.page_end,
                    block_ids=split_bids,
                )
                # Generate cheap summary from block text
                texts = []
                for bid in split_bids[:3]:
                    b = blocks_index.get(bid)
                    if b and b.text:
                        texts.append(b.text[:100])
                sub_node.summary = " ".join(texts)[:200] if texts else None
                tree.nodes[sub_id] = sub_node
                leaf.children.append(sub_id)

            log.info(
                "subdivided node %s (%d tokens) into %d sub-nodes",
                leaf.node_id,
                total_tokens,
                len(leaf.children),
            )


# ---------------------------------------------------------------------------
# Build context -- holds all the per-document state for a single build
# ---------------------------------------------------------------------------


@dataclass
class _BuildContext:
    doc: ParsedDocument
    cfg: TreeBuilderConfig | None = None
    _seq: int = 0

    # ------------------------------------------------------------------
    def _new_id(self) -> str:
        self._seq += 1
        return f"{self.doc.doc_id}:{self.doc.parse_version}:n{self._seq}"

    def _new_node(
        self,
        *,
        parent_id: str | None,
        level: int,
        title: str,
        page_start: int,
        page_end: int,
    ) -> TreeNode:
        return TreeNode(
            node_id=self._new_id(),
            doc_id=self.doc.doc_id,
            parse_version=self.doc.parse_version,
            parent_id=parent_id,
            level=level,
            title=title,
            page_start=page_start,
            page_end=page_end,
        )

    def _root(self) -> TreeNode:
        title = Path(self.doc.filename).stem or self.doc.doc_id
        last_page = max((p.page_no for p in self.doc.pages), default=1)
        return self._new_node(
            parent_id=None,
            level=0,
            title=title,
            page_start=1,
            page_end=last_page,
        )

    def _package(
        self,
        *,
        root: TreeNode,
        nodes: dict[str, TreeNode],
        method: str,
    ) -> DocTree:
        blocks_index = self.doc.blocks_by_id()
        for node in nodes.values():
            _finalize_node_enrichment(node, self.doc, blocks_index)
        return DocTree(
            doc_id=self.doc.doc_id,
            parse_version=self.doc.parse_version,
            root_id=root.node_id,
            nodes=nodes,
            quality_score=0.0,  # filled by caller
            generation_method=method,  # type: ignore[arg-type]
        )

    # ==================================================================
    # Strategy 1: TOC-based
    # ==================================================================

    def from_toc(self) -> DocTree:
        assert self.doc.toc is not None

        nodes: dict[str, TreeNode] = {}
        root = self._root()
        nodes[root.node_id] = root

        # Flatten TOC into (entry, parent_node_id, depth) with preorder
        # traversal so we can compute page_end = next sibling's page_start - 1
        flat: list[tuple[TocEntry, str, int]] = []

        def walk(entries: list[TocEntry], parent_id: str, depth: int) -> None:
            for e in entries:
                flat.append((e, parent_id, depth))
                # Placeholder; we set the parent_id to the newly created
                # node later, during the second pass
            # Second pass is not needed because we process in order and
            # create the parent before its children.

        # Walk recursively, creating nodes as we go so children see their
        # real parent id
        def walk_create(entries: list[TocEntry], parent_id: str, depth: int) -> None:
            for e in entries:
                node = self._new_node(
                    parent_id=parent_id,
                    level=depth,
                    title=e.title.strip() or "(untitled)",
                    page_start=max(1, e.page_no),
                    page_end=max(1, e.page_no),  # fixed in pass 2
                )
                nodes[node.node_id] = node
                nodes[parent_id].children.append(node.node_id)
                flat.append((e, node.node_id, depth))
                if e.children:
                    walk_create(e.children, node.node_id, depth + 1)

        walk_create(self.doc.toc, root.node_id, depth=1)

        # Pass 2: compute page_end for each TOC node.
        # A node's page_end = (the page_start of the next preorder node) - 1,
        # or doc last page if it's the last one.
        last_page = root.page_end
        for i in range(len(flat)):
            _, nid, _ = flat[i]
            node = nodes[nid]
            next_start: int | None = None
            for j in range(i + 1, len(flat)):
                _, next_nid, _ = flat[j]
                next_start = nodes[next_nid].page_start
                break
            if next_start is not None:
                node.page_end = max(node.page_start, next_start - 1)
            else:
                node.page_end = last_page

        # Attach blocks: for each reading block, pick the deepest TOC node
        # whose page range contains the block's page, then (tie-break) pick
        # the last one in preorder that starts before/at the block.
        self._attach_blocks_by_page(nodes, root)

        return self._package(root=root, nodes=nodes, method="toc")

    # ==================================================================
    # Strategy 2: Heading-based
    # ==================================================================

    # Patterns that indicate a "heading" is actually noise (URL, DOI, etc.)
    _RE_URL = re.compile(r"https?://", re.IGNORECASE)
    _RE_DOI = re.compile(r"^doi\s*[:.]", re.IGNORECASE)
    _RE_COPYRIGHT = re.compile(
        r"(^©|copyright\b|all rights reserved)",
        re.IGNORECASE,
    )
    _RE_PURE_NUMERIC = re.compile(r"^[\d\s\.\-,;:]+$")
    # Matches strings that consist entirely of non-alphanumeric chars
    # (e.g. "\", "! |", "? v", "—", "---", "* 0", "** *")
    _RE_NO_ALNUM = re.compile(
        r"^[^a-zA-Z\u00C0-\u024F\u0400-\u04FF"
        r"\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff"
        r"\uAC00-\uD7AF]+$"
    )

    @staticmethod
    def _is_junk_heading(text: str) -> bool:
        """Return True if *text* looks like noise, not a real heading.

        Filters out URLs, DOIs, copyright lines, overly long strings,
        purely numeric text, single-character garbage, and strings
        with no real alphanumeric content that MinerU sometimes
        misclassifies as titles.
        """
        t = text.strip()
        if not t:
            return True
        # Very short strings with no CJK characters are noise
        # (e.g. "\", "t", "! |"), but CJK single-char headings
        # like "序", "附" are legitimate.
        _has_cjk = any(
            "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af" for ch in t
        )
        if len(t) <= 2 and not _has_cjk:
            return True
        # No alphanumeric content at all (e.g. "\", "! |", "? v", "---")
        if _BuildContext._RE_NO_ALNUM.match(t):
            return True
        if _BuildContext._RE_URL.search(t):
            return True
        if _BuildContext._RE_DOI.match(t):
            return True
        if _BuildContext._RE_COPYRIGHT.search(t):
            return True
        # Pure numbers / punctuation (e.g. "(972 articles)")
        if _BuildContext._RE_PURE_NUMERIC.match(t):
            return True
        # Real headings are concise; 200+ char blocks are body text
        return len(t) > 200

    def from_headings(self) -> DocTree:
        nodes: dict[str, TreeNode] = {}
        root = self._root()
        nodes[root.node_id] = root

        reading = self.doc.reading_blocks()
        if not reading:
            return self._package(root=root, nodes=nodes, method="headings")

        # Pre-filter: demote junk headings to plain paragraphs so they
        # don't pollute the tree structure.
        junk_count = 0
        for b in reading:
            if b.type == BlockType.HEADING and b.level and self._is_junk_heading(b.text):
                junk_count += 1
        if junk_count:
            log.info("heading filter: demoting %d junk headings", junk_count)

        # Normalize heading levels: map the smallest observed level to 1.
        raw_levels = sorted(
            {b.level for b in reading if b.type == BlockType.HEADING and b.level and not self._is_junk_heading(b.text)}
        )
        level_map = {lv: i + 1 for i, lv in enumerate(raw_levels)}

        # Stack contains (level, node) -- the current chain of ancestors
        stack: list[tuple[int, TreeNode]] = [(0, root)]

        for block in reading:
            is_heading = block.type == BlockType.HEADING and block.level and not self._is_junk_heading(block.text)
            if is_heading:
                depth = level_map.get(block.level, block.level)
                # Pop until stack top has strictly smaller depth
                while stack and stack[-1][0] >= depth:
                    stack.pop()
                parent = stack[-1][1] if stack else root
                node = self._new_node(
                    parent_id=parent.node_id,
                    level=depth,
                    title=block.text.strip() or "(untitled)",
                    page_start=block.page_no,
                    page_end=block.page_no,
                )
                nodes[node.node_id] = node
                parent.children.append(node.node_id)
                node.block_ids.append(block.block_id)
                stack.append((depth, node))
            else:
                # Content block -> attach to stack top
                target = stack[-1][1] if stack else root
                target.block_ids.append(block.block_id)
                target.page_end = max(target.page_end, block.page_no)
                # Also propagate page_end up the ancestor chain
                self._bubble_page_end(nodes, target, block.page_no)

        return self._package(root=root, nodes=nodes, method="headings")

    # ==================================================================
    # Strategy 3: LLM-inferred structure
    # ==================================================================

    def from_llm(self) -> DocTree:
        """
        Send a condensed representation of the document's blocks to an
        LLM and ask it to infer a hierarchical section structure.
        Falls back to flat if the LLM call fails or returns bad JSON.
        """
        cfg = self.cfg
        if cfg is None or not cfg.model:
            log.warning("LLM tree: no model configured; using flat fallback")
            return self.flat_fallback()

        # Build a condensed text representation for the LLM
        reading = self.doc.reading_blocks()
        if not reading:
            return self.flat_fallback()

        prompt_lines = self._build_block_summary(reading)
        last_page = max((p.page_no for p in self.doc.pages), default=1)

        system_msg = (
            "You are a document structure analyst. Given a list of document blocks "
            "(each with an index, page number, type, and a text preview), infer the "
            "hierarchical section structure of the document.\n\n"
            "Return a JSON array of section objects. Each section has:\n"
            '  - "title": string (section title, inferred from headings or content)\n'
            '  - "level": integer (1 = top-level section, 2 = subsection, etc.)\n'
            '  - "page_start": integer (1-based, inclusive)\n'
            '  - "page_end": integer (1-based, inclusive)\n'
            '  - "block_indices": array of integer indices (0-based, referencing the block list)\n\n'
            "Rules:\n"
            "- Every block index must appear in exactly one section.\n"
            "- Sections are ordered by page_start then by appearance.\n"
            "- Use 2-3 levels of hierarchy. Don't create more than 6 levels.\n"
            "- The title should be descriptive; use heading text when available.\n"
            "- Return ONLY valid JSON, no markdown fences, no explanation."
        )

        user_msg = f"Document: {self.doc.filename}\nPages: {last_page}\nBlocks ({len(reading)} total):\n\n" + "\n".join(
            prompt_lines
        )

        try:
            raw = self._call_llm(system_msg, user_msg)
            sections = self._parse_llm_response(raw, reading)
            tree = self._sections_to_tree(sections, reading)
            log.info("LLM tree builder produced %d sections", len(sections))
            return tree
        except Exception as e:
            log.warning("LLM tree builder failed (%s); using flat fallback", e)
            return self.flat_fallback()

    def _build_block_summary(self, blocks: list[Block], max_chars: int = 12000) -> list[str]:
        """
        Build a condensed representation of blocks for the LLM prompt.
        Truncates individual block text to keep total prompt under budget.
        """
        # Estimate per-block budget
        per_block = max(40, max_chars // max(len(blocks), 1))
        lines = []
        for i, b in enumerate(blocks):
            preview = b.text[:per_block].replace("\n", " ").strip()
            if len(b.text) > per_block:
                preview += "..."
            lines.append(
                f"[{i}] p{b.page_no} {b.type.value}"
                + (f"/L{b.level}" if b.type == BlockType.HEADING and b.level else "")
                + f": {preview}"
            )
        return lines

    def _call_llm(self, system_msg: str, user_msg: str) -> str:
        """Call the LLM via litellm (routed through ingest-side cache)."""
        from forgerag.llm_cache import cached_completion

        cfg = self.cfg
        assert cfg is not None

        kwargs: dict = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": 4096,
            "timeout": 120,  # 2 min hard ceiling
        }
        api_key = resolve_api_key(api_key=cfg.api_key, api_key_env=cfg.api_key_env, context="tree_builder")
        if api_key:
            kwargs["api_key"] = api_key
        if cfg.api_base:
            kwargs["api_base"] = cfg.api_base

        log.info("tree builder LLM call: model=%s", cfg.model)
        response = cached_completion(**kwargs)
        log.info("tree builder LLM call done")
        return response.choices[0].message.content.strip()

    def _parse_llm_response(self, raw: str, blocks: list[Block]) -> list[dict]:
        """Parse and validate the LLM's JSON response."""
        num_blocks = len(blocks)
        # Strip markdown code fences if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE).strip()

        sections = json.loads(cleaned)
        if not isinstance(sections, list) or not sections:
            raise ValueError("LLM returned empty or non-list response")

        # Validate and normalize
        seen_indices: set[int] = set()
        for sec in sections:
            if not isinstance(sec, dict):
                raise ValueError(f"section is not a dict: {sec}")
            sec.setdefault("title", "(untitled)")
            sec.setdefault("level", 1)
            sec.setdefault("page_start", 1)
            sec.setdefault("page_end", sec["page_start"])
            sec.setdefault("block_indices", [])
            # Validate block_indices
            sec["block_indices"] = [i for i in sec["block_indices"] if isinstance(i, int) and 0 <= i < num_blocks]
            seen_indices.update(sec["block_indices"])

        # Assign any unassigned blocks to the nearest section by page
        all_indices = set(range(num_blocks))
        missing = all_indices - seen_indices
        if missing and sections:
            for idx in sorted(missing):
                page = blocks[idx].page_no
                # Find the section whose page range contains this block
                best = sections[0]
                for sec in sections:
                    if sec["page_start"] <= page <= sec["page_end"]:
                        best = sec
                        break
                best["block_indices"].append(idx)

        return sections

    def _sections_to_tree(self, sections: list[dict], blocks: list[Block]) -> DocTree:
        """Convert the LLM's section list into a DocTree."""
        nodes: dict[str, TreeNode] = {}
        root = self._root()
        nodes[root.node_id] = root

        # Sort sections by page_start, then level
        sections.sort(key=lambda s: (s["page_start"], s["level"]))

        # Build tree using a stack (like the heading strategy)
        stack: list[tuple[int, TreeNode]] = [(0, root)]

        for sec in sections:
            level = max(1, int(sec["level"]))
            # Pop until stack top has strictly smaller level
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1]

            node = self._new_node(
                parent_id=parent.node_id,
                level=level,
                title=str(sec["title"]).strip() or "(untitled)",
                page_start=max(1, int(sec["page_start"])),
                page_end=max(1, int(sec.get("page_end", sec["page_start"]))),
            )
            nodes[node.node_id] = node
            parent.children.append(node.node_id)

            # Attach blocks
            for idx in sec.get("block_indices", []):
                if 0 <= idx < len(blocks):
                    node.block_ids.append(blocks[idx].block_id)
                    node.page_end = max(node.page_end, blocks[idx].page_no)

            # Bubble page_end up
            self._bubble_page_end(nodes, node, node.page_end)
            stack.append((level, node))

        return self._package(root=root, nodes=nodes, method="llm")

    # ==================================================================
    # Strategy 4: Page-group + LLM merge (for flat documents)
    # ==================================================================

    def from_page_groups(self, structural_hints: str = "") -> DocTree:
        """
        LLM-based tree building via page grouping:
        1. Group blocks by page windows (e.g. every 5 pages)
        2. Send group text excerpts + structural hints to LLM
        3. LLM returns: merged sections + titles + summaries
        4. Build tree from LLM output, assign blocks via page ranges

        structural_hints: optional TOC/heading signals for the LLM to
        reference (not trusted as ground truth, just hints).

        This produces a tree with summaries in a single step.
        """
        cfg = self.cfg
        if cfg is None or not cfg.model:
            log.warning("page_groups: no LLM model configured; using flat fallback")
            return self.flat_fallback()

        reading = self.doc.reading_blocks()
        if not reading:
            return self.flat_fallback()

        last_page = max((p.page_no for p in self.doc.pages), default=1)
        group_size = cfg.page_group_size if cfg else 5

        # Step 1: Group blocks by page windows
        groups: list[dict] = []
        current_group: list[Block] = []
        current_group_start = 1

        for b in reading:
            page = max(1, b.page_no)  # guard against page_no < 1
            group_idx = (page - 1) // group_size
            expected_start = group_idx * group_size + 1
            if expected_start != current_group_start and current_group:
                groups.append(
                    {
                        "page_start": current_group_start,
                        "page_end": current_group_start + group_size - 1,
                        "blocks": current_group,
                    }
                )
                current_group = []
                current_group_start = expected_start
            elif not current_group:
                current_group_start = expected_start
            current_group.append(b)

        if current_group:
            groups.append(
                {
                    "page_start": current_group_start,
                    "page_end": min(current_group_start + group_size - 1, last_page),
                    "blocks": current_group,
                }
            )

        if not groups:
            return self.flat_fallback()

        # Step 2: Build text excerpts for each group
        max_chars = cfg.group_llm_max_chars if cfg else 40000
        per_group_budget = max(200, max_chars // max(len(groups), 1))
        group_excerpts: list[str] = []
        for i, g in enumerate(groups):
            texts = []
            total = 0
            for b in g["blocks"]:
                if total >= per_group_budget:
                    break
                texts.append(b.text[: per_group_budget - total])
                total += len(texts[-1])
            excerpt = " ".join(texts)[:per_group_budget]
            group_excerpts.append(f"[Group {i + 1}] p{g['page_start']}-{g['page_end']}:\n{excerpt}")

        # Step 3: LLM call(s) — batch if needed
        try:
            sections = self._page_group_llm_call(
                group_excerpts,
                groups,
                last_page,
                structural_hints=structural_hints,
            )
        except Exception as e:
            log.warning("page_group LLM call failed (%s); using flat fallback", e)
            return self.flat_fallback()

        if not sections:
            return self.flat_fallback()

        # Step 4: Build tree from sections
        return self._page_group_sections_to_tree(sections, groups, reading)

    def _page_group_llm_call(
        self,
        group_excerpts: list[str],
        groups: list[dict],
        last_page: int,
        structural_hints: str = "",
    ) -> list[dict]:
        """One LLM call to merge groups + generate titles + summaries."""
        from forgerag.llm_cache import cached_completion

        cfg = self.cfg
        assert cfg is not None

        hint_block = ""
        if structural_hints:
            hint_block = (
                "\n\nThe following structural signals were detected in the document. "
                "They may be helpful but are NOT reliable — use them as hints only, "
                "not as ground truth:\n\n" + structural_hints + "\n"
            )

        system_msg = (
            "You are a document structure analyst. Given page-grouped text excerpts "
            "from a document, determine the logical section structure.\n\n"
            "For each section:\n"
            '  - "title": descriptive section title\n'
            '  - "groups": array of group numbers (1-based) that belong to this section\n'
            '  - "summary": 1-2 sentence summary of what this section covers\n'
            '  - "level": hierarchy level (1 = top-level, 2 = sub-section)\n\n'
            "Rules:\n"
            "- Adjacent groups on the same topic SHOULD be merged into one section.\n"
            "- Every group number must appear in exactly one section.\n"
            "- Use 1-2 levels of hierarchy. Don't over-nest.\n"
            "- Titles should be descriptive (not 'Section 1').\n"
            "- Summaries should capture the key topics discussed.\n"
            "- Return ONLY valid JSON, no markdown fences.\n"
        )

        excerpts_text = "\n\n".join(group_excerpts)
        user_msg = (
            f"Document has {last_page} pages, divided into {len(groups)} groups.\n\n"
            f"{excerpts_text}"
            f"{hint_block}\n\n"
            f'Return JSON: {{"sections": [{{"title": "...", "groups": [1, 2], '
            f'"summary": "...", "level": 1}}, ...]}}'
        )

        # Check if we need to split into batches
        max_chars = cfg.group_llm_max_chars if cfg else 40000
        if len(user_msg) > max_chars:
            return self._page_group_batched_call(group_excerpts, groups, last_page)

        kwargs: dict = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": 4096,
            "timeout": 120,
        }
        api_key = resolve_api_key(api_key=cfg.api_key, api_key_env=cfg.api_key_env, context="tree_builder")
        if api_key:
            kwargs["api_key"] = api_key
        if cfg.api_base:
            kwargs["api_base"] = cfg.api_base

        log.info("page_group LLM call: model=%s groups=%d", cfg.model, len(groups))
        response = cached_completion(**kwargs)
        raw = response.choices[0].message.content.strip()
        return self._parse_page_group_response(raw, len(groups))

    def _page_group_batched_call(
        self,
        group_excerpts: list[str],
        groups: list[dict],
        last_page: int,
    ) -> list[dict]:
        """Split into batches when total text exceeds context window."""
        from forgerag.llm_cache import cached_completion

        cfg = self.cfg
        assert cfg is not None
        max_chars = cfg.group_llm_max_chars if cfg else 40000

        # Split excerpts into batches
        batches: list[list[str]] = []
        batch_indices: list[list[int]] = []
        current_batch: list[str] = []
        current_indices: list[int] = []
        current_len = 0

        for i, excerpt in enumerate(group_excerpts):
            if current_len + len(excerpt) > max_chars and current_batch:
                batches.append(current_batch)
                batch_indices.append(current_indices)
                current_batch = []
                current_indices = []
                current_len = 0
            current_batch.append(excerpt)
            current_indices.append(i)
            current_len += len(excerpt)

        if current_batch:
            batches.append(current_batch)
            batch_indices.append(current_indices)

        all_sections: list[dict] = []
        prev_summary = ""

        for _batch_idx, (batch, indices) in enumerate(zip(batches, batch_indices, strict=False)):
            context = ""
            if prev_summary:
                context = f"Previous sections ended with: {prev_summary}\n\n"

            group_nums = [idx + 1 for idx in indices]
            system_msg = (
                "You are a document structure analyst. Given page-grouped text excerpts, "
                "determine the logical section structure.\n"
                'Return JSON: {"sections": [{"title": "...", "groups": [...], '
                '"summary": "...", "level": 1}, ...]}\n'
                "Rules: merge adjacent groups on same topic, descriptive titles, "
                "1-2 sentence summaries. Groups in this batch: " + str(group_nums)
            )

            user_msg = context + "\n\n".join(batch)

            kwargs: dict = {
                "model": cfg.model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.1,
                "max_tokens": 4096,
                "timeout": 120,
            }
            api_key = resolve_api_key(api_key=cfg.api_key, api_key_env=cfg.api_key_env, context="tree_builder")
            if api_key:
                kwargs["api_key"] = api_key
            if cfg.api_base:
                kwargs["api_base"] = cfg.api_base

            response = cached_completion(**kwargs)
            raw = response.choices[0].message.content.strip()

            # Parse with this batch's group count, not global total
            batch_sections = self._parse_page_group_response(raw, len(groups))

            # Remap: LLM may return local group numbers (1..batch_size)
            # or global ones. Clamp to valid range for this batch and
            # remap local numbers to global.
            valid_global = set(group_nums)
            for sec in batch_sections:
                remapped = []
                for g in sec.get("groups", []):
                    if g in valid_global:
                        # Already global numbering
                        remapped.append(g)
                    elif 1 <= g <= len(group_nums):
                        # Local numbering (1-based index into this batch)
                        remapped.append(group_nums[g - 1])
                # Deduplicate and filter to valid globals
                sec["groups"] = [g for g in dict.fromkeys(remapped) if g in valid_global]
                all_sections.append(sec)

            if batch_sections:
                prev_summary = batch_sections[-1].get("summary", "")

        return all_sections

    def _parse_page_group_response(self, raw: str, num_groups: int) -> list[dict]:
        """Parse LLM response for page-group strategy.

        Handles common LLM failures:
        - Sections with overlapping/duplicate group numbers
        - Sections missing group numbers entirely
        - One section hogging all groups
        """
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE).strip()

        data = json.loads(cleaned)
        sections = data.get("sections", data) if isinstance(data, dict) else data
        if not isinstance(sections, list):
            raise ValueError("Expected list of sections")

        for sec in sections:
            sec.setdefault("title", "(untitled)")
            sec.setdefault("level", 1)
            sec.setdefault("summary", "")
            sec.setdefault("groups", [])
            sec["groups"] = [g for g in sec["groups"] if isinstance(g, int) and 1 <= g <= num_groups]

        # Sanity check: if groups are distributed too unevenly, the LLM's
        # assignment is unreliable. Fall back to position-based distribution.
        max_groups_per_sec = max((len(s["groups"]) for s in sections), default=0)
        empty_count = sum(1 for s in sections if not s["groups"])

        if len(sections) > 2 and (max_groups_per_sec > num_groups * 0.4 or empty_count > len(sections) * 0.3):
            log.warning(
                "page_group: LLM group assignment unreliable "
                "(max_per_sec=%d/%d, empty=%d/%d); using position-based fallback",
                max_groups_per_sec,
                num_groups,
                empty_count,
                len(sections),
            )
            sections = self._assign_groups_by_position(sections, num_groups)
        else:
            # Normal path: deduplicate and assign orphans
            sections = self._deduplicate_and_assign_orphans(sections, num_groups)

        return sections

    def _assign_groups_by_position(self, sections: list[dict], num_groups: int) -> list[dict]:
        """Distribute groups evenly across sections by their declared order.

        Used as a fallback when LLM's group assignment is unreliable.
        Each section gets a proportional slice of the group range based
        on its position in the section list.
        """
        n = len(sections)
        if n == 0:
            return sections

        all_groups = list(range(1, num_groups + 1))
        per_sec = max(1, num_groups // n)

        for i, sec in enumerate(sections):
            start = i * per_sec
            end = num_groups if i == n - 1 else (i + 1) * per_sec
            sec["groups"] = all_groups[start:end]

        return sections

    def _deduplicate_and_assign_orphans(self, sections: list[dict], num_groups: int) -> list[dict]:
        """Deduplicate group assignments and distribute orphan groups."""
        seen: set[int] = set()
        for sec in sections:
            deduped = []
            for g in sec["groups"]:
                if g not in seen:
                    deduped.append(g)
                    seen.add(g)
            sec["groups"] = deduped

        # Assign orphan groups by proximity
        missing = sorted(set(range(1, num_groups + 1)) - seen)
        if missing and sections:
            empty_secs = [s for s in sections if not s["groups"]]
            if empty_secs:
                # Distribute orphans to empty sections by position
                # Sort sections by their index to maintain order
                sec_indices = [(i, s) for i, s in enumerate(sections) if not s["groups"]]
                for g in missing:
                    # Assign to the empty section whose position is closest
                    # to where this group would naturally fall
                    expected_sec_idx = int((g - 1) / max(1, num_groups) * len(sections))
                    _best_i, best_s = min(sec_indices, key=lambda x: abs(x[0] - expected_sec_idx))
                    best_s["groups"].append(g)
            else:
                for g in missing:
                    best = min(
                        sections,
                        key=lambda s: min(abs(g - sg) for sg in s["groups"]) if s["groups"] else 999,
                    )
                    best["groups"].append(g)

        return sections

    def _page_group_sections_to_tree(
        self,
        sections: list[dict],
        groups: list[dict],
        reading: list[Block],
    ) -> DocTree:
        """Convert page-group sections into a DocTree with summaries."""
        nodes: dict[str, TreeNode] = {}
        root = self._root()
        nodes[root.node_id] = root

        # Sort sections by their first group number
        sections.sort(key=lambda s: min(s["groups"]) if s["groups"] else 999)

        # Build group_number -> page_range lookup
        group_pages: dict[int, tuple[int, int]] = {}
        for i, g in enumerate(groups):
            group_pages[i + 1] = (g["page_start"], g["page_end"])

        stack: list[tuple[int, TreeNode]] = [(0, root)]
        section_node_pairs: list[tuple[dict, TreeNode]] = []

        for sec in sections:
            level = max(1, int(sec.get("level", 1)))
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1]

            # Compute page range from groups
            page_starts = [group_pages[g][0] for g in sec["groups"] if g in group_pages]
            page_ends = [group_pages[g][1] for g in sec["groups"] if g in group_pages]
            page_start = min(page_starts) if page_starts else 1
            page_end = max(page_ends) if page_ends else page_start

            node = self._new_node(
                parent_id=parent.node_id,
                level=level,
                title=str(sec["title"]).strip() or "(untitled)",
                page_start=page_start,
                page_end=page_end,
            )
            node.summary = sec.get("summary") or None
            nodes[node.node_id] = node
            parent.children.append(node.node_id)
            stack.append((level, node))
            section_node_pairs.append((sec, node))

        # Assign blocks to nodes by GROUP membership (not page range).
        # Using _attach_blocks_by_page would fail when sibling sections
        # have overlapping page ranges — the "deepest" heuristic would
        # assign all blocks to whichever node appears later in preorder.
        # Instead, we directly map group → blocks → node.
        group_size = self.cfg.page_group_size if self.cfg else 5
        group_blocks: dict[int, list[Block]] = {}
        for b in reading:
            page = max(1, b.page_no)
            g_idx = (page - 1) // group_size + 1
            group_blocks.setdefault(g_idx, []).append(b)

        assigned_blocks: set[str] = set()
        for sec, node in section_node_pairs:
            for g in sec.get("groups", []):
                for b in group_blocks.get(g, []):
                    if b.block_id not in assigned_blocks:
                        node.block_ids.append(b.block_id)
                        assigned_blocks.add(b.block_id)
                        node.page_end = max(node.page_end, b.page_no)

        # Assign any unassigned blocks to root
        for b in reading:
            if b.block_id not in assigned_blocks:
                root.block_ids.append(b.block_id)

        return self._package(root=root, nodes=nodes, method="page_groups")

    # ==================================================================
    # Fallback: single section containing all reading blocks
    # ==================================================================

    def flat_fallback(self) -> DocTree:
        nodes: dict[str, TreeNode] = {}
        root = self._root()
        nodes[root.node_id] = root

        section = self._new_node(
            parent_id=root.node_id,
            level=1,
            title="Document",
            page_start=root.page_start,
            page_end=root.page_end,
        )
        nodes[section.node_id] = section
        root.children.append(section.node_id)

        for block in self.doc.reading_blocks():
            section.block_ids.append(block.block_id)

        return self._package(root=root, nodes=nodes, method="fallback")

    # ==================================================================
    # Helpers
    # ==================================================================

    def _attach_blocks_by_page(self, nodes: dict[str, TreeNode], root: TreeNode) -> None:
        """
        Assign each reading block to a TOC node by page.

        For each block, find the deepest node whose [page_start, page_end]
        contains block.page_no. If multiple nodes at the same depth match
        (happens when a section starts mid-page), pick the one that appears
        latest in preorder up to and including the block's page.
        """
        # Build preorder list of non-root nodes for tie-breaking
        preorder: list[TreeNode] = []
        for n in self._preorder(nodes, root):
            if n.node_id != root.node_id:
                preorder.append(n)

        for block in self.doc.reading_blocks():
            target = self._deepest_containing(preorder, block.page_no) or root
            target.block_ids.append(block.block_id)

    def _preorder(self, nodes: dict[str, TreeNode], start: TreeNode) -> list[TreeNode]:
        out: list[TreeNode] = []
        stack = [start.node_id]
        while stack:
            nid = stack.pop()
            node = nodes[nid]
            out.append(node)
            for cid in reversed(node.children):
                stack.append(cid)
        return out

    def _deepest_containing(self, preorder: list[TreeNode], page_no: int) -> TreeNode | None:
        best: TreeNode | None = None
        for node in preorder:
            if node.page_start <= page_no <= node.page_end:
                if best is None or node.level > best.level:
                    best = node
                elif node.level == best.level and node.page_start >= best.page_start:
                    # Later-starting node at same level wins
                    best = node
        return best

    @staticmethod
    def _bubble_page_end(nodes: dict[str, TreeNode], node: TreeNode, page_no: int) -> None:
        cur: str | None = node.parent_id
        while cur is not None:
            parent = nodes[cur]
            if parent.page_end >= page_no:
                return
            parent.page_end = page_no
            cur = parent.parent_id


# ---------------------------------------------------------------------------
# Enrichment and quality scoring
# ---------------------------------------------------------------------------


def _finalize_node_enrichment(
    node: TreeNode, doc: ParsedDocument, blocks_index: dict[str, Block] | None = None
) -> None:
    """Compute element_types / counts / content_hash for a node."""
    if blocks_index is None:
        blocks_index = doc.blocks_by_id()
    types: set[str] = set()
    tables = 0
    figures = 0
    hasher = hashlib.sha1()

    for bid in node.block_ids:
        b = blocks_index.get(bid)
        if b is None:
            continue
        types.add(b.type.value)
        if b.type == BlockType.TABLE:
            tables += 1
        elif b.type == BlockType.FIGURE:
            figures += 1
        hasher.update(b.text.encode("utf-8", errors="ignore"))
        hasher.update(b"\x00")

    node.element_types = sorted(types)
    node.table_count = tables
    node.figure_count = figures
    node.content_hash = hasher.hexdigest()


def _quality_score(tree: DocTree, doc: ParsedDocument, cfg: TreeBuilderConfig) -> float:
    """
    0~1 score. Components:
        - coverage:  fraction of doc pages that fall inside some leaf's
                     page_range (proxy for "no orphaned pages")
        - balance:   how evenly sized the leaves are (by page span)
        - depth:     penalty for pathological (flat or extremely deep) trees
        - density:   leaf pages per leaf close to cfg.target_leaf_pages
    """
    leaves = [n for n in tree.leaves() if n.node_id != tree.root_id]
    if not leaves:
        return 0.0

    total_pages = max(doc.profile.page_count, 1)
    covered = set()
    for leaf in leaves:
        for p in range(leaf.page_start, leaf.page_end + 1):
            covered.add(p)
    coverage = min(1.0, len(covered) / total_pages)

    # Balance: 1 - normalized stddev of leaf page spans
    spans = [max(1, leaf.page_end - leaf.page_start + 1) for leaf in leaves]
    mean = sum(spans) / len(spans)
    var = sum((s - mean) ** 2 for s in spans) / len(spans)
    std = math.sqrt(var)
    balance = 1.0 - min(1.0, std / (mean + 1e-6))

    # Depth penalty: reward depth in [2, max_reasonable_depth]
    max_depth = max(n.level for n in tree.nodes.values())
    if max_depth < 1:
        depth_score = 0.3
    elif max_depth > cfg.max_reasonable_depth:
        depth_score = max(0.0, 1.0 - 0.15 * (max_depth - cfg.max_reasonable_depth))
    else:
        depth_score = min(1.0, max_depth / 3.0)

    # Density: how close avg leaf span is to target
    target = cfg.target_leaf_pages
    density = 1.0 - min(1.0, abs(mean - target) / target)

    score = 0.4 * coverage + 0.2 * balance + 0.2 * depth_score + 0.2 * density
    return round(score, 3)
