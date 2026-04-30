"""
Document chunker.

Walks a DocTree in preorder and emits a list of Chunks that satisfy:

    - Every chunk belongs to exactly one tree node (the node whose
      block_ids it consumes). No cross-node chunks.
    - Chunk boundaries always coincide with block boundaries -- never
      splits a block in the middle. This is what makes citation
      highlighting precise: every chunk -> concrete bbox set.
    - Tables, images, formulas, and code are emitted as their own
      single-block chunks so that embeddings of heterogeneous content
      stay clean (controlled by ChunkerConfig.isolate_*).
    - Text blocks are greedy-packed to target_tokens, never exceeding
      max_tokens, with small trailing chunks merged into their
      predecessor when possible.
    - A second pass converts block-level cross_ref_targets into
      chunk-level cross_ref_chunk_ids using a block_id -> chunk_id
      index.

Token counting uses a cheap char-based approximation by default
(no extra dependency). CJK chars count as ~1.5 tokens, others as
~0.25 tokens, which matches empirical tiktoken ratios within ~15%.
Swap in a real tokenizer later via ChunkerConfig.tokenizer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from config import ChunkerConfig

from .schema import (
    Block,
    BlockType,
    Chunk,
    DocTree,
    ParsedDocument,
    TreeNode,
)

log = logging.getLogger(__name__)

# Blocks whose text is purely formatting noise (markdown bold/italic
# markers, stray punctuation/digits) are worthless as standalone chunks.
_RE_NOISE_BLOCK = re.compile(r"^[\s\*_#`~\-=\+\|\\/\[\](){}<>!?@$%^&;:,.\d]*$")

# Max char length for a heading we'll trust as a real section break.
# MinerU occasionally misclassifies a leading body sentence as a
# heading; gating at 100 chars keeps real H1-H4 (~60 chars max in
# practice) while rejecting body text. tree_builder uses 200 chars
# in ``_is_junk_heading``; we're stricter here because a false
# positive at chunker-flush time directly fragments output, while
# tree_builder can recover via majority structure.
_HEADING_MAX_CHARS = 100


def _is_real_heading(b: Block) -> bool:
    """Heading-likeness gate used by the chunker's heading-aware flush.

    A block must look authentically like a section header before we
    let it force a chunk boundary. Three filters:
      1. Type tagged HEADING by the parser.
      2. Text exists and is at most ``_HEADING_MAX_CHARS`` chars
         (catches body sentences mislabeled as headings).
      3. Text is not pure formatting noise (catches ``***``, ``---``,
         stray punctuation that MinerU sometimes promotes to H4).
    """
    if b.type != BlockType.HEADING:
        return False
    t = (b.text or "").strip()
    if not t or len(t) > _HEADING_MAX_CHARS:
        return False
    return not _RE_NOISE_BLOCK.match(t)


# Category used internally to decide packing behavior. Mirrors
# ``Chunk.content_type`` minus ``mixed`` (which is computed at
# emission time based on the actual block-type set).
_Category = Literal["text", "table", "image", "formula", "code"]

# Block types that count as "text-like" for the purpose of
# ``mixed`` detection. A chunk made up entirely of these stays
# ``text`` — heading + paragraph + list is the normal shape and
# shouldn't be flagged as anything special.
_TEXT_LIKE_TYPES = frozenset(
    {BlockType.PARAGRAPH, BlockType.HEADING, BlockType.LIST, BlockType.CAPTION}
)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


class Chunker:
    def __init__(self, cfg: ChunkerConfig):
        self.cfg = cfg

    def chunk(self, doc: ParsedDocument, tree: DocTree) -> list[Chunk]:
        ctx = _ChunkContext(doc=doc, tree=tree, cfg=self.cfg)

        chunks: list[Chunk] = []
        blocks_index = doc.blocks_by_id()

        for node in tree.walk_preorder():
            if not node.block_ids:
                continue
            chunks.extend(ctx.chunk_node(node, blocks_index))

        _fill_cross_refs(chunks, blocks_index)

        log.info("chunker doc=%s total_chunks=%d", doc.doc_id, len(chunks))
        return chunks


# ---------------------------------------------------------------------------
# Per-document context
# ---------------------------------------------------------------------------


@dataclass
class _ChunkContext:
    doc: ParsedDocument
    tree: DocTree
    cfg: ChunkerConfig
    _seq: int = 0

    # ------------------------------------------------------------------
    def _new_id(self) -> str:
        self._seq += 1
        return f"{self.doc.doc_id}:{self.doc.parse_version}:c{self._seq}"

    # ------------------------------------------------------------------
    def chunk_node(self, node: TreeNode, blocks_index: dict[str, Block]) -> list[Chunk]:
        # Resolve blocks in order, skip any excluded / missing / noise
        blocks: list[Block] = []
        for bid in node.block_ids:
            b = blocks_index.get(bid)
            if b is None or b.excluded:
                continue
            # Skip text/paragraph blocks that are pure formatting noise
            # (e.g. lone "*", "**", "---", stray punctuation from PDF OCR)
            if b.type in (BlockType.PARAGRAPH, BlockType.HEADING, BlockType.LIST):
                if _RE_NOISE_BLOCK.match(b.text):
                    continue
            blocks.append(b)
        if not blocks:
            return []

        section_path, ancestor_ids = self._compute_context(node)
        runs = _segment_runs(blocks, self.cfg)

        out: list[Chunk] = []
        for category, run_blocks in runs:
            if category in ("table", "image", "formula", "code"):
                for b in run_blocks:
                    out.append(
                        self._mk_chunk(
                            blocks=[b],
                            content_type=category,
                            node=node,
                            section_path=section_path,
                            ancestor_ids=ancestor_ids,
                        )
                    )
            else:
                out.extend(
                    self._pack_text_run(
                        run_blocks=run_blocks,
                        node=node,
                        section_path=section_path,
                        ancestor_ids=ancestor_ids,
                    )
                )
        return out

    # ------------------------------------------------------------------
    def _compute_context(self, node: TreeNode) -> tuple[list[str], list[str]]:
        ancestors = self.tree.ancestors(node.node_id)  # root -> parent
        section_path = [a.title for a in ancestors if a.title] + [node.title]
        ancestor_ids = [a.node_id for a in ancestors]
        return section_path, ancestor_ids

    # ------------------------------------------------------------------
    def _pack_text_run(
        self,
        *,
        run_blocks: list[Block],
        node: TreeNode,
        section_path: list[str],
        ancestor_ids: list[str],
    ) -> list[Chunk]:
        cfg = self.cfg
        chunks: list[Chunk] = []

        current: list[Block] = []
        current_tokens = 0

        def flush() -> None:
            nonlocal current, current_tokens
            if not current:
                return
            chunks.append(
                self._mk_chunk(
                    blocks=current,
                    content_type="text",
                    node=node,
                    section_path=section_path,
                    ancestor_ids=ancestor_ids,
                )
            )
            if cfg.overlap_blocks > 0 and len(current) > cfg.overlap_blocks:
                tail = current[-cfg.overlap_blocks :]
                current = list(tail)
                current_tokens = sum(approx_tokens(b.text) for b in current)
            else:
                current = []
                current_tokens = 0

        for b in run_blocks:
            bt = approx_tokens(b.text)
            if bt > cfg.max_tokens:
                # Oversized single block -- flush pending, then emit alone.
                flush()
                chunks.append(
                    self._mk_chunk(
                        blocks=[b],
                        content_type="text",
                        node=node,
                        section_path=section_path,
                        ancestor_ids=ancestor_ids,
                    )
                )
                continue
            # Heading-aware flush: a real heading should start a new
            # chunk so embeddings respect author-declared section
            # boundaries, BUT only flush if the current chunk has
            # already accumulated >= ``min_tokens``. Without that
            # guard, consecutive small headings (TOCs, chapter
            # spreads) would each become a 5-token chunk. The
            # ``_is_real_heading`` gate filters out MinerU misclas-
            # sifications and noise so we never flush on garbage.
            if (
                _is_real_heading(b)
                and current
                and current_tokens >= cfg.min_tokens
            ):
                flush()
            if current and current_tokens + bt > cfg.target_tokens:
                flush()
            current.append(b)
            current_tokens += bt

        if current:
            chunks.append(
                self._mk_chunk(
                    blocks=current,
                    content_type="text",
                    node=node,
                    section_path=section_path,
                    ancestor_ids=ancestor_ids,
                )
            )

        # Merge trailing tiny chunk into its predecessor within this run
        # if below min_tokens. Only merge if combined size is still
        # within max_tokens (preserve the hard cap).
        if len(chunks) >= 2 and chunks[-1].token_count < cfg.min_tokens and chunks[-2].content_type == "text":
            combined_tokens = chunks[-2].token_count + chunks[-1].token_count
            if combined_tokens <= cfg.max_tokens:
                merged = self._merge_chunks(chunks[-2], chunks[-1])
                chunks = [*chunks[:-2], merged]

        return chunks

    # ------------------------------------------------------------------
    def _mk_chunk(
        self,
        *,
        blocks: list[Block],
        content_type: _Category,
        node: TreeNode,
        section_path: list[str],
        ancestor_ids: list[str],
    ) -> Chunk:
        content = _join_block_texts(blocks, content_type)
        token_count = approx_tokens(content)
        page_start = min(b.page_no for b in blocks)
        page_end = max(b.page_no for b in blocks)

        # y_sort: negate y1 of first block on page_start so ascending = top-to-bottom
        _first = [b for b in blocks if b.page_no == page_start and b.bbox]
        y_sort = -max(b.bbox[3] for b in _first) if _first else 0.0

        # Upgrade content_type to "mixed" only when a *structural*
        # block (image / table / formula / code) leaks into a text
        # run. heading + paragraph + list combinations are normal
        # text chunks and stay ``text`` — the previous "any 2 types
        # → mixed" rule flagged 60%+ of chunks unhelpfully.
        types = {b.type for b in blocks}
        ctype: str = content_type
        if content_type == "text" and (types - _TEXT_LIKE_TYPES):
            ctype = "mixed"

        return Chunk(
            chunk_id=self._new_id(),
            doc_id=self.doc.doc_id,
            parse_version=self.doc.parse_version,
            node_id=node.node_id,
            block_ids=[b.block_id for b in blocks],
            content=content,
            content_type=ctype,  # type: ignore[arg-type]
            page_start=page_start,
            page_end=page_end,
            token_count=token_count,
            y_sort=y_sort,
            section_path=section_path,
            ancestor_node_ids=ancestor_ids,
            # Inherited from owning tree node so KG extraction can
            # filter out noise sources (Index / TOC / Bibliography /
            # Front matter) without re-walking the tree.
            role=node.role,
        )

    # ------------------------------------------------------------------
    def _merge_chunks(self, a: Chunk, b: Chunk) -> Chunk:
        """Merge chunk b into a. Keeps a's id (stable downstream)."""
        seen: set[str] = set()
        merged_blocks: list[str] = []
        for bid in a.block_ids + b.block_ids:
            if bid not in seen:
                seen.add(bid)
                merged_blocks.append(bid)
        merged_content = a.content + "\n\n" + b.content
        return Chunk(
            chunk_id=a.chunk_id,
            doc_id=a.doc_id,
            parse_version=a.parse_version,
            node_id=a.node_id,
            block_ids=merged_blocks,
            content=merged_content,
            content_type=a.content_type,
            page_start=min(a.page_start, b.page_start),
            page_end=max(a.page_end, b.page_end),
            token_count=a.token_count + b.token_count,
            y_sort=min(a.y_sort, b.y_sort),
            section_path=a.section_path,
            ancestor_node_ids=a.ancestor_node_ids,
            role=a.role,
        )


