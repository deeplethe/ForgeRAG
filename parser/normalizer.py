r"""
Post-parse normalizer.

Runs on every ParsedDocument regardless of which backend produced
it. Behavior is hardcoded (per the design decision to keep the
early-stage code simple); only on/off switches are exposed via
NormalizeConfig.

Rules, all non-destructive
--------------------------
1. Header/footer detection -- blocks that repeat across >=40% of
   pages at similar y-coordinates are marked excluded=True with
   reason "header" or "footer". They are NOT deleted, so citations
   that happen to land on them still resolve.

2. Cross-page paragraph merge -- if page N's last reading block
   ends without sentence-terminating punctuation and page N+1's
   first reading block starts with a lowercase letter (or Chinese
   non-heading character), merge the text into the earlier block
   and mark the later block excluded with reason "merged_into".
   The merged block's bbox is left on its original page; the
   other block keeps its bbox so highlight can still flash both
   regions via its caption_of pointer.

3. Caption binding -- text blocks matching /^(figure|fig\.?|table|
   图|表)\s*\d+/i are marked as captions and linked to the nearest
   preceding figure/table block on the same page via caption_of.

Order matters: header/footer first (so they are excluded before
paragraph merging looks at "last reading block"), then paragraph
merge, then caption binding, then inline reference resolution.

Reference resolution (step 4) scans captions and figure/table
blocks to build a label index like {"figure 1": block_id, "图 3": ...},
then scans paragraph text for patterns like "see Figure 3" or
"如表 2 所示" and records the matched target block_ids in the
source block's `cross_ref_targets`. This lets the retrieval merge
step expand citations along cross-reference edges without doing
any NLP at query time.
"""

from __future__ import annotations

import re
from collections import defaultdict

from config import NormalizeConfig

from .schema import Block, BlockType, ParsedDocument

# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def normalize(doc: ParsedDocument, cfg: NormalizeConfig) -> ParsedDocument:
    if cfg.strip_header_footer:
        _mark_header_footer(doc)
    if cfg.merge_cross_page_paragraphs:
        _merge_cross_page_paragraphs(doc)
    if cfg.bind_captions:
        _bind_captions(doc)
    if cfg.resolve_references:
        _resolve_inline_references(doc)
    return doc


# ---------------------------------------------------------------------------
# 1. Header / footer
# ---------------------------------------------------------------------------


_HF_MIN_PAGE_RATIO = 0.4  # must appear on >=40% of pages
_HF_Y_TOLERANCE = 8.0  # pt
_HF_TEXT_MAX_LEN = 120  # long lines are never headers/footers


