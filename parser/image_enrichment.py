"""
Image enrichment: VLM description + OCR in one pass.

For each figure block that has a `image_storage_key` (image stored
in BlobStore), ask a vision LLM to:

    1. Describe the image (what it shows, key findings)
    2. OCR any text in the image (axis labels, data values, legends)

The combined output replaces the figure block's `text` field so
it becomes searchable by all three retrieval paths (vector, BM25,
tree navigation). The block's `image_caption` is also updated
if the original caption was empty.

This is an optional async enrichment pass — same pattern as
summary enrichment. Run it after ingestion or as a batch job.

Usage:
    from parser.image_enrichment import enrich_images, make_vlm_fn
    vlm_fn = make_vlm_fn(model="openai/gpt-4o-mini", api_key="...", api_base="...")
    count, fails = enrich_images(doc, blob_store, vlm_fn=vlm_fn)
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from parser.blob_store import BlobStore
from parser.schema import Block, BlockType, ParsedDocument

log = logging.getLogger(__name__)


def enrich_images(
    doc: ParsedDocument,
    blob_store: BlobStore,
    *,
    vlm_fn: Callable[[bytes, str], str],
    skip_if_has_text: bool = True,
    max_failures: int = 3,
    max_workers: int = 4,
) -> tuple[int, int]:
    """
    Walk every figure block, call vlm_fn(image_bytes, prompt) -> text
    **in parallel**, store the result in block.text and block.image_caption.

    Returns (enriched_count, failure_count).
    """
    # ── Collect tasks ──
    tasks: list[tuple[Block, bytes]] = []
    for block in doc.blocks:
        if block.type != BlockType.IMAGE:
            continue
        if not block.image_storage_key:
            continue
        if skip_if_has_text and block.text and len(block.text) > 50:
            continue  # already has meaningful text
        try:
            img_bytes = blob_store.get(block.image_storage_key)
        except Exception:
            continue
        if len(img_bytes) < 200:
            continue  # too small to be meaningful
        tasks.append((block, img_bytes))

    if not tasks:
        return 0, 0

    log.info("image enrichment: %d figures to process (workers=%d)", len(tasks), max_workers)

    # ── Execute in parallel ──
    count = 0
    failures = 0

    def _process_one(block: Block, img_bytes: bytes) -> tuple[Block, str | None]:
        prompt = _build_vlm_prompt(block)
        try:
            desc = vlm_fn(img_bytes, prompt).strip()
            return block, desc
        except Exception as e:
            log.warning("image enrichment failed for %s: %s", block.block_id, e)
            return block, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_process_one, blk, img): blk for blk, img in tasks}
        consecutive_fails = 0
        for fut in as_completed(futures):
            block, description = fut.result()
            if description:
                block.text = description
                if not block.image_caption or len(block.image_caption) < 20:
                    block.image_caption = description[:300]
                count += 1
                consecutive_fails = 0
                log.debug("enriched image %s: %s", block.block_id, description[:80])
            else:
                failures += 1
                consecutive_fails += 1
                if consecutive_fails >= max_failures:
                    log.warning(
                        "aborting image enrichment after %d consecutive failures",
                        max_failures,
                    )
                    # Cancel remaining futures
                    for f in futures:
                        f.cancel()
                    break

    log.info("image enrichment done: %d enriched, %d failed", count, failures)
    return count, failures


def _build_vlm_prompt(block: Block) -> str:
    parts = [
        "Analyze this figure from a research paper. Provide:",
        "1. A detailed description of what the figure shows (chart type, data patterns, key findings)",
        "2. All text visible in the image (axis labels, legends, data values, annotations)",
        "3. The main takeaway or conclusion from this figure",
        "",
        "Be concise but comprehensive. Focus on factual content, not aesthetics.",
    ]
    if block.image_caption:
        parts.insert(0, f"Caption: {block.image_caption}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# VLM function builder
# ---------------------------------------------------------------------------


def make_vlm_fn(
    *,
    model: str = "openai/gpt-4o-mini",
    api_key: str | None = None,
    api_key_env: str | None = None,
    api_base: str | None = None,
) -> Callable[[bytes, str], str]:
    """
    Build a callable `(image_bytes, prompt) -> description` using
    litellm's vision API.
    """
    from config.auth import resolve_api_key

    key = resolve_api_key(api_key=api_key, api_key_env=api_key_env)

    def _call(image_bytes: bytes, prompt: str) -> str:
        from forgerag.llm_cache import cached_completion

        b64 = base64.b64encode(image_bytes).decode("ascii")
        # Detect mime from magic bytes
        mime = "image/png"
        if image_bytes[:2] == b"\xff\xd8":
            mime = "image/jpeg"
        elif image_bytes[:4] == b"RIFF":
            mime = "image/webp"

        kwargs: dict[str, Any] = dict(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{b64}",
                            },
                        },
                    ],
                }
            ],
            temperature=0.1,
            timeout=60,
        )
        if key:
            kwargs["api_key"] = key
        if api_base:
            kwargs["api_base"] = api_base

        resp = cached_completion(**kwargs)
        return resp.choices[0].message.content or ""

    return _call