# ---------------------------------------------------------------------------
# Run segmentation
# ---------------------------------------------------------------------------


def _segment_runs(blocks: list[Block], cfg: ChunkerConfig) -> list[tuple[_Category, list[Block]]]:
    """Group consecutive blocks by category (text vs isolated types)."""
    runs: list[tuple[_Category, list[Block]]] = []
    current_cat: _Category | None = None
    current: list[Block] = []

    for b in blocks:
        cat = _classify(b, cfg)
        if cat == current_cat:
            current.append(b)
        else:
            if current:
                runs.append((current_cat, current))  # type: ignore[arg-type]
            current_cat = cat
            current = [b]
    if current:
        runs.append((current_cat, current))  # type: ignore[arg-type]
    return runs


def _classify(block: Block, cfg: ChunkerConfig) -> _Category:
    if block.type == BlockType.TABLE and cfg.isolate_tables:
        return "table"
    if block.type == BlockType.IMAGE and cfg.isolate_images:
        return "image"
    if block.type == BlockType.FORMULA and cfg.isolate_formulas:
        return "formula"
    if block.type == BlockType.CODE and cfg.isolate_code:
        return "code"
    return "text"


# ---------------------------------------------------------------------------
# Content joining
# ---------------------------------------------------------------------------


def _join_block_texts(blocks: list[Block], category: _Category) -> str:
    """
    Join block texts into the chunk body.

    Text chunks use ``\\n\\n`` between blocks so paragraph boundaries
    are preserved. Structural categories (table / image / formula /
    code) prefer their typed payload when one is present.
    """
    if category == "table" and len(blocks) == 1:
        b = blocks[0]
        return b.table_markdown or b.text or (b.table_html or "")
    if category == "formula" and len(blocks) == 1:
        b = blocks[0]
        return b.formula_latex or b.text
    if category == "image" and len(blocks) == 1:
        b = blocks[0]
        caption = b.image_caption or ""
        return caption or b.text or f"[image:{b.block_id}]"
    if category == "code" and len(blocks) == 1:
        b = blocks[0]
        return b.code_text or b.text or ""
    return "\n\n".join(b.text for b in blocks if b.text)


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