def _mark_header_footer(doc: ParsedDocument) -> None:
    if len(doc.pages) < 3:
        # Too few pages -- recurrence analysis is noisy
        return

    # Group blocks by (normalized_text, rounded_y_band)
    # We use distance from top for headers, from bottom for footers,
    # rounded to HF_Y_TOLERANCE buckets.
    page_heights = {p.page_no: p.height for p in doc.pages}
    header_groups: dict[tuple[str, int], list[Block]] = defaultdict(list)
    footer_groups: dict[tuple[str, int], list[Block]] = defaultdict(list)

    for b in doc.blocks:
        if b.type not in (BlockType.PARAGRAPH, BlockType.HEADING):
            continue
        text = _norm_hf_text(b.text)
        if not text or len(b.text) > _HF_TEXT_MAX_LEN:
            continue
        page_h = page_heights.get(b.page_no)
        if not page_h:
            continue
        y0, _, _, y1 = b.bbox[1], b.bbox[0], b.bbox[2], b.bbox[3]
        # distance from top of page (PDF coords: larger y = closer to top)
        dist_from_top = page_h - y1
        dist_from_bottom = y0
        if dist_from_top < page_h * 0.12:
            key = (text, int(dist_from_top // _HF_Y_TOLERANCE))
            header_groups[key].append(b)
        elif dist_from_bottom < page_h * 0.12:
            key = (text, int(dist_from_bottom // _HF_Y_TOLERANCE))
            footer_groups[key].append(b)

    n_pages = len(doc.pages)
    threshold = max(2, int(n_pages * _HF_MIN_PAGE_RATIO))

    for group, reason in ((header_groups, "header"), (footer_groups, "footer")):
        for blocks in group.values():
            if len({b.page_no for b in blocks}) >= threshold:
                for b in blocks:
                    b.excluded = True
                    b.excluded_reason = reason
                    # Also retype so downstream can tell at a glance
                    b.type = BlockType.HEADER if reason == "header" else BlockType.FOOTER


_HF_NORM_RE = re.compile(r"\s+")
_PAGE_NUM_RE = re.compile(r"\b\d+\b")


def _norm_hf_text(text: str) -> str:
    """Normalize text for header/footer recurrence matching."""
    t = _HF_NORM_RE.sub(" ", text.strip().lower())
    # Replace page numbers so "Page 3" and "Page 4" collapse
    t = _PAGE_NUM_RE.sub("#", t)
    return t


# ---------------------------------------------------------------------------
# 2. Cross-page paragraph merge
# ---------------------------------------------------------------------------


_SENTENCE_END = re.compile(r"[.!?。!?;;:：]\s*$")
_STARTS_WITH_LOWER = re.compile(r"^[a-z]")
_CJK_PUNCT_START = re.compile(r"^[,，、)）】」》\]]")


def _merge_cross_page_paragraphs(doc: ParsedDocument) -> None:
    # Get reading-order blocks (skip excluded) grouped by page
    per_page: dict[int, list[Block]] = defaultdict(list)
    for b in doc.blocks:
        if b.excluded:
            continue
        if b.type != BlockType.PARAGRAPH:
            continue
        per_page[b.page_no].append(b)

    sorted_pages = sorted(per_page.keys())
    for i in range(len(sorted_pages) - 1):
        cur_page = sorted_pages[i]
        next_page = sorted_pages[i + 1]
        if next_page != cur_page + 1:
            continue
        cur_blocks = per_page[cur_page]
        next_blocks = per_page[next_page]
        if not cur_blocks or not next_blocks:
            continue

        tail = cur_blocks[-1]
        head = next_blocks[0]
        if _SENTENCE_END.search(tail.text):
            continue
        if not (_STARTS_WITH_LOWER.match(head.text) or _CJK_PUNCT_START.match(head.text)):
            continue

        # Merge: append head.text to tail.text, mark head excluded
        tail.text = tail.text.rstrip() + " " + head.text.lstrip()
        head.excluded = True
        head.excluded_reason = "merged_into:" + tail.block_id


# ---------------------------------------------------------------------------
# 3. Caption binding
# ---------------------------------------------------------------------------


_CAPTION_RE = re.compile(r"^\s*(?:figure|fig\.?|table|图|表)\s*\d+", re.IGNORECASE)


def _bind_captions(doc: ParsedDocument) -> None:
    # Walk blocks per page in reading order.
    per_page: dict[int, list[Block]] = defaultdict(list)
    for b in doc.blocks:
        per_page[b.page_no].append(b)

    for _page_no, blocks in per_page.items():
        blocks.sort(key=lambda x: x.seq)
        last_media: Block | None = None
        for b in blocks:
            if b.type in (BlockType.IMAGE, BlockType.TABLE):
                last_media = b
                continue
            if b.excluded:
                continue
            if b.type == BlockType.PARAGRAPH and _CAPTION_RE.match(b.text):
                b.type = BlockType.CAPTION
                if last_media is not None:
                    b.caption_of = last_media.block_id
                    if last_media.type == BlockType.IMAGE and not last_media.image_caption:
                        last_media.image_caption = b.text


# ---------------------------------------------------------------------------
# 4. Inline reference resolution
# ---------------------------------------------------------------------------

# Label kind canonicalization -- English terms and CJK terms collapse
# onto the same bucket so "Figure 3" and "图 3" can share an entry if
# the doc happens to use both.
_LABEL_KIND_NORMALIZE = {
    "figure": "figure",
    "fig": "figure",
    "fig.": "figure",
    "图": "figure",
    "table": "table",
    "tbl": "table",
    "tbl.": "table",
    "表": "table",
    "equation": "equation",
    "eq": "equation",
    "eq.": "equation",
    "公式": "equation",
    "式": "equation",
    "section": "section",
    "sec": "section",
    "sec.": "section",
    "chapter": "section",
    "章节": "section",
    "节": "section",
    "章": "section",
}

# Pattern that extracts a caption's own label, e.g. "Figure 3: ..." or
# "图 3 系统架构". Used when building the label index.
_LABEL_DECLARE_RE = re.compile(
    r"^\s*(figure|fig\.?|table|tbl\.?|equation|eq\.?|图|表|公式|式)\s*"
    r"(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Patterns that match inline references in paragraph text.
# We intentionally keep the kind token broad; _LABEL_KIND_NORMALIZE
# canonicalizes it afterwards.
_REF_PATTERNS = [
    # English: "see Figure 3", "refer to Table 2", "(Figure 1)", "cf. Eq. 5"
    re.compile(
        r"(?:see|refer\s+to|cf\.?|as\s+shown\s+in|shown\s+in|in)?\s*"
        r"\(?\s*(figure|fig\.?|table|tbl\.?|equation|eq\.?)\s*"
        r"(\d+(?:\.\d+)?)\s*\)?",
        re.IGNORECASE,
    ),
    # Chinese: "如图 3 所示", "见表 2", "参见公式 5", "图 1"
    re.compile(
        r"(?:如|见|参见|详见|参考|参阅)?\s*"
        r"(图|表|公式|式)\s*(\d+(?:\.\d+)?)"
    ),
]


def _normalize_label_kind(raw: str) -> str | None:
    key = raw.strip().lower().rstrip(".")
    return _LABEL_KIND_NORMALIZE.get(key) or _LABEL_KIND_NORMALIZE.get(key + ".")


def _build_label_index(doc: ParsedDocument) -> dict[tuple[str, str], str]:
    """
    Build {(kind, number): block_id} from captions and directly-labeled
    figure/table blocks.

    Kind is canonicalized via _LABEL_KIND_NORMALIZE so "Figure"/"Fig"/"图"
    all land in the "figure" bucket. Number is kept verbatim as a string
    (supports "3" and "3.2").
    """
    index: dict[tuple[str, str], str] = {}

    for b in doc.blocks:
        # Captions explicitly declare their label in text
        if b.type == BlockType.CAPTION:
            m = _LABEL_DECLARE_RE.match(b.text)
            if m:
                kind = _normalize_label_kind(m.group(1))
                if kind:
                    target = b.caption_of or b.block_id
                    index.setdefault((kind, m.group(2)), target)
            continue

        # Images/tables may already have image_caption populated via
        # _bind_captions -- mine that too in case caption block is
        # excluded or missing.
        if b.type in (BlockType.IMAGE, BlockType.TABLE):
            cap = b.image_caption or ""
            m = _LABEL_DECLARE_RE.match(cap)
            if m:
                kind = _normalize_label_kind(m.group(1))
                if kind:
                    index.setdefault((kind, m.group(2)), b.block_id)

    return index


def _resolve_inline_references(doc: ParsedDocument) -> None:
    label_index = _build_label_index(doc)
    if not label_index:
        return

    for b in doc.blocks:
        if b.excluded:
            continue
        # Only scan running text blocks -- captions point at media, not
        # at other refs; headings are too short to bother.
        if b.type not in (BlockType.PARAGRAPH, BlockType.LIST):
            continue
        text = b.text
        if not text:
            continue

        found: list[str] = []
        for pat in _REF_PATTERNS:
            for m in pat.finditer(text):
                kind = _normalize_label_kind(m.group(1))
                if not kind:
                    continue
                target = label_index.get((kind, m.group(2)))
                if target and target != b.block_id and target not in found:
                    found.append(target)

        if found:
            # Merge with anything already present (idempotent re-runs)
            seen = set(b.cross_ref_targets)
            for t in found:
                if t not in seen:
                    b.cross_ref_targets.append(t)
                    seen.add(t)
