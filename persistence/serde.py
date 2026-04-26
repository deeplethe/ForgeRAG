"""
Dataclass <-> dict converters for the persistence layer.

Rationale for not using dataclasses.asdict() directly:

    - We need to turn Enum fields (BlockType, DocFormat, Complexity)
      into their .value strings because the DB stores text.
    - We want control over which fields go into JSONB vs scalar
      columns (DocProfile and ParseTrace go to JSONB; bbox is split
      into four REAL columns).
    - We want to be able to rebuild a ParsedDocument from the DB
      without passing through intermediate dict shapes.

Every function here is pure and does not touch a DB connection.
"""

from __future__ import annotations

from typing import Any

from parser.schema import (
    Block,
    BlockType,
    Chunk,
    DocFormat,
    DocProfile,
    DocTree,
    Page,
    ParseTrace,
    TreeNode,
)

# ---------------------------------------------------------------------------
# DocProfile / ParseTrace -> dict (for JSONB)
# ---------------------------------------------------------------------------


def profile_to_dict(p: DocProfile) -> dict[str, Any]:
    return {
        "page_count": p.page_count,
        "format": p.format.value,
        "file_size_bytes": p.file_size_bytes,
        "heading_hint_strength": p.heading_hint_strength,
    }


def profile_from_dict(d: dict[str, Any]) -> DocProfile:
    """Tolerant of legacy rows that still have the old fat schema
    (text_density / scanned_ratio / complexity / needed_tier / etc.) —
    those keys are silently ignored."""
    return DocProfile(
        page_count=d["page_count"],
        format=DocFormat(d["format"]),
        file_size_bytes=d["file_size_bytes"],
        heading_hint_strength=d.get("heading_hint_strength", 0.0),
    )


def trace_to_dict(t: ParseTrace) -> dict[str, Any]:
    return {
        "backend": t.backend,
        "duration_ms": t.duration_ms,
        "error_message": t.error_message,
    }


def trace_from_dict(d: dict[str, Any]) -> ParseTrace:
    """Tolerant of legacy rows that have the old multi-attempt fallback
    schema (``attempts`` / ``final_backend`` / ``total_duration_ms``);
    we collapse them onto the new flat shape."""
    return ParseTrace(
        backend=d.get("backend") or d.get("final_backend"),
        duration_ms=d.get("duration_ms") or d.get("total_duration_ms") or 0,
        error_message=d.get("error_message"),
    )


# ---------------------------------------------------------------------------
# Block <-> row
# ---------------------------------------------------------------------------


def block_to_row(b: Block) -> dict[str, Any]:
    return {
        "block_id": b.block_id,
        "doc_id": b.doc_id,
        "parse_version": b.parse_version,
        "page_no": b.page_no,
        "seq": b.seq,
        "bbox_x0": b.bbox[0],
        "bbox_y0": b.bbox[1],
        "bbox_x1": b.bbox[2],
        "bbox_y1": b.bbox[3],
        "type": b.type.value,
        "level": b.level,
        "text": b.text,
        "confidence": b.confidence,
        "table_html": b.table_html,
        "table_markdown": b.table_markdown,
        "figure_storage_key": b.figure_storage_key,
        "figure_mime": b.figure_mime,
        "figure_caption": b.figure_caption,
        "formula_latex": b.formula_latex,
        "excluded": b.excluded,
        "excluded_reason": b.excluded_reason,
        "caption_of": b.caption_of,
        "cross_ref_targets": list(b.cross_ref_targets),
    }


def row_to_block(r: dict[str, Any]) -> Block:
    return Block(
        block_id=r["block_id"],
        doc_id=r["doc_id"],
        parse_version=r["parse_version"],
        page_no=r["page_no"],
        seq=r["seq"],
        bbox=(r["bbox_x0"], r["bbox_y0"], r["bbox_x1"], r["bbox_y1"]),
        type=BlockType(r["type"]),
        level=r["level"],
        text=r["text"],
        confidence=r["confidence"],
        table_html=r["table_html"],
        table_markdown=r["table_markdown"],
        figure_storage_key=r["figure_storage_key"],
        figure_mime=r["figure_mime"],
        figure_caption=r["figure_caption"],
        formula_latex=r["formula_latex"],
        excluded=r["excluded"],
        excluded_reason=r["excluded_reason"],
        caption_of=r["caption_of"],
        cross_ref_targets=list(r.get("cross_ref_targets") or []),
    )