def approx_tokens(text: str) -> int:
    """
    Cheap char-based approximation. CJK chars ~1.5 tokens each
    (real ratio varies 1.2 ~ 1.7 with tiktoken cl100k), other chars
    ~0.25 tokens (roughly one token per 4 chars of English).
    """
    if not text:
        return 0
    cjk = 0
    for c in text:
        if "\u4e00" <= c <= "\u9fff":
            cjk += 1
    other = len(text) - cjk
    return max(1, int(cjk * 1.5 + other * 0.25))


# ---------------------------------------------------------------------------
# Cross-ref second pass
# ---------------------------------------------------------------------------


def _fill_cross_refs(chunks: list[Chunk], blocks_index: dict[str, Block]) -> None:
    """
    For each chunk, look up block.cross_ref_targets and translate
    them into chunk_ids via a block_id -> chunk_id index.

    If a block is referenced by multiple chunks (e.g. overlap_blocks
    > 0), block_to_chunk maps to the LAST chunk containing it -- a
    deliberate simplification. Overlap is off by default.
    """
    block_to_chunk: dict[str, str] = {}
    for c in chunks:
        for bid in c.block_ids:
            block_to_chunk[bid] = c.chunk_id

    for c in chunks:
        targets: list[str] = []
        seen: set[str] = {c.chunk_id}
        for bid in c.block_ids:
            b = blocks_index.get(bid)
            if b is None:
                continue
            for tgt_bid in b.cross_ref_targets:
                tgt_cid = block_to_chunk.get(tgt_bid)
                if tgt_cid and tgt_cid not in seen:
                    targets.append(tgt_cid)
                    seen.add(tgt_cid)
        c.cross_ref_chunk_ids = targets
