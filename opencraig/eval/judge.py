"""
LLM-as-judge scorer — optional, uses the caller's own LLM via LiteLLM.

This is *opt-in*: OpenCraig's CI doesn't run it (judge calls are expensive
and deterministic scoring is never guaranteed). Use it locally against
your fixture corpus when comparing pipeline variants.

Two scorers, matching the common RAG-eval taxonomy:

    * faithfulness(answer, citations) — is the answer grounded in cited text?
    * context_precision(answer, citations) — are cited snippets actually used?

``LLMJudge`` wraps an explicit model + api_base + api_key; it calls
LiteLLM with a strict JSON-output prompt and parses {score, reason}.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


_FAITHFULNESS_SYSTEM = (
    "You are a strict RAG evaluator. Read the ANSWER and the SOURCE SNIPPETS "
    "it cites. Decide whether every claim in the answer is supported by the "
    'snippets. Respond ONLY with JSON: {"score": <0.0-1.0>, "reason": "<1 sentence>"}. '
    "1.0 = every claim directly supported. 0.0 = answer contradicts or "
    "invents content not in any snippet."
)

_CONTEXT_PRECISION_SYSTEM = (
    "You are a RAG evaluator. Given the ANSWER and each SOURCE SNIPPET, "
    "judge whether each snippet was actually useful for the answer. "
    'Respond ONLY with JSON: {"score": <fraction-of-useful-snippets>, '
    '"reason": "<1 sentence>"}.'
)


@dataclass
class JudgeResult:
    score: float
    reason: str = ""
    raw: str = ""


@dataclass
class LLMJudge:
    """
    Args:
        model:      LiteLLM model string, e.g. ``"openai/gpt-4o-mini"``.
        api_base:   Endpoint (optional if env-default works).
        api_key:    Plaintext key (dev only).
        api_key_env: Env-var name to read the key from.
        temperature: Judge determinism — 0.0 is the sensible default.
        timeout:    Per-call seconds.
    """

    model: str
    api_base: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    timeout: float = 30.0

    def __post_init__(self):
        if not self.api_key and self.api_key_env:
            self.api_key = os.environ.get(self.api_key_env)

    # ── Scorers ────────────────────────────────────────────────────────

    def faithfulness(self, answer: str, citations: list[str | dict]) -> JudgeResult:
        """``citations`` items can be plain snippet strings or dicts with
        a ``snippet`` / ``text`` field."""
        snippets = _normalise_snippets(citations)
        return self._score(
            system=_FAITHFULNESS_SYSTEM,
            user=(
                f"ANSWER:\n{answer}\n\n"
                "SOURCE SNIPPETS:\n" + "\n---\n".join(f"[{i}] {s}" for i, s in enumerate(snippets, 1))
            ),
        )

    def context_precision(self, answer: str, citations: list[str | dict]) -> JudgeResult:
        snippets = _normalise_snippets(citations)
        return self._score(
            system=_CONTEXT_PRECISION_SYSTEM,
            user=(
                f"ANSWER:\n{answer}\n\n"
                "SOURCE SNIPPETS:\n" + "\n---\n".join(f"[{i}] {s}" for i, s in enumerate(snippets, 1))
            ),
        )

    # ── Core LLM call ──────────────────────────────────────────────────

    def _score(self, system: str, user: str) -> JudgeResult:
        import litellm

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "timeout": self.timeout,
            # Thinking-disabled invariant: judges score short JSON
            # outputs; CoT tokens just clip the budget and risk
            # truncating the score (see benchmark/metrics.py for the
            # original bug report). Same flag every other LLM call
            # in OpenCraig uses.
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        resp = litellm.completion(**kwargs)
        try:
            text = resp.choices[0].message.content or ""
        except Exception:
            text = ""
        return _parse_score(text)


def _normalise_snippets(items: list[Any]) -> list[str]:
    out: list[str] = []
    for x in items or []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            out.append(str(x.get("snippet") or x.get("text") or ""))
        else:
            out.append(str(getattr(x, "snippet", None) or getattr(x, "text", "") or ""))
    return out


def _parse_score(text: str) -> JudgeResult:
    # Grab the first balanced {...} — models sometimes leak chatter.
    try:
        i = text.index("{")
        j = text.rindex("}") + 1
        d = json.loads(text[i:j])
        return JudgeResult(
            score=float(d.get("score", 0.0)),
            reason=str(d.get("reason", "")).strip(),
            raw=text,
        )
    except Exception as e:
        log.warning("LLMJudge parse failed: %s — raw=%r", e, text[:200])
        return JudgeResult(score=0.0, reason="parse_failed", raw=text)
