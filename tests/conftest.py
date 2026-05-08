"""
Pytest configuration.

Adds the project root to sys.path so `import parser`, `import config`
work without installing the package. Also provides a pytest fixture
`sample_pdf` that synthesizes a small multi-page PDF on the fly using
PyMuPDF -- real enough to exercise probe / pymupdf-backend / normalizer
without committing a binary fixture to the repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _restore_bm25_in_registry():
    """Re-register ``search_bm25`` in the agent's TOOL_REGISTRY for
    the duration of each test.

    Production omits the BM25 spec from the registry: the build
    path produces an empty index on the live corpus and the
    char-level CJK tokenizer makes Chinese keyword search useless
    in practice. Removing it from the registry stops the LLM
    burning iterations on a tool that always returns 0 hits.

    Tests, however, still exercise the dispatcher's BM25 path —
    scope filters, citation-pool seeding, error handling — because
    the underlying handler + spec are intentionally retained for
    future reactivation. This fixture re-registers BM25 so those
    tests' ``dispatch("search_bm25", ...)`` calls don't bounce off
    "unknown tool".
    """
    try:
        from api.agent.tools import TOOL_REGISTRY, _BM25_SPEC
    except Exception:
        yield
        return
    had_bm25 = _BM25_SPEC.name in TOOL_REGISTRY
    if not had_bm25:
        TOOL_REGISTRY[_BM25_SPEC.name] = _BM25_SPEC
    yield
    if not had_bm25:
        TOOL_REGISTRY.pop(_BM25_SPEC.name, None)


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory) -> Path:
    """
    Build a 4-page PDF with:
        page 1: title (big font), body paragraph, running header, page num footer
        page 2: heading (medium font), two-paragraph body ending mid-sentence,
                same header + footer
        page 3: continuation (starts lowercase), a "Figure 1" caption,
                same header + footer
        page 4: heading + body
    """
    fitz = pytest.importorskip("fitz")

    out = tmp_path_factory.mktemp("pdf") / "sample.pdf"
    doc = fitz.open()

    header_text = "OpenCraig Test Document"
    for i in range(4):
        page = doc.new_page(width=595, height=842)  # A4 portrait
        # Header (repeats on every page, near top)
        page.insert_text((72, 40), header_text, fontsize=9)
        # Footer page number
        page.insert_text((280, 810), f"Page {i + 1}", fontsize=9)

    # Page 1: title + body
    p0 = doc[0]
    p0.insert_text((72, 120), "Introduction", fontsize=22)  # heading
    p0.insert_text(
        (72, 180),
        "This document is used by the OpenCraig parser test suite.",
        fontsize=11,
    )

    # Page 2: heading + body that ends mid-sentence (no terminal punct)
    p1 = doc[1]
    p1.insert_text((72, 120), "Methods", fontsize=16)  # heading
    p1.insert_text(
        (72, 180),
        "The parsing pipeline first probes the document and then",
        fontsize=11,
    )

    # Page 3: continuation starting lowercase + figure caption
    p2 = doc[2]
    p2.insert_text(
        (72, 120),
        "routes it to the most suitable backend available.",
        fontsize=11,
    )
    p2.insert_text((72, 200), "Figure 1: Overall architecture", fontsize=11)

    # Page 4: heading + body
    p3 = doc[3]
    p3.insert_text((72, 120), "Results", fontsize=16)
    p3.insert_text((72, 180), "All backends passed the smoke test.", fontsize=11)

    # Embed a TOC
    doc.set_toc(
        [
            [1, "Introduction", 1],
            [1, "Methods", 2],
            [1, "Results", 4],
        ]
    )

    doc.save(out)
    doc.close()
    return out
