#!/usr/bin/env python3
"""
OpenCraig vs LightRAG Benchmark (UltraDomain)
=============================================

Runs both systems in parallel on the same queries, then does
LLM-as-judge pairwise evaluation.

Pipeline
--------
  1.  Download UltraDomain JSONL from HuggingFace
  2.  Ingest into both OpenCraig and LightRAG (parallel)
  3.  Generate high-level queries via LLM (shared)
  4.  Query both systems in parallel, checkpoint every 5 answers
  5.  Pairwise evaluation (LLM-as-judge)

Usage
-----
  python scripts/compare_bench.py --domain agriculture
  python scripts/compare_bench.py --domain cs --skip-ingest --skip-generate

Sample Results
--------------
  See scripts/sample_results/agriculture_eval.json for a complete
  evaluation output (140 queries, agriculture domain).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

log = logging.getLogger("compare_bench")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HF_DATASET = "TommyChien/UltraDomain"
DOMAINS = [
    "agriculture",
    "cs",
    "biology",
    "fin",
    "legal",
    "health",
    "physics",
    "technology",
]

FORGERAG_URL = "http://localhost:8000"
LIGHTRAG_URL = "http://localhost:9621"

CHECKPOINT_INTERVAL = 5  # save every N answers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_checkpoint(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return []


def _save_checkpoint(data: list[dict], path: Path):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


# ---------------------------------------------------------------------------
# Step 0: Download + extract unique contexts
# ---------------------------------------------------------------------------


def download_domain(domain: str, cache_dir: Path) -> Path:
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
    if out_path.exists():
        log.info("cached unique contexts: %s", out_path)
        return json.loads(out_path.read_text("utf-8"))

    seen: dict[str, None] = {}
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            ctx = obj.get("context", "").strip()
            if ctx and ctx not in seen:
                seen[ctx] = None
    contexts = list(seen.keys())
    out_path.write_text(json.dumps(contexts, ensure_ascii=False, indent=2), "utf-8")
    log.info("extracted %d unique contexts → %s", len(contexts), out_path)
    return contexts


# ---------------------------------------------------------------------------
# Step 1: Ingest
# ---------------------------------------------------------------------------


def ingest_forgerag(contexts: list[str], base_url: str, domain: str):
    """Upload contexts as .txt files to OpenCraig."""
    import httpx

    log.info("[OpenCraig] ingesting %d contexts ...", len(contexts))
    client = httpx.Client(base_url=base_url, timeout=120)

    for i, ctx in enumerate(contexts):
        fname = f"ultradomain_{domain}_{i:04d}.txt"
        files = {"file": (fname, ctx.encode("utf-8"), "text/plain")}
        try:
            resp = client.post("/api/v1/documents/upload-and-ingest", files=files)
            resp.raise_for_status()
        except Exception as e:
            log.warning("[OpenCraig] ingest %d failed: %s", i, e)
        if (i + 1) % 50 == 0:
            log.info("[OpenCraig] ingested %d / %d", i + 1, len(contexts))

    # Wait for processing — paginate since API caps limit at 200
    log.info("[OpenCraig] waiting for ingestion to complete ...")
    for _ in range(120):
        time.sleep(5)
        try:
            pending = 0
            offset = 0
            while True:
                resp = client.get("/api/v1/documents", params={"limit": 200, "offset": offset})
                data = resp.json()
                docs = data.get("items", [])
                pending += sum(1 for d in docs if d.get("status") not in ("ready", "error"))
                if len(docs) < 200:
                    break
                offset += 200
            if pending == 0:
                log.info("[OpenCraig] all documents ready")
                break
            log.info("[OpenCraig] %d docs still processing ...", pending)
        except Exception:
            pass
    client.close()


def ingest_lightrag(contexts: list[str], base_url: str, domain: str):
    """Upload contexts as text to LightRAG."""
    import httpx

    log.info("[LightRAG] ingesting %d contexts ...", len(contexts))
    client = httpx.Client(base_url=base_url, timeout=300)

    # Use batch insert — LightRAG expects {"texts": [...]}
    batch_size = 20
    for i in range(0, len(contexts), batch_size):
        batch = contexts[i : i + batch_size]
        try:
            resp = client.post("/documents/texts", json={"texts": batch})
            resp.raise_for_status()
        except Exception as e:
            log.warning("[LightRAG] batch %d failed: %s", i, e)
        done = min(i + batch_size, len(contexts))
        if done % 100 == 0 or done == len(contexts):
            log.info("[LightRAG] ingested %d / %d", done, len(contexts))

    # Wait for pipeline
    log.info("[LightRAG] waiting for pipeline to complete ...")
    for _ in range(120):
        time.sleep(5)
        try:
            resp = client.get("/documents/pipeline_status")
            status = resp.json()
            if not status.get("is_busy", True):
                log.info("[LightRAG] pipeline idle")
                break
        except Exception:
            try:
                health = client.get("/health").json()
                if not health.get("pipeline_busy", True):
                    log.info("[LightRAG] pipeline idle (via health)")
                    break
            except Exception:
                pass
    client.close()


# ---------------------------------------------------------------------------
# Step 2: Generate queries (shared)
# ---------------------------------------------------------------------------

QUERY_GEN_PROMPT = """You are a curious researcher studying the domain of {domain}.
Given a dataset summary below, generate {n} diverse, high-level questions that
require cross-document reasoning. Questions should be non-trivial and require
synthesizing information from multiple passages.

