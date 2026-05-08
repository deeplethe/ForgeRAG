#!/usr/bin/env python3
"""
UltraDomain Benchmark for OpenCraig
===================================

Standalone script that reproduces the LightRAG evaluation methodology
(https://github.com/HKUDS/LightRAG/blob/main/docs/Reproduce.md)
against a running OpenCraig instance.

Dataset : TommyChien/UltraDomain  (HuggingFace)
Metrics : Comprehensiveness · Diversity · Empowerment  (LLM-as-judge)

Pipeline
--------
  1.  Download  — fetch domain JSONL from HuggingFace
  2.  Extract   — deduplicate contexts
  3.  Ingest    — upload unique contexts as .txt files into OpenCraig
  4.  Generate  — create high-level queries via LLM
  5.  Query     — ask OpenCraig each question, collect answers
  6.  Evaluate  — LLM-as-judge pairwise comparison (optional baseline)

Usage
-----
  # Full run: ingest agriculture domain + generate queries + answer
  python scripts/ultradomain_bench.py --domain agriculture --forgerag http://localhost:8000

  # Skip ingestion (already loaded), just re-run queries
  python scripts/ultradomain_bench.py --domain agriculture --skip-ingest --skip-generate

  # Compare OpenCraig answers against a baseline JSON file
  python scripts/ultradomain_bench.py --domain agriculture --skip-ingest --skip-generate \\
      --baseline results/baseline_agriculture.json --eval-model gpt-4o-mini

Requirements
------------
  pip install httpx litellm huggingface_hub
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("ultradomain_bench")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HF_DATASET = "TommyChien/UltraDomain"
DOMAINS = [
    "agriculture",
    "art",
    "biography",
    "biology",
    "cooking",
    "cs",
    "fiction",
    "fin",
    "health",
    "history",
    "legal",
    "literature",
    "mathematics",
    "mix",
    "music",
    "philosophy",
    "physics",
    "politics",
    "psychology",
    "technology",
]

DEFAULT_EVAL_MODEL = "gpt-4o-mini"

# ---------------------------------------------------------------------------
# Step 0: Download + extract unique contexts
# ---------------------------------------------------------------------------


def download_domain(domain: str, cache_dir: Path) -> Path:
    """Download a single domain JSONL from HuggingFace."""
    from huggingface_hub import hf_hub_download

    local = cache_dir / f"{domain}.jsonl"
    if local.exists():
        log.info("cached: %s", local)
        return local

    log.info("downloading %s.jsonl from %s ...", domain, HF_DATASET)
    path = hf_hub_download(
        repo_id=HF_DATASET,
        filename=f"{domain}.jsonl",
        repo_type="dataset",
        local_dir=str(cache_dir),
    )
    return Path(path)


def extract_unique_contexts(jsonl_path: Path, out_path: Path) -> list[str]:
    """Deduplicate context field from JSONL → list[str]."""
    if out_path.exists():
        log.info("cached unique contexts: %s", out_path)
        return json.loads(out_path.read_text("utf-8"))

    seen: dict[str, None] = {}
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ctx = obj.get("context")
                if ctx and ctx not in seen:
                    seen[ctx] = None
            except json.JSONDecodeError:
                continue

    contexts = list(seen.keys())
    out_path.write_text(json.dumps(contexts, ensure_ascii=False, indent=2), "utf-8")
    log.info("extracted %d unique contexts → %s", len(contexts), out_path)
    return contexts


# ---------------------------------------------------------------------------
# Step 1: Ingest into OpenCraig
# ---------------------------------------------------------------------------


def ingest_contexts(
    contexts: list[str],
    api_base: str,
    *,
    batch_size: int = 5,
    poll_interval: float = 3.0,
    timeout: float = 600.0,
) -> list[dict]:
    """Upload each context as a .txt document and wait until all are ready."""
    import httpx

    client = httpx.Client(base_url=api_base, timeout=60.0)
    doc_ids: list[dict] = []

    log.info("ingesting %d contexts into OpenCraig ...", len(contexts))
    for i, ctx in enumerate(contexts):
        fname = f"ultradomain_ctx_{i:04d}.txt"
        resp = client.post(
            "/api/v1/documents/upload-and-ingest",
            files={"file": (fname, ctx.encode("utf-8"), "text/plain")},
        )
        if resp.status_code not in (201, 202):
            log.error("upload failed [%d]: %s", resp.status_code, resp.text[:200])
            continue
        data = resp.json()
        doc_ids.append({"doc_id": data["doc_id"], "file_id": data.get("file_id")})

        if (i + 1) % 10 == 0:
            log.info("  uploaded %d / %d", i + 1, len(contexts))

    # Poll until all documents reach "ready" (or "error")
    log.info("waiting for %d documents to finish processing ...", len(doc_ids))
    pending = {d["doc_id"] for d in doc_ids}
    t0 = time.time()
    while pending and (time.time() - t0) < timeout:
        time.sleep(poll_interval)
        still_pending = set()
        for doc_id in pending:
            try:
                r = client.get(f"/api/v1/documents/{doc_id}")
                status = r.json().get("status", "unknown")
                if status in ("ready", "error"):
                    continue  # done
                still_pending.add(doc_id)
            except Exception:
                still_pending.add(doc_id)
        log.info("  %d / %d still processing ...", len(still_pending), len(doc_ids))
        pending = still_pending

    if pending:
        log.warning("%d documents did not finish within %.0fs", len(pending), timeout)

    client.close()
    return doc_ids


# ---------------------------------------------------------------------------
# Step 2: Generate queries
# ---------------------------------------------------------------------------

QUERY_GEN_PROMPT = """\
Given the following description of a dataset:

