"""
LLM-as-judge scoring for benchmark items.

Evaluates three RAGAS-style metrics via direct LiteLLM calls
(no RAGAS / LangChain dependency):

    1. Faithfulness  — answer grounded in retrieved context?
    2. Relevancy     — answer addresses the question?
    3. Context Precision — retrieved chunks relevant to the question?

Each metric returns a float 0-1.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable

from config.auth import resolve_api_key

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_FAITHFULNESS_PROMPT = """\
You are evaluating the faithfulness of an answer.

Question: {question}

Context (retrieved passages):
{context}

Answer: {answer}

Does the answer ONLY use information present in the context above?
Score from 0.0 to 1.0:
- 1.0 = every claim in the answer is supported by the context
- 0.5 = some claims are supported, some are not
- 0.0 = the answer fabricates information not in the context

Respond with ONLY a JSON object: {{"score": <float>, "reason": "<brief explanation>"}}
"""

_RELEVANCY_PROMPT = """\
You are evaluating the relevancy of an answer.

Question: {question}
Answer: {answer}

Does the answer actually address the question asked?
Score from 0.0 to 1.0:
- 1.0 = the answer directly and completely addresses the question
- 0.5 = the answer partially addresses the question
- 0.0 = the answer is irrelevant or off-topic

Respond with ONLY a JSON object: {{"score": <float>, "reason": "<brief explanation>"}}
"""

_CONTEXT_PRECISION_PROMPT = """\
You are evaluating context precision for a retrieval system.

Question: {question}
Ground truth answer: {ground_truth}

Retrieved passages:
{context}

Are the retrieved passages relevant to answering the question?
Score from 0.0 to 1.0:
- 1.0 = all passages contain information needed to answer the question
- 0.5 = some passages are relevant, some are noise
- 0.0 = none of the passages are relevant

Respond with ONLY a JSON object: {{"score": <float>, "reason": "<brief explanation>"}}
"""


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_items(
    *,
    items: list,
    cfg,
    cancel: threading.Event | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
):
    """Score each BenchmarkItem in-place with three metrics.

    Prefer the dedicated benchmark judge LLM (cfg.benchmark) if a
    separate judge provider was configured — this avoids the model
    scoring its own answers (self-preference bias). Falls back to the
    answer generator with a log warning so users know the scores may
    be biased.
    """
    bench_cfg = getattr(cfg, "benchmark", None)
    gen_cfg = cfg.answering.generator
    # An independent judge is configured when benchmark.model is non-empty
    # AND it isn't the same model as the generator. Anything else falls
    # back to the generator with a self-preference-bias warning.
    use_independent_judge = bool(bench_cfg and bench_cfg.model and bench_cfg.model != gen_cfg.model)

    if use_independent_judge:
        model = bench_cfg.model
        api_key = resolve_api_key(
            api_key=bench_cfg.api_key,
            api_key_env=bench_cfg.api_key_env,
            context="benchmark_scoring",
            required=False,
        )
        api_base = bench_cfg.api_base
        log.info("benchmark: using independent judge model=%s", model)
    else:
        model = gen_cfg.model
        api_key = resolve_api_key(
            api_key=gen_cfg.api_key,
            api_key_env=gen_cfg.api_key_env,
            context="benchmark_scoring",
        )
        api_base = gen_cfg.api_base
        log.warning(
            "benchmark: no independent judge configured — reusing the Answer LLM "
            "for scoring, which introduces self-preference bias. Set benchmark.model "
            "(and api_base / api_key_env) to a different model for rigorous scoring."
        )

    import litellm

    total = len(items)
    for i, item in enumerate(items):
        if cancel and cancel.is_set():
            return
        if item.error or not item.answer:
            if progress_cb:
                progress_cb(i + 1, total)
            continue

        # Pack context lines into an 8k-char budget. Under the old
        # `contexts[:10]` + `context[:5000]` combo, the first 10 slots
        # were all raw chunk snippets and the appended KG synthesized
        # context (entities / relations) never reached the judge —
        # faithfulness / context_precision were under-reported for
        # answers that cited KG material.
        #
        # item.contexts is structured as [chunks..., KG lines...], so
        # a char-budget scan preserves that priority: chunks come
        # first, KG lines fill whatever budget remains.
        _JUDGE_CTX_BUDGET = 8000
        _parts: list[str] = []
        _used = 0
        for _line in item.contexts or []:
            ln = len(_line)
            if _used + ln + 4 > _JUDGE_CTX_BUDGET:  # +4 accounts for the "\n---\n" separator
                break
            _parts.append(_line)
            _used += ln + 4
        context_str = "\n---\n".join(_parts) if _parts else "(no context retrieved)"

        # Run three scoring prompts
        try:
            item.faithfulness = _call_judge(
                litellm,
                model,
                api_key,
                api_base,
                _FAITHFULNESS_PROMPT.format(
                    question=item.question,
                    context=context_str,
                    answer=item.answer[:2000],
                ),
            )
        except Exception as e:
            log.warning("faithfulness scoring failed for item %d: %s", i, e)
            item.faithfulness = None

        try:
            item.relevancy = _call_judge(
                litellm,
                model,
                api_key,
                api_base,
                _RELEVANCY_PROMPT.format(
                    question=item.question,
                    answer=item.answer[:2000],
                ),
            )
        except Exception as e:
            log.warning("relevancy scoring failed for item %d: %s", i, e)
            item.relevancy = None

        try:
            item.context_precision = _call_judge(
                litellm,
                model,
                api_key,
                api_base,
                _CONTEXT_PRECISION_PROMPT.format(
                    question=item.question,
                    ground_truth=item.ground_truth[:1000],
                    context=context_str,
                ),
            )
        except Exception as e:
            log.warning("context_precision scoring failed for item %d: %s", i, e)
            item.context_precision = None

        if progress_cb:
            progress_cb(i + 1, total)


def _call_judge(
    litellm,
    model: str,
    api_key: str | None,
    api_base: str | None,
    prompt: str,
) -> float:
    """Call LLM judge and extract score.

    Two important quirks driving the kwargs below:

      * Thinking-mode models (DeepSeek V4-Pro, OpenAI o1, etc.)
        emit CoT tokens that count against ``max_tokens``. The old
        cap of 200 was too tight — for the Faithfulness / CP
        prompts (which embed up to 8KB of context), the model
        would burn the budget on thinking and never reach the
        ``{"score": ...}`` JSON, falling through to the 0.0
        ``_extract_score`` fallback. Net effect: F + CP reported
        ~0 across the board even when the answer was correct.
        Bumped to 500 so the JSON has room.
      * ``extra_body={"thinking": {"type": "disabled"}}`` tells
        Anthropic / DeepSeek to skip CoT entirely. Saves tokens
        and removes the truncation risk altogether. Same flag the
        agent loop + query understanding use.
    """
    kwargs = dict(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=500,
        timeout=30.0,
        extra_body={"thinking": {"type": "disabled"}},
    )
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base

    resp = litellm.completion(**kwargs)
    text = resp.choices[0].message.content or ""
    return _extract_score(text)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_score(text: str) -> float:
    m = _JSON_RE.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
            score = float(data.get("score", 0))
            return max(0.0, min(1.0, score))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    # Fallback: try to find a bare number
    nums = re.findall(r"(\d+\.?\d*)", text)
    for n in nums:
        v = float(n)
        if 0 <= v <= 1:
            return v
    return 0.0
