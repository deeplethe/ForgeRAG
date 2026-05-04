"""LLM-driven compaction of entity / relation description fragments.

Borrows the strategy from LightRAG's ``_handle_entity_relation_summary``:
descriptions accumulate as a newline-joined list during ingest (each new
chunk that mentions an entity contributes a fragment). When the list
exceeds a configurable token / count threshold, the LLM summarises all
fragments into a single canonical paragraph. Map-reduce + recursion
handle the case where the fragment list is too big to fit in one LLM
call.

Why this lives outside the GraphStore:
  - The store mutates under a process-wide lock; awaiting an LLM call
    inside it would block concurrent ingests for seconds.
  - Re-embedding the new description after summary needs the embedder,
    which the store doesn't know about.
  - Backfill scripts and the in-line ingest path need the same logic;
    keeping it here lets both call ``summarize_descriptions`` directly.

Triggering policy (``needs_summary``):
  - Token total of all fragments ≥ ``trigger_tokens`` (default 1200), OR
  - Fragment count ≥ ``force_on_count`` (default 8).

Either condition is sufficient. Token-based catches "one entity gets
mentioned in 50 chunks" growth; count-based catches "lots of small
fragments adding up faster than the token check trips".

The summarize prompt is adapted near-verbatim from LightRAG's
``summarize_entity_descriptions``: same role, same instructions, same
JSONL input format, same conflict-handling guidance for ambiguous-name
entities. ``{language}`` is set to "the original language of the input
descriptions" by default — the LLM follows the input rather than
forcing a specific language. Configurable via ``cfg.language``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# Reuse the project's existing token estimator so summarisation
# thresholds line up with how chunker / context-builder count tokens
# elsewhere. ``approx_tokens`` is a char-based heuristic — we don't
# need tiktoken-level precision for this layer; rough is fine for
# deciding "is this big enough to compress yet?".
from parser.chunker import approx_tokens

# ---------------------------------------------------------------------------
# Prompt — adapted from LightRAG's ``summarize_entity_descriptions``
# ---------------------------------------------------------------------------


PROMPT_SYSTEM = "You are a Knowledge Graph Specialist, proficient in data curation and synthesis."

# Default prompt — used for ``kind="entity"`` and ``kind="relation"``.
# Originally adapted from LightRAG's ``summarize_entity_descriptions``.
PROMPT_USER_ENTITY = """\
---Task---
Your task is to synthesize a list of descriptions of a given {kind} into a single, comprehensive, and cohesive summary.

---Instructions---
1. Input Format: The description list is provided in JSONL — each line is a JSON object with a single ``description`` field.
2. Output Format: Plain text, multiple paragraphs allowed. NO additional formatting, NO comments before or after the summary.
3. Comprehensiveness: The summary must integrate all key information from EVERY provided description. Do not omit important facts or details.
4. Context: Write the summary from an objective, third-person perspective. Explicitly mention the {kind} name at the beginning for clarity.
5. Conflict Handling:
   - If conflicting descriptions arise from multiple distinct entities/relations sharing the same name, summarise each one SEPARATELY within the output.
   - For conflicts within a single entity/relation (e.g. historical discrepancies), reconcile them or present both viewpoints with noted uncertainty.
6. Length Constraint: The summary's total length must not exceed {max_tokens} tokens while still maintaining depth and completeness.
7. Language: {language}. Proper nouns (personal names, place names, organisation names) retain their original form when no widely accepted translation exists.

---Input---
{kind} Name: {name}

Description List:
{json_list}

---Output---
"""

# Table-specific prompt — used for ``kind="table"``. Tables differ
# from entities in two ways:
#   1. The "fragments" are row-group markdown tables (each fragment
#      is itself a valid markdown sub-table), not free-form
#      descriptions. The LLM has to read and interpret data, not
#      reconcile descriptions.
#   2. We want a structured-style description (what the table is
#      about, what columns it has, notable patterns, scale) rather
#      than narrative reconciliation.
# The map-reduce machinery is the same — single-pass for fits,
# row-group split + reduce for big tables.
PROMPT_USER_TABLE = """\
---Task---
You will be given excerpts of a spreadsheet, possibly split into row groups when the table is too large for one pass. Synthesize a single concise description of what this table contains.