Dataset summary:
{summary}

Generate exactly {n} questions, one per line. No numbering, no bullets, just plain questions."""


def generate_queries(
    contexts: list[str],
    out_path: Path,
    domain: str,
    *,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
    api_base: str | None = None,
    n_questions: int = 125,
) -> list[str]:
    if out_path.exists():
        questions = _extract_questions(out_path.read_text("utf-8"))
        if len(questions) >= 10:
            log.info("cached %d queries from %s", len(questions), out_path)
            return questions

    import litellm

    # Build summary from first 20 contexts
    sample = contexts[:20]
    summary_parts = []
    for i, ctx in enumerate(sample):
        snippet = ctx[:500].replace("\n", " ")
        summary_parts.append(f"[{i + 1}] {snippet}")
    summary = "\n".join(summary_parts)

    prompt = QUERY_GEN_PROMPT.format(domain=domain, summary=summary, n=n_questions)

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

    log.info("generating %d queries with %s ...", n_questions, model)
    resp = litellm.completion(**kwargs)
    text = resp.choices[0].message.content or ""
    out_path.write_text(text, "utf-8")

    questions = _extract_questions(text)
    log.info("generated %d questions → %s", len(questions), out_path)
    return questions


def _extract_questions(text: str) -> list[str]:
    lines = text.strip().split("\n")
    questions = []
    for line in lines:
        q = line.strip()
        # Remove numbering like "1. " or "- "
        q = re.sub(r"^\d+[\.\)]\s*", "", q)
        q = re.sub(r"^[-*]\s*", "", q)
        q = q.strip()
        if q and q.endswith("?"):
            questions.append(q)
    # Also include lines that look like questions but don't end with ?
    if len(questions) < 5:
        for line in lines:
            q = line.strip()
            q = re.sub(r"^\d+[\.\)]\s*", "", q).strip()
            if q and len(q) > 15 and q not in questions:
                questions.append(q)
    return questions


# ---------------------------------------------------------------------------
# Step 3: Query both systems in parallel
# ---------------------------------------------------------------------------


def _query_forgerag_one(query: str, base_url: str, timeout: float = 120) -> dict:
    import httpx

    t0 = time.time()
    try:
        client = httpx.Client(base_url=base_url, timeout=timeout)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": query,
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        client.close()
        ms = int((time.time() - t0) * 1000)
        return {
            "query": query,
            "result": data.get("text", data.get("answer", "")),
            "citations": len(data.get("citations_used", [])),
            "latency_ms": ms,
            "model": data.get("model", ""),
            "system": "OpenCraig",
        }
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {
            "query": query,
            "result": "",
            "error": str(e),
            "latency_ms": ms,
            "system": "OpenCraig",
        }


def _query_lightrag_one(query: str, base_url: str, mode: str = "hybrid", timeout: float = 120) -> dict:
    import httpx

    t0 = time.time()
    try:
        client = httpx.Client(base_url=base_url, timeout=timeout)
        resp = client.post(
            "/query",
            json={
                "query": query,
                "mode": mode,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        client.close()
        ms = int((time.time() - t0) * 1000)
        return {
            "query": query,
            "result": data.get("response", ""),
            "references": len(data.get("references", [])),
            "latency_ms": ms,
            "system": "LightRAG",
        }
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {
            "query": query,
            "result": "",
            "error": str(e),
            "latency_ms": ms,
            "system": "LightRAG",
        }


def query_both_parallel(
    queries: list[str],
    forgerag_url: str,
    lightrag_url: str,
    forge_path: Path,
    light_path: Path,
    *,
    max_workers: int = 4,
):
    """Query OpenCraig and LightRAG in parallel, with checkpointing."""
    forge_answers = _load_checkpoint(forge_path)
    light_answers = _load_checkpoint(light_path)

    forge_done = {a["query"] for a in forge_answers}
    light_done = {a["query"] for a in light_answers}

    forge_todo = [(i, q) for i, q in enumerate(queries) if q not in forge_done]
    light_todo = [(i, q) for i, q in enumerate(queries) if q not in light_done]

    if forge_todo:
        log.info("[OpenCraig] %d queries remaining (of %d)", len(forge_todo), len(queries))
    else:
        log.info("[OpenCraig] all %d queries already cached", len(queries))

    if light_todo:
        log.info("[LightRAG] %d queries remaining (of %d)", len(light_todo), len(queries))
    else:
        log.info("[LightRAG] all %d queries already cached", len(queries))

    forge_new = 0
    light_new = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}

        # Submit OpenCraig queries
        for idx, q in forge_todo:
            f = pool.submit(_query_forgerag_one, q, forgerag_url)
            futures[f] = ("forge", idx, q)

        # Submit LightRAG queries
        for idx, q in light_todo:
            f = pool.submit(_query_lightrag_one, q, lightrag_url)
            futures[f] = ("light", idx, q)

        total = len(futures)
        done_count = 0

        for future in as_completed(futures):
            system, idx, q = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = {"query": q, "result": "", "error": str(e), "system": system, "latency_ms": 0}

            if system == "forge":
                forge_answers.append(result)
                forge_new += 1
                if forge_new % CHECKPOINT_INTERVAL == 0:
                    _save_checkpoint(forge_answers, forge_path)
            else:
                light_answers.append(result)
                light_new += 1
                if light_new % CHECKPOINT_INTERVAL == 0:
                    _save_checkpoint(light_answers, light_path)

            done_count += 1
            if done_count % 10 == 0:
                log.info("  progress: %d / %d completed", done_count, total)

    # Final save
    _save_checkpoint(forge_answers, forge_path)
    _save_checkpoint(light_answers, light_path)
    log.info("queries done: OpenCraig=%d LightRAG=%d", len(forge_answers), len(light_answers))
    return forge_answers, light_answers


# ---------------------------------------------------------------------------
# Step 4: Pairwise evaluation
# ---------------------------------------------------------------------------

EVAL_SYSTEM = """You are an impartial evaluator. Given a question and two answers,
judge which answer is better on the following criteria.
Respond with ONLY a JSON object."""

EVAL_PROMPT = """Question: {query}

