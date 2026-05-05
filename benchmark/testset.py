"""
Test-set generation: read ingested document chunks and ask an LLM to
produce question + expected_answer pairs.

No RAGAS / LangChain dependency — uses LiteLLM directly.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
from collections.abc import Callable

from config.auth import resolve_api_key

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a QA test-set generator for a document retrieval system.
Given a passage from a document, generate exactly {n} diverse question-answer pairs that can ONLY be answered using the information in the passage.

Rules:
- Questions should be specific, factual, and answerable from the passage alone.
- Vary question types: what, why, how, comparison, numerical, definition.
- Answers should be concise (1-3 sentences) and directly supported by the passage.
- Respond with ONLY a JSON array (no markdown fences):
[
  {{"question": "...", "answer": "..."}},
  ...
]
"""


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


def generate_testset(
    *,
    store,
    cfg,
    num_questions: int = 30,
    cancel: threading.Event | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list:
    """Generate QA pairs from ingested document chunks."""
    from benchmark.runner import BenchmarkItem

    # Resolve LLM config — use answering generator's model
    gen_cfg = cfg.answering.generator
    model = gen_cfg.model
    api_key = resolve_api_key(
        api_key=gen_cfg.api_key,
        api_key_env=gen_cfg.api_key_env,
        context="benchmark_testset",
    )
    api_base = gen_cfg.api_base

    import litellm

    # Collect document chunks
    doc_ids = store.list_document_ids()
    if not doc_ids:
        raise RuntimeError("No documents ingested — cannot generate benchmark test set")

    # Gather all text chunks across documents
    all_chunks: list[dict] = []
    for doc_id in doc_ids:
        doc = store.get_document(doc_id)
        if not doc or doc.get("status") != "ready":
            continue
        pv = doc.get("active_parse_version", 1)
        chunks = store.get_chunks(doc_id, pv)
        text_chunks = [
            {**c, "_doc_id": doc_id, "_doc_title": doc.get("title", doc.get("filename", ""))}
            for c in chunks
            if c.get("content_type") == "text" and len(c.get("content", "")) > 100
        ]
        all_chunks.extend(text_chunks)

    if not all_chunks:
        raise RuntimeError("No text chunks found in ingested documents")

    # Sample chunks — pick enough to generate the requested number of questions.
    # Generate ~2 questions per chunk, so pick ceil(num_questions / 2) chunks.
    chunks_needed = min(len(all_chunks), max(1, (num_questions + 1) // 2))
    sampled = random.sample(all_chunks, chunks_needed)

    items: list[BenchmarkItem] = []
    questions_remaining = num_questions

    for i, chunk in enumerate(sampled):
        if cancel and cancel.is_set():
            break
        if questions_remaining <= 0:
            break

        n_for_chunk = min(3, max(1, questions_remaining))
        content = chunk.get("content", "")[:3000]  # cap to avoid token overflow

        try:
            kwargs = dict(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM.format(n=n_for_chunk)},
                    {"role": "user", "content": f"Passage:\n{content}"},
                ],
                temperature=0.7,
                max_tokens=1024,
                timeout=60.0,
                # Test-question generation is structured JSON output;
                # CoT just clips the budget and risks truncation.
                # Same flag every other LLM call in OpenCraig uses.
                extra_body={"thinking": {"type": "disabled"}},
            )
            if api_key:
                kwargs["api_key"] = api_key
            if api_base:
                kwargs["api_base"] = api_base

            resp = litellm.completion(**kwargs)
            text = resp.choices[0].message.content or ""
            pairs = _parse_pairs(text)

            for pair in pairs[:n_for_chunk]:
                items.append(
                    BenchmarkItem(
                        idx=len(items),
                        question=pair["question"],
                        ground_truth=pair["answer"],
                        doc_id=chunk["_doc_id"],
                        doc_title=chunk["_doc_title"],
                    )
                )
                questions_remaining -= 1
                if questions_remaining <= 0:
                    break

        except Exception as e:
            log.warning("testset generation failed for chunk %d: %s", i, e)

        if progress_cb:
            progress_cb(len(items), num_questions)

    if not items:
        raise RuntimeError("Failed to generate any test questions")

    if progress_cb:
        progress_cb(len(items), len(items))

    log.info("generated %d test questions from %d chunks", len(items), len(sampled))
    return items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_pairs(text: str) -> list[dict]:
    """Parse LLM response into list of {question, answer}."""
    m = _JSON_RE.search(text)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [
        {"question": str(d.get("question", "")), "answer": str(d.get("answer", ""))}
        for d in data
        if isinstance(d, dict) and d.get("question")
    ]