---Instructions---
1. Input Format: Each fragment is provided as JSONL — each line has a ``description`` field whose value is a markdown table (or a partial summary of one when the recursive reduce path is active).
2. Output Format: Plain text, one or two paragraphs. NO additional formatting, NO bullet lists, NO row-by-row dumps.
3. Cover (in order): what the table is about; the columns and what they appear to represent; the approximate row count and any visible scale / range indicators (e.g. "revenue values range from 1M to 89B"); any obvious patterns (time-series, regional groupings, categorical breakdowns).
4. Context: Mention the table name explicitly at the start. Write from an objective, third-person perspective.
5. Don't quote individual cell values unless they are summary statistics. Don't reproduce rows.
6. Length Constraint: The summary's total length must not exceed {max_tokens} tokens.
7. Language: {language}. Column headers and proper nouns retain their original form.

---Input---
Table Name: {name}

Fragments (each is a markdown sub-table or a partial summary):
{json_list}

---Output---
"""


# Concrete-summary prompt for **small** tables — used when the
# caller (table_enrichment) detects the markdown is small enough that
# a budget-respecting prose summary can comfortably mention specific
# values. Identical structure to PROMPT_USER_TABLE, but rule 5 flips
# from "don't quote values" to "DO quote representative values" so
# the description naturally includes "EMEA Q1 was 1200" style facts.
#
# Why bother having two prompts? For a 5-row table, an abstract
# description is wasteful — the user wants the numbers, and the
# answer LLM can quote them straight from the description without a
# follow-up agent round-trip. For a 5,000-row table, the same
# instruction produces noisy verbose output that gets truncated; the
# abstract prompt is the right call there.
PROMPT_USER_TABLE_SMALL = """\
---Task---
You will be given a small spreadsheet (or a few row groups of one). Produce a concise description that incorporates the actual cell values into the prose, so a downstream reader can see specific numbers / dates / names without opening the source file.

---Instructions---
1. Input Format: Each fragment is provided as JSONL — each line has a ``description`` field whose value is a markdown table.
2. Output Format: Plain text, one or two paragraphs. NO additional formatting, NO bullet lists, NO row-by-row dumps.
3. Cover (in order): what the table is about; the columns and what they represent; representative cell values inline (e.g. "EMEA Q1 revenue was $1,200; APAC was $2,200"); any obvious patterns.
4. Context: Mention the table name explicitly at the start. Write from an objective, third-person perspective.
5. DO incorporate specific cell values into the prose. If there are few enough rows (say, ≤ 10), it is fine to mention each one in a single sentence. For more rows, mention representative values, ranges, and notable extremes rather than every row.
6. Length Constraint: The summary's total length must not exceed {max_tokens} tokens. If a small table cannot fit verbatim within this budget, prefer aggregate values + a few representative rows over truncating mid-row.
7. Language: {language}. Column headers and proper nouns retain their original form.

---Input---
Table Name: {name}

Fragments (each is a markdown sub-table):
{json_list}

