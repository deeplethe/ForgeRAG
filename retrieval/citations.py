"""
Citation builder: convert MergedChunks (after rerank/merge) into
Citation objects that the frontend PDF viewer can consume.

Each citation carries:
    - block_ids (for versioned regeneration / debugging)
    - highlights: [HighlightRect] with bbox in PDF points, bottom-left origin
    - snippet: truncated content for hover preview
    - open_url: rendered from the config template

The bboxes come from the parsed_blocks table. We batch-load all
needed blocks in a single relational call per invocation.
"""

from __future__ import annotations

from collections.abc import Iterable

from config import CitationsConfig
from parser.schema import Citation, HighlightRect
from persistence.serde import row_to_block
from persistence.store import Store as RelationalStore

from .types import MergedChunk


def build_citations(
    merged: list[MergedChunk],
    relational: RelationalStore,
    cfg: CitationsConfig,
) -> list[Citation]:
    if not merged:
        return []

    # 1. Gather every block_id referenced by the chunk set
    wanted_block_ids: list[str] = []
    seen: set[str] = set()
    for m in merged:
        if m.chunk is None:
            continue
        for bid in m.chunk.block_ids:
            if bid not in seen:
                wanted_block_ids.append(bid)
                seen.add(bid)

    # 2. Batch-load blocks
    block_rows = _load_blocks(relational, wanted_block_ids)
    blocks_by_id = {row["block_id"]: row_to_block(row) for row in block_rows}

    # 3. Resolve file_id per doc_id (cache to avoid repeated lookups)
    #    Prefer pdf_file_id (converted PDF) over file_id (original) so
    #    the frontend can render + highlight the document correctly.
    #    Also track the original source file_id for download.
    doc_to_file: dict[str, tuple[str | None, str | None, str]] = {}
    # value: (view_file_id, source_file_id_if_different, source_format)

    def _resolve_doc_files(doc_id: str) -> tuple[str | None, str | None, str]:
        if doc_id not in doc_to_file:
            row = relational.get_document(doc_id)
            if row:
                pdf_fid = row.get("pdf_file_id")
                orig_fid = row.get("file_id")
                fmt = row.get("format", "")
                if pdf_fid:
                    # Converted: view uses PDF, source is original
                    doc_to_file[doc_id] = (pdf_fid, orig_fid, fmt)
                else:
                    # Native PDF: view uses original, no separate source
                    doc_to_file[doc_id] = (orig_fid, None, fmt)
            else:
                doc_to_file[doc_id] = (None, None, "")
        return doc_to_file[doc_id]

    # 4. Build citations in merged order
    out: list[Citation] = []
    for i, m in enumerate(merged):
        c = m.chunk
        if c is None:
            continue
        highlights: list[HighlightRect] = []
        for bid in c.block_ids:
            b = blocks_by_id.get(bid)
            if b is None:
                continue
            highlights.append(HighlightRect(page_no=b.page_no, bbox=b.bbox))
        if not highlights:
            continue

        citation_id = f"c_{i + 1}"
        snippet = _make_snippet(c.content, cfg.max_snippet_chars)
        open_url = cfg.open_url_template.format(
            doc_id=c.doc_id,
            page_no=highlights[0].page_no,
            citation_id=citation_id,
        )
        view_fid, source_fid, source_fmt = _resolve_doc_files(c.doc_id)
        out.append(
            Citation(
                citation_id=citation_id,
                chunk_id=m.chunk_id,
                doc_id=c.doc_id,
                parse_version=c.parse_version,
                block_ids=list(c.block_ids),
                page_no=highlights[0].page_no,
                highlights=highlights,
                snippet=snippet,
                score=m.rrf_score,
                file_id=view_fid,
                source_file_id=source_fid,
                source_format=source_fmt,
                open_url=open_url,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_blocks(relational: RelationalStore, block_ids: Iterable[str]) -> list[dict]:
    bid_list = list(block_ids)
    if not bid_list:
        return []
    return list(relational.get_blocks_by_ids(bid_list))


def _make_snippet(content: str, max_chars: int) -> str:
    text = (content or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