# ---------------------------------------------------------------------------
# TreeNode / DocTree <-> dict (pure JSONB storage)
# ---------------------------------------------------------------------------


def tree_to_dict(t: DocTree) -> dict[str, Any]:
    return {
        "doc_id": t.doc_id,
        "parse_version": t.parse_version,
        "root_id": t.root_id,
        "quality_score": t.quality_score,
        "generation_method": t.generation_method,
        "nodes": {nid: _node_to_dict(n) for nid, n in t.nodes.items()},
    }


def tree_from_dict(d: dict[str, Any]) -> DocTree:
    nodes = {nid: _node_from_dict(nd) for nid, nd in d["nodes"].items()}
    return DocTree(
        doc_id=d["doc_id"],
        parse_version=d["parse_version"],
        root_id=d["root_id"],
        nodes=nodes,
        quality_score=d["quality_score"],
        generation_method=d["generation_method"],
    )


def _node_to_dict(n: TreeNode) -> dict[str, Any]:
    return {
        "node_id": n.node_id,
        "doc_id": n.doc_id,
        "parse_version": n.parse_version,
        "parent_id": n.parent_id,
        "level": n.level,
        "title": n.title,
        "page_start": n.page_start,
        "page_end": n.page_end,
        "block_ids": list(n.block_ids),
        "children": list(n.children),
        "element_types": list(n.element_types),
        "table_count": n.table_count,
        "figure_count": n.figure_count,
        "content_hash": n.content_hash,
        "summary": n.summary,
        "key_entities": list(n.key_entities),
        "cross_reference_targets": list(n.cross_reference_targets),
    }


def _node_from_dict(d: dict[str, Any]) -> TreeNode:
    return TreeNode(
        node_id=d["node_id"],
        doc_id=d["doc_id"],
        parse_version=d["parse_version"],
        parent_id=d.get("parent_id"),
        level=d["level"],
        title=d["title"],
        page_start=d["page_start"],
        page_end=d["page_end"],
        block_ids=list(d.get("block_ids") or []),
        children=list(d.get("children") or []),
        element_types=list(d.get("element_types") or []),
        table_count=d.get("table_count", 0),
        figure_count=d.get("figure_count", 0),
        content_hash=d.get("content_hash", ""),
        summary=d.get("summary"),
        key_entities=list(d.get("key_entities") or []),
        cross_reference_targets=list(d.get("cross_reference_targets") or []),
    )


# ---------------------------------------------------------------------------
# Chunk <-> row
# ---------------------------------------------------------------------------


def chunk_to_row(c: Chunk) -> dict[str, Any]:
    return {
        "chunk_id": c.chunk_id,
        "doc_id": c.doc_id,
        "parse_version": c.parse_version,
        "node_id": c.node_id,
        "content": c.content,
        "content_type": c.content_type,
        "block_ids": list(c.block_ids),
        "page_start": c.page_start,
        "page_end": c.page_end,
        "token_count": c.token_count,
        "y_sort": c.y_sort,
        "section_path": list(c.section_path),
        "ancestor_node_ids": list(c.ancestor_node_ids),
        "cross_ref_chunk_ids": list(c.cross_ref_chunk_ids),
    }


def row_to_chunk(r: dict[str, Any]) -> Chunk:
    return Chunk(
        chunk_id=r["chunk_id"],
        doc_id=r["doc_id"],
        parse_version=r["parse_version"],
        node_id=r["node_id"],
        content=r["content"],
        content_type=r["content_type"],
        block_ids=list(r.get("block_ids") or []),
        page_start=r["page_start"],
        page_end=r["page_end"],
        token_count=r["token_count"],
        y_sort=r.get("y_sort", 0.0),
        section_path=list(r.get("section_path") or []),
        ancestor_node_ids=list(r.get("ancestor_node_ids") or []),
        cross_ref_chunk_ids=list(r.get("cross_ref_chunk_ids") or []),
    )


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------


def page_to_row(p: Page, doc_id: str, parse_version: int) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "parse_version": parse_version,
        "page_no": p.page_no,
        "width": p.width,
        "height": p.height,
    }