---Output---
"""


def _select_prompt(kind: str) -> str:
    """Pick the user-prompt template by ``kind``.

    Falls through to the entity/relation default if an unknown
    ``kind`` is passed — keeps callers loose-coupled.

    Recognized kinds:
      * ``"table"``       — abstract summary, no value quoting (big tables)
      * ``"table_small"`` — concrete summary, values inline (small tables)
      * ``"entity"`` / ``"relation"`` / anything else — the
        original LightRAG-style entity-description prompt.
    """
    if kind == "table":
        return PROMPT_USER_TABLE
    if kind == "table_small":
        return PROMPT_USER_TABLE_SMALL
    return PROMPT_USER_ENTITY


# Back-compat alias — older code references ``PROMPT_USER`` directly.
# Don't remove without an audit of callers.
PROMPT_USER = PROMPT_USER_ENTITY


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SummarizeConfig:
    """Knobs controlling when + how to compress a description.

    Defaults are tuned for ForgeRAG's typical entity description
    profile (50–100 tokens / fragment): ``trigger_tokens=1200`` lines
    up with the post-merge bloat threshold we observed empirically;
    ``force_on_count=8`` is the count-based escape hatch for "lots of
    small fragments slipping under the token gate".
    """

    enabled: bool = True

    # Trigger gates — either condition fires summarisation.
    trigger_tokens: int = 1200
    force_on_count: int = 8

    # Output length target. Passed through to the prompt as a
    # soft ceiling — the LLM doesn't strictly enforce it, but
    # gpt-4o-mini-class models hit ±15 % of this in practice.
    max_output_tokens: int = 600

    # Map-reduce input window. If total fragment tokens > this we
    # split into chunks of ≥2 fragments each, summarise each chunk,
    # then loop on the chunk-summaries until the result fits.
    context_size: int = 12000

    # Convergence guard for the map-reduce loop. With sane defaults
    # we typically converge in 1–2 iterations; 5 is a defensive
    # ceiling in case an LLM keeps producing oversized output.
    max_iterations: int = 5

    # LLM call params. Mirrors ``KGExtractor.__init__``.
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_base: str | None = None
    temperature: float = 0.0
    timeout: float = 60.0

    # Language directive for the prompt. The default tells the LLM
    # to follow the input — works for monolingual EN, monolingual ZH,
    # and mixed corpora alike. Override to e.g. ``"Chinese"`` if you
    # want canonical descriptions in a specific language regardless
    # of source.
    language: str = "Write the entire output in the original language of the input descriptions"

    # Optional thinking-mode directive forwarded as ``extra_body``.
    # Default disables CoT for these structured-text tasks — same
    # rationale as ``KGExtractor`` (CoT adds 5–7× latency for zero
    # accuracy gain on synthesise-from-list problems).
    extra_body: dict[str, Any] = field(default_factory=lambda: {"thinking": {"type": "disabled"}})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def split_fragments(description: str) -> list[str]:
    """Re-derive fragments from a stored description.

    Description merging in both ``NetworkXGraphStore`` and
    ``Neo4jGraphStore`` joins fragments with ``\\n``. Round-trip is
    not perfectly faithful — fragments that themselves contain
    newlines get over-split — but it's good enough for "decide
    whether to summarise" and "feed JSONL to the LLM".
    """
    if not description:
        return []
    return [s.strip() for s in description.split("\n") if s.strip()]


def needs_summary(fragments: list[str], cfg: SummarizeConfig) -> bool:
    """``True`` when the description has grown enough to warrant compaction."""
    if not cfg.enabled or not fragments:
        return False
    if len(fragments) >= cfg.force_on_count:
        return True
    total = sum(approx_tokens(f) for f in fragments)
    return total >= cfg.trigger_tokens


def summarize_descriptions(
    *,
    name: str,
    kind: str,
    fragments: list[str],
    cfg: SummarizeConfig,
) -> str:
    """Compress a description-fragment list to a single summary string.

    Map-reduce + recursive: if all fragments don't fit ``cfg.context_size``,
    split into chunks (≥2 fragments per chunk), summarise each, then loop
    on the chunk summaries until the total fits within the context size
    AND fragment count drops below the count-based threshold.

    ``kind`` is one of ``"entity"`` / ``"relation"`` — surfaced in the
    prompt as ``{kind} Name``. Returns the summary text on success;
    raises on LLM failure (caller decides whether to fall back to the
    pre-summary description).

    Sync interface matching ``KGExtractor._call_llm`` — caching
    handled transparently via ``forgerag.llm_cache.cached_completion``.
    """
    fragments = [f.strip() for f in fragments if f and f.strip()]
    if not fragments:
        return ""
    if len(fragments) == 1:
        return fragments[0]

    for iteration in range(1, cfg.max_iterations + 1):
        total_tok = sum(approx_tokens(f) for f in fragments)
        fits_context = total_tok <= cfg.context_size
        few_enough = len(fragments) < cfg.force_on_count

        # Base case: one LLM call merges everything into one summary.
        # We always want a single LLM-merged paragraph as the final
        # output, even when the count is already below threshold —
        # otherwise the caller gets back the same multi-line concat
        # that triggered summarisation in the first place.
        if fits_context:
            log.info(
                "summarise %s '%s': single call (%d frags, ~%d tok, iter=%d)",
                kind,
                name,
                len(fragments),
                total_tok,
                iteration,
            )
            return _summarize_chunk(name, kind, fragments, cfg)

        # Fragment list too big for one window — map-reduce.
        chunks = _chunk_by_token(fragments, cfg.context_size)
        if len(chunks) <= 1:
            # Couldn't split (one fragment is itself bigger than the
            # context window). Best-effort: send what we can.
            log.warning(
                "summarise %s '%s': single fragment exceeds context; truncating",
                kind,
                name,
            )
            return _summarize_chunk(name, kind, chunks[0] if chunks else fragments, cfg)

        log.info(
            "summarise %s '%s': map-reduce iter=%d, %d frags → %d chunks",
            kind,
            name,
            iteration,
            len(fragments),
            len(chunks),
        )

        # Map: summarise each chunk independently.
        summaries: list[str] = []
        for c in chunks:
            try:
                s = _summarize_chunk(name, kind, c, cfg)
                if s.strip():
                    summaries.append(s.strip())
            except Exception as exc:
                # One chunk's failure shouldn't lose the whole entity.
                # Fall back to verbatim concatenation of this chunk —
                # subsequent iterations may compress it further.
                log.warning(
                    "summarise %s '%s' chunk LLM failed (%s); using verbatim",
                    kind,
                    name,
                    exc,
                )
                summaries.append("\n".join(c))

        # Reduce: chunk summaries become the new fragment list. Loop.
        fragments = summaries

        # Suppressing ``few_enough`` here on purpose — the loop's
        # exit condition is ``fits_context``. Once the list fits we
        # do one final merge call.
        _ = few_enough  # quieten linters

    # Convergence guard fired. Return what we have, joined.
    log.warning(
        "summarise %s '%s': did not converge in %d iters; returning joined",
        kind,
        name,
        cfg.max_iterations,
    )
    return "\n\n".join(fragments)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _chunk_by_token(fragments: list[str], limit: int) -> list[list[str]]:
    """Greedy pack fragments into chunks bounded by approximate token count.

    Each chunk gets ≥2 fragments where possible — a single-fragment
    chunk is wasteful (the LLM call just paraphrases one input). We
    enforce this by merging a trailing 1-fragment chunk back into
    the previous one, even if that nominally exceeds ``limit`` —
    summarisation is a soft constraint anyway and the LLM call has
    its own provider-side ceiling.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    current_tok = 0
    for f in fragments:
        ft = approx_tokens(f)
        if current and current_tok + ft > limit:
            chunks.append(current)
            current = [f]
            current_tok = ft
        else:
            current.append(f)
            current_tok += ft
    if current:
        chunks.append(current)

    # Merge a trailing single-fragment chunk into its predecessor.
    if len(chunks) > 1 and len(chunks[-1]) == 1:
        chunks[-2].extend(chunks[-1])
        chunks.pop()

    return chunks


