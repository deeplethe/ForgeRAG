"""
Query translation for the Search page.

Pre-pass before BM25: detect the source language of the user's
query, call a small LLM to render it in the other supported
language(s), and return the union for BM25 expansion. Lets
``"蜜蜂"`` recall English passages mentioning bees (and vice
versa) while keeping BM25's matched-token highlighting — vector
search would have given cross-lingual recall too, but at the cost
of losing keyword highlights and the file-as-primary-row UX.

Hot-path knobs:

  * ``thinking`` is hard-disabled in the LLM call. Translation is
    a one-line task; reasoning models would burn tokens on
    "thinking" they don't need. Same flag the agent uses
    (``api/agent/llm.py:146``).
  * Translations are LRU-cached in-process keyed by (query,
    target_lang). Human search reuses the same query a lot
    (page refresh, language toggle, accidental Enter); the
    cache turns the second-and-later searches into ~0ms.
  * On any LLM error / timeout, we fall back to the original
    query alone — BM25 still runs, just without expansion. The
    Search page degrades gracefully rather than 500-ing.

Language detection is intentionally cheap and heuristic: count
CJK code-point characters; if any present, treat as Chinese,
otherwise English. Mixed-language queries are treated as
Chinese (the heavier signal). For more languages, swap in a
proper detector — the public API stays the same.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import OrderedDict
from typing import Iterable

from config.search import TranslationConfig

log = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[一-鿿]")


def detect_language(text: str) -> str:
    """Return ISO 639-1 code for the dominant script in ``text``.

    Heuristic only — good enough for the en/zh corpora the project
    targets today. For a third language, replace with a proper
    detector (e.g. fasttext lid.176, langdetect, lingua).
    """
    if not text:
        return "en"
    if _CJK_RE.search(text):
        return "zh"
    return "en"


# Per-target-language prompt. Kept short on purpose — long
# instructions inflate token cost and risk the model adding
# explanation paragraphs we'd then have to strip. The "no
# explanation" guardrail is the only thing that matters.
_PROMPT_BY_LANG = {
    "en": (
        "Translate the following search query to English. "
        "Output only the translation, no explanation, no quotes, "
        "no extra words. Preserve names and acronyms verbatim."
    ),
    "zh": (
        "把以下搜索查询翻译成中文。只输出译文，不要解释、不要引号、不要多余的字。"
        "人名、专有名词、缩写保持原样。"
    ),
}


class QueryTranslator:
    """Single instance per process. Lazy LiteLLM completion +
    threadsafe LRU cache. Reuses ``litellm`` the same way
    ``api/agent/llm.py`` does — no extra dependency.

    Public API is one method: ``expand(query) -> list[str]``,
    returning the original query plus its translations into the
    configured target_languages (minus the detected source). The
    caller usually space-joins the list to produce an expanded
    BM25 query string.
    """

    def __init__(self, cfg: TranslationConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._litellm = None  # lazy import — same dance as agent llm.py

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def expand(self, query: str) -> list[str]:
        """Return [original, *translations]. On any failure, returns
        just [original] so the caller can fall back transparently."""
        q = (query or "").strip()
        if not q or not self.cfg.enabled:
            return [q] if q else []

        src_lang = detect_language(q)
        targets = [lc for lc in self.cfg.target_languages if lc != src_lang]
        if not targets:
            return [q]

        out: list[str] = [q]
        for lc in targets:
            try:
                t = self._translate_cached(q, lc)
                if t and t.strip() and t.strip() != q:
                    out.append(t.strip())
            except Exception as e:
                log.warning(
                    "translation %s -> %s failed: %s — falling back to original only",
                    src_lang, lc, e,
                )
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _translate_cached(self, query: str, target_lang: str) -> str:
        key = (query, target_lang)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                # LRU: move to end on hit so the most-recently-used
                # entries survive eviction.
                self._cache.move_to_end(key)
                return hit

        translated = self._call_llm(query, target_lang)

        with self._lock:
            self._cache[key] = translated
            # Cap the cache. Drop the least-recently-used.
            while len(self._cache) > self.cfg.cache_size:
                self._cache.popitem(last=False)
        return translated

    def _ensure_litellm(self):
        if self._litellm is not None:
            return self._litellm
        import litellm  # type: ignore[import-not-found]

        # Drop unsupported params (e.g. ``extra_body`` on providers
        # that don't accept it) silently — same setup the agent
        # uses. Without this, every non-Anthropic provider would
        # 400 on the thinking-disable flag.
        litellm.drop_params = True
        self._litellm = litellm
        return self._litellm

    def _call_llm(self, query: str, target_lang: str) -> str:
        litellm = self._ensure_litellm()

        api_key = self.cfg.api_key
        if not api_key and self.cfg.api_key_env:
            api_key = os.environ.get(self.cfg.api_key_env)

        prompt = _PROMPT_BY_LANG.get(target_lang) or (
            f"Translate the following search query to {target_lang}. "
            f"Output only the translation, no explanation."
        )
        kwargs = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": query},
            ],
            "temperature": 0.0,
            "max_tokens": 128,
            "timeout": self.cfg.timeout,
            # See module docstring + api/agent/llm.py:146 — keep
            # reasoning models out of the loop for this one-line
            # task.
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if api_key:
            kwargs["api_key"] = api_key
        if self.cfg.api_base:
            kwargs["api_base"] = self.cfg.api_base

        resp = litellm.completion(**kwargs)
        return _extract_text(resp)


def _extract_text(resp) -> str:
    """Pull the assistant content from a LiteLLM response. Robust
    against providers that omit the content field — returns empty
    string instead of raising, so the caller falls back to the
    original query."""
    try:
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return ""
        msg = getattr(choices[0], "message", None)
        if msg is None:
            return ""
        text = getattr(msg, "content", None)
        return (text or "").strip()
    except Exception:
        return ""


def join_for_bm25(parts: Iterable[str]) -> str:
    """Concatenate the original + translated query variants into
    one string for BM25 ingestion. Space-joining is enough — the
    BM25 tokenizer (``[a-z0-9]+|[\\u4e00-\\u9fff]``) already splits
    on whitespace and treats ASCII / CJK independently, so each
    variant contributes its own tokens to the bag without
    collision. De-duplicates surface forms so the same query
    sent twice doesn't double-weight tokens."""
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        s = (p or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return " ".join(out)
