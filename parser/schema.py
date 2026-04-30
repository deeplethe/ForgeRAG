"""
Parser layer data contract.

All parser backends (PyMuPDF / MinerU / MinerU-VLM) produce a
ParsedDocument that conforms to this schema. Downstream modules
(tree builder, chunker, retriever, citation resolver) depend only
on this file.

Coordinate system
-----------------
All bbox values use the native PDF coordinate system:
    - origin at bottom-left of the page
    - units in points (1 pt = 1/72 inch)
    - tuple order: (x0, y0, x1, y1) where x0<x1, y0<y1
Do NOT normalize. The frontend (PDF.js) handles conversion to
viewport coordinates via page.getViewport().convertToViewportRectangle.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

BBox = tuple[float, float, float, float]  # (x0, y0, x1, y1) in PDF points


class DocFormat(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    HTML = "html"
    TEXT = "text"
    IMAGE = "image"


class BlockType(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    IMAGE = "image"  # raster image / chart / diagram (was: FIGURE)
    FORMULA = "formula"
    CODE = "code"  # code block (markdown ``` fences, monospace blocks)
    CAPTION = "caption"
    HEADER = "header"  # page header (usually excluded from reading flow)
    FOOTER = "footer"  # page footer (usually excluded from reading flow)


# ---------------------------------------------------------------------------
# Document profile — cheap, always-computed features
# ---------------------------------------------------------------------------


@dataclass
class DocProfile:
    """Lightweight per-document metadata.

    Trimmed from the legacy multi-tier-probe shape (complexity / needed_tier
    / scanned_ratio / table_density / multicolumn / TOC / etc.) which was
    only useful when the parser had a fallback chain. Now the user picks
    the backend explicitly and we just need a few facts the downstream
    layers actually consume:

      * ``page_count``           — read by tree_builder for proportionality
      * ``heading_hint_strength``— read by md_headings + tree_builder to
                                   decide whether to use the heading-based
                                   strategy vs. fallback grouping
      * ``format`` / ``file_size_bytes`` — observability
    """

    page_count: int
    format: DocFormat
    file_size_bytes: int
    heading_hint_strength: float = 0.0  # 0~1; bumped by md_headings


# ---------------------------------------------------------------------------
# Parse trace (observability)
# ---------------------------------------------------------------------------


@dataclass
class ParseTrace:
    """Single-backend parse summary.

    The legacy multi-attempt fallback chain is gone — there's exactly one
    backend per parse now, picked by ``parser.backend`` config. We keep
    a flat record for observability and DB compatibility.
    """

    backend: str | None = None
    duration_ms: int = 0
    error_message: str | None = None


# ---------------------------------------------------------------------------
# TOC (preserved verbatim if the source file has an embedded one)
# ---------------------------------------------------------------------------


@dataclass
class TocEntry:
    level: int  # 1-based
    title: str
    page_no: int  # 1-based page number
    children: list[TocEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """
    Smallest addressable unit of content. Every citation in the system
    ultimately resolves to one or more Block ids.

    block_id format: "{doc_id}:{parse_version}:{page_no}:{seq}"
    This makes ids stable within a parse_version and trivially
    decomposable for debugging.
    """

    block_id: str
    doc_id: str
    parse_version: int
    page_no: int  # 1-based
    seq: int  # order within the page
    bbox: BBox  # PDF points, origin bottom-left
    type: BlockType

    text: str  # for tables: markdown rendering
    level: int | None = None  # heading level, 1~6
    confidence: float = 1.0  # parser confidence 0~1

    # Table payload
    table_html: str | None = None
    table_markdown: str | None = None

    # Image payload (stored via BlobStore; these are the lookup keys).
    # Renamed from ``figure_*`` since ``image`` covers the broader
    # taxonomy (raster image / chart / diagram / icon) not just
    # academic-style figures.
    image_storage_key: str | None = None
    image_mime: str | None = None
    image_caption: str | None = None

    # Formula payload
    formula_latex: str | None = None

    # Code payload — preserved verbatim so language detection / syntax
    # highlighting can run downstream. Currently populated only when
    # the parser backend specifically tags a block as CODE.
    code_text: str | None = None
    code_language: str | None = None

    # Normalizer flags -- never delete blocks, only mark them
    excluded: bool = False  # True for header/footer/noise
    excluded_reason: str | None = None

    # Cross-refs discovered at parse time (optional; filled by normalizer)
    caption_of: str | None = None  # this caption describes block_id X

    # Inline references this block makes to other blocks in the same doc,
    # e.g. text "as shown in Figure 3" -> [block_id of Figure 3].
    # Populated by normalizer._resolve_inline_references.
    cross_ref_targets: list[str] = field(default_factory=list)


@dataclass
class Page:
    page_no: int  # 1-based
    width: float  # in points
    height: float  # in points
    block_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level ParsedDocument -- the single contract with downstream
# ---------------------------------------------------------------------------


@dataclass
class ParsedDocument:
    doc_id: str
    filename: str
    format: DocFormat
    parse_version: int  # bumped on every re-parse

    profile: DocProfile
    parse_trace: ParseTrace

    pages: list[Page]
    blocks: list[Block]  # flat, reading order across pages
    toc: list[TocEntry] | None = None

    def reading_blocks(self) -> list[Block]:
        """Blocks in reading order, excluding headers/footers/noise."""
        return [b for b in self.blocks if not b.excluded]

    def blocks_by_id(self) -> dict[str, Block]:
        return {b.block_id: b for b in self.blocks}


# ---------------------------------------------------------------------------
# Citation (retrieval -> viewer highlight)
# ---------------------------------------------------------------------------


@dataclass
class HighlightRect:
    page_no: int
    bbox: BBox


# ---------------------------------------------------------------------------
# Document tree (PageIndex-style hierarchical index)
# ---------------------------------------------------------------------------


@dataclass
class TreeNode:
    """
    A single node in the document tree.

    A node represents either:
        - the whole document (root, level=0, title = filename)
        - a section/subsection identified from TOC, heading blocks,
          or LLM-inferred structure.

    block_ids holds only the blocks DIRECTLY owned by this node, not
    the union with descendants. Callers that need "all blocks under
    this subtree" should traverse children.

    node_id format: "{doc_id}:{parse_version}:n{seq}" where seq is
    the preorder index assigned at build time.
    """

    node_id: str
    doc_id: str
    parse_version: int
    parent_id: str | None  # None for root
    level: int  # 0 = root, 1.. = section depth
    title: str
    page_start: int  # 1-based, inclusive
    page_end: int  # 1-based, inclusive
    block_ids: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)  # ordered node_ids

    # Enrichment (cheap, computed at build time)
    element_types: list[str] = field(default_factory=list)  # BlockType values
    table_count: int = 0
    image_count: int = 0  # was: figure_count
    content_hash: str = ""  # hash of concatenated block text

    # Deferred enrichment (filled by later passes)
    summary: str | None = None
    key_entities: list[str] = field(default_factory=list)
    cross_reference_targets: list[str] = field(default_factory=list)  # node_ids

    # Section role — drives downstream filtering. ``main`` is body
    # content (default). The non-main values let KG extraction skip
    # noise sources (TOC, Index, Bibliography, Front matter) and let
    # retrieval optionally downweight supplementary material.
    #   "main"          - body chapter/section content
    #   "front_matter"  - copyright, dedication, foreword, acknowledgements
    #   "toc"           - table of contents
    #   "glossary"      - definitions list (KEPT for KG; high-quality)
    #   "appendix"      - supplementary content (KEPT for KG)
    #   "bibliography"  - references / works cited
    #   "index"         - alphabetical term index
    # Populated by tree_builder via LLM tag + regex fallback.
    role: str = "main"


@dataclass
class DocTree:
    """Flat storage for a document tree. Access via node_id lookups."""

    doc_id: str
    parse_version: int
    root_id: str
    nodes: dict[str, TreeNode]
    quality_score: float
    generation_method: Literal["toc", "headings", "llm", "page_groups", "fallback"]

    def get(self, node_id: str) -> TreeNode:
        return self.nodes[node_id]

    def root(self) -> TreeNode:
        return self.nodes[self.root_id]

    def leaves(self) -> list[TreeNode]:
        return [n for n in self.nodes.values() if not n.children]

    def walk_preorder(self, start: str | None = None) -> Iterator[TreeNode]:
        stack = [start or self.root_id]
        while stack:
            nid = stack.pop()
            node = self.nodes[nid]
            yield node
            # Push children in reverse so they pop in order
            for cid in reversed(node.children):
                stack.append(cid)

    def ancestors(self, node_id: str) -> list[TreeNode]:
        """Return ancestors from root to direct parent (excludes self)."""
        chain: list[TreeNode] = []
        node = self.nodes[node_id]
        while node.parent_id is not None:
            parent = self.nodes[node.parent_id]
            chain.append(parent)
            node = parent
        chain.reverse()
        return chain


# ---------------------------------------------------------------------------
# Chunks (retrieval unit)
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    """
    A retrieval-unit chunk produced by the chunker.

    Invariants:
        - block_ids are contiguous within a single tree node (one
          chunk never spans multiple nodes).
        - content_type reflects the structural kind of content:
            "text":    ordinary prose / lists / headings
            "table":   a single table block (with caption)
            "image":   a single image block (was: figure)
            "formula": a single formula block
            "code":    a single code block (markdown ``` fence,
                       monospace block, etc.)
            "mixed":   text chunk that happens to include a structural
                       (non-text-like) block — image, table, formula
                       or code — because the corresponding
                       ``isolate_*`` was disabled. Heading + paragraph
                       + list combinations are NOT mixed, they're
                       still ``text``.

    chunk_id format: "{doc_id}:{parse_version}:c{seq}"
    """

    chunk_id: str
    doc_id: str
    parse_version: int
    node_id: str  # owning tree node (leaf or inner)
    block_ids: list[str]  # ordered, contiguous
    content: str  # joined block texts

    content_type: Literal["text", "table", "image", "formula", "code", "mixed"]
    page_start: int
    page_end: int
    token_count: int  # approximate, per ChunkerConfig.tokenizer

    # Structural context -- filled at chunking time from the tree.
    # section_path goes root -> owning node inclusive, by title.
    # ancestor_node_ids goes root -> direct parent (excludes owning node).
    section_path: list[str] = field(default_factory=list)
    ancestor_node_ids: list[str] = field(default_factory=list)

    # Sort key: negative y1 of the first block on page_start.
    # PDF y origin is bottom-left (higher y = higher on page), so
    # negating gives ascending sort = top-to-bottom reading order.
    y_sort: float = 0.0

    # Cross-references: other chunks that blocks in this chunk point at
    # via their block.cross_ref_targets. Filled in a second pass after
    # all chunks are emitted. Deduped, excludes self.
    cross_ref_chunk_ids: list[str] = field(default_factory=list)

    # Inherited from owning ``TreeNode.role``. Lets KG extraction skip
    # noise sources (Index, TOC, Bibliography, Front matter) without
    # re-walking the tree, and lets retrieval downweight supplementary
    # content (Appendix). See ``TreeNode.role`` for the value set.
    role: str = "main"


# ---------------------------------------------------------------------------
# Citation (retrieval -> viewer highlight)
# ---------------------------------------------------------------------------


@dataclass
class Citation:
    """
    A single answer-span citation. Produced by the retrieval/rerank
    layer and consumed by the frontend PDF viewer.

    The viewer reads `highlights` and draws annotation-layer rectangles
    on top of the PDF.js page; `page_no` is used for initial scroll.
    `file_id` is the FileStore identifier the viewer should use to
    fetch the source PDF blob.
    """

    citation_id: str  # short id, e.g. "c_12"
    chunk_id: str  # full chunk_id for traceability
    doc_id: str
    parse_version: int  # for version-mismatch detection
    block_ids: list[str]  # ordered, may span multiple blocks
    page_no: int  # first block's page, for jump
    highlights: list[HighlightRect]
    snippet: str  # <=200 chars, for hover preview
    score: float  # rerank score
    file_id: str | None = None  # FileStore file_id for PDF viewing (may be converted)
    source_file_id: str | None = None  # original file_id (only if converted, for download)
    source_format: str = ""  # original format, e.g. "docx" (empty if native PDF)
    open_url: str | None = None  # e.g. /viewer/{doc_id}?page=14&hl=c_12
    # NOTE: filename is intentionally NOT stored here. ``doc_id`` is the
    # stable identity; the display name is mutable (rename, reorganize)
    # and is resolved at render-time via /api/v1/documents/{doc_id} —
    # otherwise persisted citations would carry stale names forever.