def _summarize_chunk(
    name: str,
    kind: str,
    fragments: list[str],
    cfg: SummarizeConfig,
) -> str:
    """One LLM round-trip: ``fragments → single summary``."""
    json_list = "\n".join(
        json.dumps({"description": f}, ensure_ascii=False) for f in fragments
    )
    template = _select_prompt(kind)
    user = template.format(
        kind=kind,
        name=name,
        max_tokens=cfg.max_output_tokens,
        json_list=json_list,
        language=cfg.language,
    )
    return _call_llm(PROMPT_SYSTEM, user, cfg)


def _call_llm(system: str, user: str, cfg: SummarizeConfig) -> str:
    """LLM round-trip via ``forgerag.llm_cache.cached_completion``.

    Returns the trimmed content string. Empty content (some providers
    return ``""`` on rare failure modes) raises so callers can fall
    back to the verbatim description rather than overwriting it with
    nothing.
    """
    from opencraig.llm_cache import cached_completion

    kwargs: dict[str, Any] = dict(
        model=cfg.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=cfg.temperature,
        timeout=cfg.timeout,
    )
    if cfg.extra_body:
        kwargs["extra_body"] = cfg.extra_body
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    if cfg.api_base:
        kwargs["api_base"] = cfg.api_base

    resp = cached_completion(**kwargs)
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("summarize LLM returned empty content")
    return content