Answer A:
{answer1}

Answer B:
{answer2}

Evaluate on these criteria. For each, explain briefly and declare "Winner": "Answer A" or "Answer B".

{{
  "Comprehensiveness": {{"Explanation": "...", "Winner": "Answer A or Answer B"}},
  "Diversity": {{"Explanation": "...", "Winner": "Answer A or Answer B"}},
  "Empowerment": {{"Explanation": "...", "Winner": "Answer A or Answer B"}},
  "Overall Winner": {{"Explanation": "...", "Winner": "Answer A or Answer B"}}
}}"""


def pairwise_eval(
    queries: list[str],
    forge_answers: list[dict],
    light_answers: list[dict],
    out_path: Path,
    *,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
    api_base: str | None = None,
) -> dict:
    """Run LLM-as-judge pairwise evaluation with checkpointing."""
    import litellm

    # Build query → answer maps
    forge_map = {a["query"]: a.get("result", "") for a in forge_answers}
    light_map = {a["query"]: a.get("result", "") for a in light_answers}

    # Load existing evals
    existing = {}
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text("utf-8"))
            for ev in prev.get("evaluations", []):
                existing[ev.get("query", "")] = ev
        except Exception:
            pass

    evals: list[dict] = list(existing.values())
    wins = {
        "Comprehensiveness": {"OpenCraig": 0, "LightRAG": 0},
        "Diversity": {"OpenCraig": 0, "LightRAG": 0},
        "Empowerment": {"OpenCraig": 0, "LightRAG": 0},
        "Overall Winner": {"OpenCraig": 0, "LightRAG": 0},
    }

    def _tally_winner(winner: str) -> str | None:
        """Map judge output to system label, neutral to A/B naming."""
        w = winner.upper()
        if "A" in w and "B" not in w:
            return "OpenCraig"
        if "B" in w and "A" not in w:
            return "LightRAG"
        # Legacy compat: "1" / "2" from older checkpoint files
        if "1" in w and "2" not in w:
            return "OpenCraig"
        if "2" in w and "1" not in w:
            return "LightRAG"
        return None

    # Recount existing wins
    for ev in evals:
        for criterion in wins:
            winner = ev.get(criterion, {}).get("Winner", "")
            label = _tally_winner(winner)
            if label:
                wins[criterion][label] += 1

    todo = [q for q in queries if q not in existing and q in forge_map and q in light_map]
    log.info("pairwise eval: %d existing, %d remaining", len(existing), len(todo))

    for i, q in enumerate(todo):
        text_a = forge_map.get(q, "")
        text_b = light_map.get(q, "")

        if not text_a.strip() and not text_b.strip():
            continue

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

        for criterion in wins:
            winner = result.get(criterion, {}).get("Winner", "")
            label = _tally_winner(winner)
            if label:
                wins[criterion][label] += 1

        # Checkpoint
        if (i + 1) % CHECKPOINT_INTERVAL == 0:
            _save_eval_checkpoint(evals, wins, out_path, model, len(queries))
            log.info("  eval progress: %d / %d", len(evals), len(queries))

    _save_eval_checkpoint(evals, wins, out_path, model, len(queries))
    output = json.loads(out_path.read_text("utf-8"))
    return output


def _save_eval_checkpoint(evals, wins, out_path, model, total):
    summary = {}
    for criterion, counts in wins.items():
        summary[criterion] = {
            label: f"{count}/{total} ({count / max(total, 1) * 100:.1f}%)" for label, count in counts.items()
        }
    output = {
        "label_a": "OpenCraig",
        "label_b": "LightRAG",
        "model": model,
        "total_queries": total,
        "evaluated": len(evals),
        "summary": summary,
        "raw_wins": wins,
        "evaluations": evals,
    }
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), "utf-8")


def print_summary(output: dict):
    total = output["total_queries"]
    evaluated = output.get("evaluated", total)
    summary = output["summary"]

    print(f"\n{'=' * 65}")
    print(f"  UltraDomain Pairwise Evaluation  (evaluated={evaluated}/{total})")
    print("  OpenCraig (Answer 1) vs LightRAG (Answer 2)")
    print(f"  Judge: {output['model']}")
    print(f"{'=' * 65}")
    print(f"  {'Criterion':<22} {'OpenCraig':>18} {'LightRAG':>18}")
    print(f"  {'-' * 58}")
    for criterion in ("Comprehensiveness", "Diversity", "Empowerment", "Overall Winner"):
        vals = summary.get(criterion, {})
        a_val = vals.get("OpenCraig", "0/0 (0.0%)")
        b_val = vals.get("LightRAG", "0/0 (0.0%)")
        print(f"  {criterion:<22} {a_val:>18} {b_val:>18}")
    print(f"{'=' * 65}")


def print_latency_stats(forge_answers: list[dict], light_answers: list[dict]):
    """Print latency comparison."""
    forge_lats = [a["latency_ms"] for a in forge_answers if "latency_ms" in a and not a.get("error")]
    light_lats = [a["latency_ms"] for a in light_answers if "latency_ms" in a and not a.get("error")]

    if forge_lats and light_lats:
        print("\n  Latency (ms):")
        print(f"  {'':22} {'OpenCraig':>18} {'LightRAG':>18}")
        print(f"  {'-' * 58}")
        f_sorted = sorted(forge_lats)
        l_sorted = sorted(light_lats)
        print(
            f"  {'Mean':22} {sum(forge_lats) / len(forge_lats):>17.0f}ms {sum(light_lats) / len(light_lats):>17.0f}ms"
        )
        print(f"  {'Median':22} {f_sorted[len(f_sorted) // 2]:>17.0f}ms {l_sorted[len(l_sorted) // 2]:>17.0f}ms")
        print(
            f"  {'P95':22} {f_sorted[min(int(len(f_sorted) * 0.95), len(f_sorted) - 1)]:>17.0f}ms {l_sorted[min(int(len(l_sorted) * 0.95), len(l_sorted) - 1)]:>17.0f}ms"
        )
        print(
            f"  {'Answered':22} {len(forge_lats):>17}/{len(forge_answers):<3} {len(light_lats):>14}/{len(light_answers):<3}"
        )
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="OpenCraig vs LightRAG benchmark (UltraDomain)",
    )
    parser.add_argument("--domain", required=True, choices=[*DOMAINS, "all"], help="Domain to benchmark (or 'all')")
    parser.add_argument("--forgerag", default=FORGERAG_URL)
    parser.add_argument("--lightrag", default=LIGHTRAG_URL)
    parser.add_argument("--output-dir", default="./benchmark_results")
    parser.add_argument("--cache-dir", default="./benchmark_cache")

    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--skip-generate", action="store_true")
    parser.add_argument("--skip-query", action="store_true")
    parser.add_argument("--skip-eval", action="store_true")

    parser.add_argument("--eval-model", default="gpt-4o-mini")
    parser.add_argument("--query-model", default="gpt-4o-mini")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-base", default=None)

    parser.add_argument("--max-contexts", type=int, default=None)
    parser.add_argument("--max-queries", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=4, help="Concurrent query workers (default: 4)")

    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    domains = DOMAINS if args.domain == "all" else [args.domain]

    for domain in domains:
        log.info("\n" + "=" * 60)
        log.info("  DOMAIN: %s", domain)
        log.info("=" * 60)

        cache_dir = Path(args.cache_dir)
        out_dir = Path(args.output_dir) / domain
        cache_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        # ── Step 0: Download + extract ──
        log.info("=== Step 0: Download & extract ===")
        jsonl_path = download_domain(domain, cache_dir)
        contexts_path = cache_dir / f"{domain}_unique_contexts.json"
        contexts = extract_unique_contexts(jsonl_path, contexts_path)
        log.info("  %d unique contexts", len(contexts))
        if args.max_contexts:
            contexts = contexts[: args.max_contexts]

        # ── Step 1: Ingest (parallel) ──
        if not args.skip_ingest:
            log.info("=== Step 1: Ingest into both systems (parallel) ===")
            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(ingest_forgerag, contexts, args.forgerag, domain)
                f2 = pool.submit(ingest_lightrag, contexts, args.lightrag, domain)
                f1.result()
                f2.result()
        else:
            log.info("=== Step 1: Skipped (--skip-ingest) ===")

        # ── Step 2: Generate queries ──
        queries_path = out_dir / "queries.txt"
        if not args.skip_generate:
            log.info("=== Step 2: Generate queries ===")
            queries = generate_queries(
                contexts,
                queries_path,
                domain,
                model=args.query_model,
                api_key=args.api_key,
                api_base=args.api_base,
            )
        else:
            log.info("=== Step 2: Skipped (--skip-generate) ===")
            if queries_path.exists():
                queries = _extract_questions(queries_path.read_text("utf-8"))
            else:
                log.error("no cached queries at %s", queries_path)
                continue

        if args.max_queries:
            queries = queries[: args.max_queries]
        log.info("  %d queries", len(queries))

        # ── Step 3: Query both systems ──
        forge_path = out_dir / "forgerag_answers.json"
        light_path = out_dir / "lightrag_answers.json"

        if not args.skip_query:
            log.info("=== Step 3: Query both systems (parallel) ===")
            forge_answers, light_answers = query_both_parallel(
                queries,
                args.forgerag,
                args.lightrag,
                forge_path,
                light_path,
                max_workers=args.max_workers,
            )
        else:
            log.info("=== Step 3: Skipped (--skip-query) ===")
            forge_answers = _load_checkpoint(forge_path)
            light_answers = _load_checkpoint(light_path)

        # Stats
        forge_ok = sum(1 for a in forge_answers if a.get("result", "").strip())
        light_ok = sum(1 for a in light_answers if a.get("result", "").strip())
        log.info("  OpenCraig: %d/%d answered", forge_ok, len(forge_answers))
        log.info("  LightRAG: %d/%d answered", light_ok, len(light_answers))

        print_latency_stats(forge_answers, light_answers)

        # ── Step 4: Pairwise evaluation ──
        if not args.skip_eval:
            log.info("=== Step 4: Pairwise evaluation ===")
            eval_path = out_dir / "eval_results.json"
            output = pairwise_eval(
                queries,
                forge_answers,
                light_answers,
                eval_path,
                model=args.eval_model,
                api_key=args.api_key,
                api_base=args.api_base,
            )
            print_summary(output)
        else:
            log.info("=== Step 4: Skipped (--skip-eval) ===")

        log.info("Results saved to %s", out_dir)


if __name__ == "__main__":
    main()