{description}

Please identify 5 potential users who would engage with this dataset. \
For each user, list 5 tasks they would perform with this dataset. \
Then, for each (user, task) combination, generate 5 questions that \
require a high-level understanding of the entire dataset.

Output the results in the following structure:
- User 1: [user description]
    - Task 1: [task description]
        - Question 1:
        - Question 2:
        - Question 3:
        - Question 4:
        - Question 5:
    - Task 2: [task description]
        ...
    - Task 5: [task description]
- User 2: [user description]
    ...
- User 5: [user description]
    ...
"""


def _summarize_for_description(contexts: list[str], max_tokens: int = 2000) -> str:
    """Build a dataset description from sampled context tokens (LightRAG style)."""
    combined = "\n\n".join(contexts[:20])  # use first 20 contexts as representative sample
    # Rough token budget: 4 chars ≈ 1 token
    char_budget = max_tokens * 4
    if len(combined) > char_budget:
        half = char_budget // 2
        combined = combined[:half] + "\n...\n" + combined[-half:]
    return combined


def generate_queries(
    contexts: list[str],
    out_path: Path,
    *,
    model: str = DEFAULT_EVAL_MODEL,
    api_key: str | None = None,
    api_base: str | None = None,
) -> list[str]:
    """Generate high-level queries via LLM (LightRAG methodology)."""
    if out_path.exists():
        text = out_path.read_text("utf-8")
        queries = _extract_questions(text)
        if queries:
            log.info("cached %d queries from %s", len(queries), out_path)
            return queries

    import litellm

    description = _summarize_for_description(contexts)
    prompt = QUERY_GEN_PROMPT.format(description=description)

    kwargs: dict[str, Any] = dict(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=4000,
    )
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base

    log.info("generating queries with %s ...", model)
    resp = litellm.completion(**kwargs)
    text = resp.choices[0].message.content or ""

    out_path.write_text(text, "utf-8")
    queries = _extract_questions(text)
    log.info("generated %d queries → %s", len(queries), out_path)
    return queries


def _extract_questions(text: str) -> list[str]:
    """Extract 'Question N: ...' lines from LLM output."""
    text = text.replace("**", "")
    return re.findall(r"[-–]\s*Question\s+\d+:\s*(.+)", text)


# ---------------------------------------------------------------------------
# Step 3: Query OpenCraig
# ---------------------------------------------------------------------------


def query_forgerag(
    queries: list[str],
    api_base: str,
    out_path: Path,
    *,
    conversation_id: str | None = None,
) -> list[dict]:
    """Send each query to OpenCraig, collect answers."""
    if out_path.exists():
        results = json.loads(out_path.read_text("utf-8"))
        if len(results) == len(queries):
            log.info("cached %d answers from %s", len(results), out_path)
            return results

    import httpx

    client = httpx.Client(base_url=api_base, timeout=120.0)
    results: list[dict] = []

    log.info("querying OpenCraig with %d questions ...", len(queries))
    for i, q in enumerate(queries):
        body: dict[str, Any] = {"query": q, "stream": False}
        try:
            resp = client.post("/api/v1/query", json=body)
            if resp.status_code == 200:
                data = resp.json()
                results.append(
                    {
                        "query": q,
                        "result": data.get("text", ""),
                        "citations_count": len(data.get("citations_used", [])),
                        "model": data.get("model", ""),
                        "finish_reason": data.get("finish_reason", ""),
                        "stats": data.get("stats", {}),
                    }
                )
            else:
                log.warning("query %d failed [%d]: %s", i, resp.status_code, resp.text[:200])
                results.append({"query": q, "result": "", "error": resp.text[:200]})
        except Exception as e:
            log.error("query %d exception: %s", i, e)
            results.append({"query": q, "result": "", "error": str(e)})

        if (i + 1) % 10 == 0:
            log.info("  answered %d / %d", i + 1, len(queries))
            # Checkpoint
            out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")

    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
    client.close()
    log.info("collected %d answers → %s", len(results), out_path)
    return results


# ---------------------------------------------------------------------------
# Step 4: Pairwise LLM-as-judge evaluation
# ---------------------------------------------------------------------------

EVAL_SYSTEM = """\
---Role---
You are an expert tasked with evaluating two answers to the same \
question based on three criteria: **Comprehensiveness**, **Diversity**, \
and **Empowerment**."""

EVAL_PROMPT = """\
You will evaluate two answers to the same question based on three \
criteria: **Comprehensiveness**, **Diversity**, and **Empowerment**.

- **Comprehensiveness**: How much detail does the answer provide to \
cover all aspects and details of the question?
- **Diversity**: How varied and rich is the answer in providing \
different perspectives and insights on the question?
- **Empowerment**: How well does the answer help the reader understand \
and make informed judgments about the topic?

For each criterion, choose the better answer (either Answer 1 or \
Answer 2) and explain why. Then, select an overall winner based on \
these three categories.

Here is the question:
{query}

Here are the two answers:

**Answer 1:**
{answer1}

**Answer 2:**
{answer2}

Evaluate both answers using the three criteria listed above and \
provide detailed explanations for each criterion.

Output your evaluation in the following JSON format:

{{
    "Comprehensiveness": {{
        "Winner": "[Answer 1 or Answer 2]",
        "Explanation": "[Provide explanation here]"
    }},
    "Diversity": {{
        "Winner": "[Answer 1 or Answer 2]",
        "Explanation": "[Provide explanation here]"
    }},
    "Empowerment": {{
        "Winner": "[Answer 1 or Answer 2]",
        "Explanation": "[Provide explanation here]"
    }},
    "Overall Winner": {{
        "Winner": "[Answer 1 or Answer 2]",
        "Explanation": "[Summarize why this answer is the overall winner]"
    }}
}}"""


def pairwise_eval(
    queries: list[str],
    answers_a: list[dict],
    answers_b: list[dict],
    out_path: Path,
    *,
    label_a: str = "OpenCraig",
    label_b: str = "Baseline",
    model: str = DEFAULT_EVAL_MODEL,
    api_key: str | None = None,
    api_base: str | None = None,
) -> dict:
    """Run LLM-as-judge pairwise evaluation (LightRAG methodology)."""
    import litellm

    assert len(queries) == len(answers_a) == len(answers_b), (
        f"length mismatch: {len(queries)} queries, {len(answers_a)} A, {len(answers_b)} B"
    )

    evals: list[dict] = []
    wins = {
        "Comprehensiveness": {label_a: 0, label_b: 0},
        "Diversity": {label_a: 0, label_b: 0},
        "Empowerment": {label_a: 0, label_b: 0},
        "Overall Winner": {label_a: 0, label_b: 0},
    }

    log.info("running pairwise evaluation (%d pairs) with %s ...", len(queries), model)
    for i, (q, a, b) in enumerate(zip(queries, answers_a, answers_b)):
        text_a = a.get("result", "") if isinstance(a, dict) else str(a)
        text_b = b.get("result", "") if isinstance(b, dict) else str(b)

        prompt = EVAL_PROMPT.format(query=q, answer1=text_a, answer2=text_b)

        kwargs: dict[str, Any] = dict(
            model=model,
            messages=[
                {"role": "system", "content": EVAL_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=2000,
        )
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base

        try:
            resp = litellm.completion(**kwargs)
            content = resp.choices[0].message.content or ""
            # Parse JSON from response
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                result = json.loads(m.group(0))
            else:
                result = {"parse_error": content[:300]}
        except Exception as e:
            log.warning("eval %d failed: %s", i, e)
            result = {"error": str(e)}

        result["query"] = q
        evals.append(result)

        # Tally wins
        for criterion in ("Comprehensiveness", "Diversity", "Empowerment", "Overall Winner"):
            winner = result.get(criterion, {}).get("Winner", "")
            if "1" in winner:
                wins[criterion][label_a] += 1
            elif "2" in winner:
                wins[criterion][label_b] += 1

        if (i + 1) % 10 == 0:
            log.info("  evaluated %d / %d", i + 1, len(queries))

    # Summary
    total = len(queries)
    summary = {}
    for criterion, counts in wins.items():
        summary[criterion] = {
            label: f"{count}/{total} ({count / max(total, 1) * 100:.1f}%)" for label, count in counts.items()
        }

    output = {
        "label_a": label_a,
        "label_b": label_b,
        "model": model,
        "total_queries": total,
        "summary": summary,
        "raw_wins": wins,
        "evaluations": evals,
    }
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), "utf-8")
    log.info("evaluation complete → %s", out_path)
    return output


def print_summary(output: dict):
    """Pretty-print evaluation summary table."""
    label_a = output["label_a"]
    label_b = output["label_b"]
    total = output["total_queries"]
    summary = output["summary"]

    print(f"\n{'=' * 60}")
    print(f"  UltraDomain Pairwise Evaluation  (n={total})")
    print(f"  {label_a} (Answer 1) vs {label_b} (Answer 2)")
    print(f"  Judge: {output['model']}")
    print(f"{'=' * 60}")
    print(f"  {'Criterion':<22} {label_a:>15} {label_b:>15}")
    print(f"  {'-' * 52}")
    for criterion in ("Comprehensiveness", "Diversity", "Empowerment", "Overall Winner"):
        vals = summary.get(criterion, {})
        a_val = vals.get(label_a, "0/0 (0.0%)")
        b_val = vals.get(label_b, "0/0 (0.0%)")
        print(f"  {criterion:<22} {a_val:>15} {b_val:>15}")
    print(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="UltraDomain benchmark for OpenCraig (LightRAG methodology)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Full pipeline: download, ingest, generate queries, answer, evaluate
  python scripts/ultradomain_bench.py --domain agriculture

  # Only query + eval (data already ingested)
  python scripts/ultradomain_bench.py --domain agriculture --skip-ingest --skip-generate

  # Compare against a baseline
  python scripts/ultradomain_bench.py --domain agriculture --skip-ingest \\
      --baseline results/naiverag_agriculture.json
""",
    )
    parser.add_argument(
        "--domain",
        required=True,
        choices=DOMAINS,
        help="UltraDomain domain to benchmark",
    )
    parser.add_argument(
        "--forgerag",
        default="http://localhost:8000",
        help="OpenCraig API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--output-dir",
        default="./benchmark_results",
        help="Directory for all output files",
    )
    parser.add_argument(
        "--cache-dir",
        default="./benchmark_cache",
        help="Directory for downloaded datasets & cached contexts",
    )

    # Skip flags
    parser.add_argument("--skip-ingest", action="store_true", help="Skip ingestion (assume docs already loaded)")
    parser.add_argument("--skip-generate", action="store_true", help="Skip query generation (use cached queries)")
    parser.add_argument("--skip-query", action="store_true", help="Skip querying OpenCraig (use cached answers)")

    # Eval options
    parser.add_argument(
        "--baseline", type=str, default=None, help="Path to baseline answers JSON for pairwise comparison"
    )
    parser.add_argument(
        "--eval-model",
        default=DEFAULT_EVAL_MODEL,
        help=f"LLM model for evaluation judge (default: {DEFAULT_EVAL_MODEL})",
    )
    parser.add_argument(
        "--query-model",
        default=DEFAULT_EVAL_MODEL,
        help=f"LLM model for query generation (default: {DEFAULT_EVAL_MODEL})",
    )

    # LLM auth
    parser.add_argument("--api-key", default=None, help="API key for LLM calls (query gen + eval)")
    parser.add_argument("--api-base", default=None, help="API base URL for LLM calls")

    # Limits
    parser.add_argument(
        "--max-contexts", type=int, default=None, help="Limit number of contexts to ingest (for quick test)"
    )
    parser.add_argument("--max-queries", type=int, default=None, help="Limit number of queries to run")

    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    domain = args.domain
    cache_dir = Path(args.cache_dir)
    out_dir = Path(args.output_dir) / domain
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 0: Download + extract ──
    log.info("=== Step 0: Download & extract unique contexts ===")
    jsonl_path = download_domain(domain, cache_dir)
    contexts_path = cache_dir / f"{domain}_unique_contexts.json"
    contexts = extract_unique_contexts(jsonl_path, contexts_path)
    log.info("  %d unique contexts", len(contexts))

    if args.max_contexts:
        contexts = contexts[: args.max_contexts]
        log.info("  limited to %d contexts", len(contexts))

    # ── Step 1: Ingest ──
    if not args.skip_ingest:
        log.info("=== Step 1: Ingest into OpenCraig ===")
        doc_ids = ingest_contexts(contexts, args.forgerag)
        (out_dir / "doc_ids.json").write_text(
            json.dumps(doc_ids, indent=2),
            "utf-8",
        )
    else:
        log.info("=== Step 1: Skipped (--skip-ingest) ===")

    # ── Step 2: Generate queries ──
    queries_path = out_dir / "queries.txt"
    if not args.skip_generate:
        log.info("=== Step 2: Generate queries ===")
        queries = generate_queries(
            contexts,
            queries_path,
            model=args.query_model,
            api_key=args.api_key,
            api_base=args.api_base,
        )
    else:
        log.info("=== Step 2: Skipped (--skip-generate) ===")
        if queries_path.exists():
            queries = _extract_questions(queries_path.read_text("utf-8"))
        else:
            log.error("no cached queries at %s; remove --skip-generate", queries_path)
            sys.exit(1)

    if args.max_queries:
        queries = queries[: args.max_queries]
    log.info("  %d queries to evaluate", len(queries))

    # ── Step 3: Query OpenCraig ──
    answers_path = out_dir / "forgerag_answers.json"
    if not args.skip_query:
        log.info("=== Step 3: Query OpenCraig ===")
        answers = query_forgerag(queries, args.forgerag, answers_path)
    else:
        log.info("=== Step 3: Skipped (--skip-query) ===")
        if answers_path.exists():
            answers = json.loads(answers_path.read_text("utf-8"))
        else:
            log.error("no cached answers at %s; remove --skip-query", answers_path)
            sys.exit(1)

    # Stats
    answered = sum(1 for a in answers if a.get("result", "").strip())
    log.info("  %d / %d answered (%.1f%%)", answered, len(answers), answered / max(len(answers), 1) * 100)

    # ── Step 4: Pairwise evaluation (optional) ──
    if args.baseline:
        log.info("=== Step 4: Pairwise evaluation ===")
        baseline = json.loads(Path(args.baseline).read_text("utf-8"))
        if isinstance(baseline, list):
            baseline_answers = baseline
        elif isinstance(baseline, dict) and "answers" in baseline:
            baseline_answers = baseline["answers"]
        else:
            baseline_answers = baseline

        # Align lengths
        n = min(len(queries), len(answers), len(baseline_answers))
        eval_path = out_dir / "eval_results.json"
        output = pairwise_eval(
            queries[:n],
            answers[:n],
            baseline_answers[:n],
            eval_path,
            label_a="OpenCraig",
            label_b="Baseline",
            model=args.eval_model,
            api_key=args.api_key,
            api_base=args.api_base,
        )
        print_summary(output)
    else:
        log.info("=== Step 4: Skipped (no --baseline provided) ===")
        log.info("  To run pairwise eval, provide --baseline <path-to-answers.json>")
        log.info('  Baseline JSON format: [{"result": "answer text"}, ...]')

    log.info("Done. Results in %s", out_dir)


if __name__ == "__main__":
    main()
