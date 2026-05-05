"""
Side-by-side benchmark: new agent path vs old fixed pipeline.

Drives both retrieval paths against the SAME questions on YOUR
running corpus and produces a markdown report you can read +
share. Uses the existing benchmark machinery (testset generation
from your docs, LLM-as-judge scoring) so the numbers compare
directly to anything you've previously run via /api/v1/benchmark.

Usage::

    .venv/Scripts/python.exe -m scripts.bench_agent_vs_fixed \\
        --config myconfig.yaml \\
        --num-questions 15

Output (under benchmark_results/agent_vs_fixed_<run_id>.{json,md}):
  * answer + citations + latency + tokens for both paths, per question
  * tool-call trace for the agent path (which tools, in what order,
    with what params)
  * three LLM-as-judge metrics per path: faithfulness,
    answer_relevancy, context_precision
  * delta column flagging per-question wins / regressions

Library-mode — no HTTP, no auth. Uses the same AppState the API
server builds at startup, with a synthetic auth-disabled admin
principal so the agent loop sees the full corpus.

Things this script DOES NOT exercise:
  * web_search tool — provider isn't wired on AppState yet, so the
    agent will get an "error: web search not configured" if it tries.
    The agent's system prompt steers it away from web_search unless
    the question is genuinely off-corpus, so this rarely fires.
  * Streaming — uses ``AgentLoop.run`` (not ``stream``) since this
    is offline benchmarking; the SSE event sequence is covered by
    test_agent_stream.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Ensure the repo root is on sys.path when invoked as a script.
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from api.agent import (
    AgentConfig,
    AgentLoop,
    LiteLLMClient,
    build_tool_context,
)
from api.auth import AuthenticatedPrincipal
from api.state import AppState
from benchmark.metrics import score_items
from benchmark.runner import BenchmarkItem
from benchmark.testset import generate_testset
from config import load_config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Run the new agent path and the old fixed pipeline against the "
            "same questions, score both, and write a side-by-side markdown "
            "report."
        )
    )
    p.add_argument(
        "--config",
        default="myconfig.yaml",
        help="Path to OpenCraig YAML config (default: myconfig.yaml).",
    )
    p.add_argument(
        "--num-questions",
        type=int,
        default=15,
        help="Number of questions to auto-generate from your corpus.",
    )
    p.add_argument(
        "--questions",
        type=str,
        default=None,
        help=(
            "Optional JSON file with a precomputed question list "
            "[{question, ground_truth?, doc_id?, doc_title?}, ...]. "
            "Skips test-set generation."
        ),
    )
    p.add_argument(
        "--out-dir",
        default="benchmark_results",
        help="Where to write the JSON + markdown reports.",
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="Override run id (default: auto-generated).",
    )
    p.add_argument(
        "--skip-scoring",
        action="store_true",
        help="Run both paths but skip the LLM-as-judge scoring step.",
    )
    p.add_argument(
        "--mode",
        choices=("both", "agent", "fixed"),
        default="both",
        help="Which path(s) to run (both = side-by-side comparison).",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# AppState helpers
# ---------------------------------------------------------------------------


def _wire_agent_deps(state: AppState) -> None:
    """Attach the agent-specific dependencies that AppState doesn't
    auto-wire yet. Idempotent.

    * ``state.reranker`` — currently owned by RetrievalPipeline. The
      agent's ``rerank`` tool reads ``state.reranker`` directly, so
      we expose it on the AppState before the loop starts.
    * ``state.web_search_provider`` / ``state.web_search_cache`` —
      not configured in single-user dev. Set to None so the agent's
      web_search handler returns a clean DispatchError if it tries.
    """
    # Reach into the (lazily-built) retrieval pipeline to pick up
    # its reranker. Same instance the fixed pipeline uses, so
    # benchmark comparisons are apples-to-apples.
    try:
        retr = state.retrieval  # property — triggers lazy build
        state.reranker = getattr(retr, "reranker", None)
    except Exception:
        log.warning("could not attach reranker to AppState; rerank tool will fail")
        state.reranker = None

    if not hasattr(state, "web_search_provider"):
        state.web_search_provider = None
    if not hasattr(state, "web_search_cache"):
        state.web_search_cache = None


def _build_agent(state: AppState) -> AgentLoop:
    """Construct an AgentLoop using the same model + key the
    answering pipeline is configured with — fairest comparison."""
    gen = state.cfg.answering.generator
    cfg = AgentConfig(
        model=gen.model,
        api_key=gen.api_key or os.environ.get("ANTHROPIC_API_KEY"),
        api_base=gen.api_base,
    )
    llm = LiteLLMClient(
        model=cfg.model,
        api_key=cfg.api_key,
        api_base=cfg.api_base,
    )
    return AgentLoop(cfg, llm)


def _admin_principal() -> AuthenticatedPrincipal:
    """Synthetic auth-disabled admin — sees the full corpus,
    benchmark questions can hit any doc."""
    return AuthenticatedPrincipal(
        user_id="local",
        username="bench-runner",
        role="admin",
        via="auth_disabled",
    )


# ---------------------------------------------------------------------------
# Per-question runners
# ---------------------------------------------------------------------------


def _run_fixed(state: AppState, question: str) -> dict:
    t0 = time.time()
    try:
        ans = state.answering.ask(question)
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "latency_ms": int((time.time() - t0) * 1000),
        }
    contexts: list[str] = []
    for c in getattr(ans, "citations_all", []) or []:
        snip = getattr(c, "snippet", "") or ""
        if snip:
            contexts.append(snip)
    # Pull KG synthesis context too — old pipeline injects it into
    # the prompt; the judge needs to see it for fair faithfulness
    # scoring.
    kg_ctx = (getattr(ans, "stats", None) or {}).get("kg_context") or {}
    for e in kg_ctx.get("entities", []) or []:
        desc = (e.get("description") or "").strip()
        if desc:
            contexts.append(f"[KG] {e.get('name', '')}: {desc}")
    return {
        "ok": True,
        "answer": getattr(ans, "text", "") or "",
        "contexts": contexts,
        "citations": [
            {
                "citation_id": getattr(c, "citation_id", None),
                "doc_id": getattr(c, "doc_id", None),
                "page_no": getattr(c, "page_no", None),
                "snippet": (getattr(c, "snippet", "") or "")[:200],
            }
            for c in (getattr(ans, "citations_used", []) or [])
        ],
        "latency_ms": int((time.time() - t0) * 1000),
    }


def _run_agent(
    agent: AgentLoop, state: AppState, question: str
) -> dict:
    ctx = build_tool_context(state, _admin_principal())
    try:
        result = agent.run(question, ctx)
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }
    return {
        "ok": True,
        "answer": result.answer,
        "stop_reason": result.stop_reason,
        "iterations": result.iterations,
        "tool_calls_count": result.tool_calls_count,
        "latency_ms": result.total_latency_ms,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "tool_calls_log": list(ctx.tool_calls_log),
        "citations": [
            {
                "chunk_id": c.get("chunk_id"),
                "doc_id": c.get("doc_id"),
                "page": c.get("page_start"),
                "snippet": (c.get("content") or "")[:200],
                "score": c.get("score"),
                "sources": c.get("sources"),
            }
            for c in result.citations
        ],
        "contexts": [
            (c.get("content") or "")[:1000]
            for c in result.citations
        ],
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score(records: list[dict], side: str, cfg) -> None:
    """Score one path's records in place. Mutates each record's
    ``side`` dict to add faithfulness / relevancy / context_precision.
    """
    items: list[BenchmarkItem] = []
    item_to_rec: dict[int, dict] = {}
    idx = 0
    for rec in records:
        side_rec = rec.get(side)
        if not side_rec or not side_rec.get("ok"):
            continue
        bi = BenchmarkItem(
            idx=idx,
            question=rec["question"],
            ground_truth=rec.get("ground_truth", ""),
            answer=side_rec.get("answer", ""),
            contexts=side_rec.get("contexts") or [],
        )
        items.append(bi)
        item_to_rec[idx] = side_rec
        idx += 1
    if not items:
        log.warning("no scoreable items for %s side", side)
        return
    score_items(items=items, cfg=cfg)
    for bi in items:
        side_rec = item_to_rec[bi.idx]
        side_rec["faithfulness"] = bi.faithfulness
        side_rec["relevancy"] = bi.relevancy
        side_rec["context_precision"] = bi.context_precision


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _aggregate_scores(records: list[dict], side: str) -> dict[str, float | None]:
    vals_f: list[float] = []
    vals_r: list[float] = []
    vals_cp: list[float] = []
    for rec in records:
        s = rec.get(side) or {}
        if s.get("faithfulness") is not None:
            vals_f.append(float(s["faithfulness"]))
        if s.get("relevancy") is not None:
            vals_r.append(float(s["relevancy"]))
        if s.get("context_precision") is not None:
            vals_cp.append(float(s["context_precision"]))

    def _avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 4) if xs else None

    return {
        "faithfulness": _avg(vals_f),
        "answer_relevancy": _avg(vals_r),
        "context_precision": _avg(vals_cp),
        "n_scored": len(vals_f),
    }


def _aggregate_latency(records: list[dict], side: str) -> dict[str, Any]:
    lats = [
        rec[side]["latency_ms"]
        for rec in records
        if rec.get(side, {}).get("ok")
    ]
    if not lats:
        return {"n": 0}
    lats_sorted = sorted(lats)
    return {
        "n": len(lats),
        "avg_ms": round(sum(lats) / len(lats), 1),
        "median_ms": lats_sorted[len(lats_sorted) // 2],
        "p95_ms": lats_sorted[max(0, int(len(lats_sorted) * 0.95) - 1)],
        "max_ms": max(lats),
    }


def _aggregate_agent_tools(records: list[dict]) -> dict[str, Any]:
    tool_counts: dict[str, int] = {}
    direct_answers = 0
    total_calls = 0
    iter_counts: list[int] = []
    stop_reasons: dict[str, int] = {}
    for rec in records:
        a = rec.get("agent") or {}
        if not a.get("ok"):
            continue
        n_calls = a.get("tool_calls_count", 0)
        total_calls += n_calls
        if n_calls == 0:
            direct_answers += 1
        for entry in a.get("tool_calls_log") or []:
            t = entry.get("tool")
            if t:
                tool_counts[t] = tool_counts.get(t, 0) + 1
        iter_counts.append(a.get("iterations", 0))
        sr = a.get("stop_reason", "?")
        stop_reasons[sr] = stop_reasons.get(sr, 0) + 1
    return {
        "direct_answer_share": (
            round(direct_answers / max(len(iter_counts), 1), 3)
            if iter_counts else None
        ),
        "avg_tool_calls": (
            round(total_calls / max(len(iter_counts), 1), 2)
            if iter_counts else None
        ),
        "avg_iterations": (
            round(sum(iter_counts) / max(len(iter_counts), 1), 2)
            if iter_counts else None
        ),
        "tool_usage": tool_counts,
        "stop_reasons": stop_reasons,
    }


def _md_table(rows: list[list[str]], headers: list[str]) -> str:
    out = ["| " + " | ".join(headers) + " |"]
    out.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def _render_report(
    *,
    run_id: str,
    records: list[dict],
    fixed_scores: dict[str, Any],
    agent_scores: dict[str, Any],
    fixed_lat: dict[str, Any],
    agent_lat: dict[str, Any],
    agent_tool_stats: dict[str, Any],
    config_snapshot: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append(f"# Agent vs Fixed Pipeline — Benchmark `{run_id}`")
    lines.append("")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Questions: {len(records)}")
    lines.append("")

    # ── Headline metrics ──
    lines.append("## Quality (LLM-as-judge, higher is better)")
    lines.append("")
    rows = []
    for k in ("faithfulness", "answer_relevancy", "context_precision"):
        f = fixed_scores.get(k)
        a = agent_scores.get(k)
        if f is None and a is None:
            continue
        delta = (
            f"{a - f:+.4f}" if (f is not None and a is not None) else "—"
        )
        rows.append([
            k,
            "—" if f is None else f"{f:.4f}",
            "—" if a is None else f"{a:.4f}",
            delta,
        ])
    if rows:
        lines.append(_md_table(rows, ["metric", "fixed", "agent", "Δ (agent − fixed)"]))
    else:
        lines.append("_no scored items — re-run without `--skip-scoring`_")
    lines.append("")

    # ── Latency ──
    lines.append("## Latency")
    lines.append("")
    rows = []
    for k in ("n", "avg_ms", "median_ms", "p95_ms", "max_ms"):
        rows.append([
            k,
            fixed_lat.get(k, "—"),
            agent_lat.get(k, "—"),
        ])
    lines.append(_md_table(rows, ["", "fixed", "agent"]))
    lines.append("")

    # ── Agent tool usage ──
    lines.append("## Agent Tool Usage")
    lines.append("")
    lines.append(
        f"Direct-answer share: **{agent_tool_stats.get('direct_answer_share')}** "
        f"(0 tool calls = LLM answered straight off intent recognition)"
    )
    lines.append(
        f"Avg tool calls per query: **{agent_tool_stats.get('avg_tool_calls')}**"
    )
    lines.append(
        f"Avg LLM iterations per query: **{agent_tool_stats.get('avg_iterations')}**"
    )
    lines.append("")
    lines.append("Tool invocation counts:")
    rows = sorted(
        ((t, n) for t, n in (agent_tool_stats.get("tool_usage") or {}).items()),
        key=lambda kv: -kv[1],
    )
    if rows:
        lines.append(_md_table([[t, n] for t, n in rows], ["tool", "calls"]))
    else:
        lines.append("_no tool calls recorded_")
    lines.append("")
    lines.append("Stop reasons:")
    sr_rows = sorted(
        ((r, n) for r, n in (agent_tool_stats.get("stop_reasons") or {}).items()),
        key=lambda kv: -kv[1],
    )
    if sr_rows:
        lines.append(_md_table([[r, n] for r, n in sr_rows], ["stop_reason", "n"]))
    lines.append("")

    # ── Per-question detail ──
    lines.append("## Per-Question Detail")
    lines.append("")
    for i, rec in enumerate(records, start=1):
        q = rec["question"]
        lines.append(f"### Q{i}. {q}")
        lines.append("")
        if rec.get("doc_title"):
            lines.append(f"_source doc: {rec['doc_title']}_")
            lines.append("")
        if rec.get("ground_truth"):
            lines.append(f"**Ground truth:** {rec['ground_truth']}")
            lines.append("")

        for side, label in (("fixed", "Fixed pipeline"), ("agent", "Agent")):
            s = rec.get(side) or {}
            if not s:
                continue
            lines.append(f"**{label}**" + (
                f" · {s.get('latency_ms', '?')}ms" if s.get("ok") else ""
            ))
            if not s.get("ok"):
                lines.append(f"\n> ❌ error: `{s.get('error', 'unknown')}`\n")
                continue
            scores_bits = []
            if s.get("faithfulness") is not None:
                scores_bits.append(f"F={s['faithfulness']:.2f}")
            if s.get("relevancy") is not None:
                scores_bits.append(f"R={s['relevancy']:.2f}")
            if s.get("context_precision") is not None:
                scores_bits.append(f"CP={s['context_precision']:.2f}")
            if scores_bits:
                lines.append(" · ".join(scores_bits))
            lines.append("")
            lines.append(f"> {s.get('answer', '')[:600]}{'…' if len(s.get('answer', '')) > 600 else ''}")
            lines.append("")
            if side == "agent":
                tcl = s.get("tool_calls_log") or []
                if tcl:
                    summary_bits = [
                        f"`{c['tool']}`({c.get('latency_ms', '?')}ms)"
                        for c in tcl
                    ]
                    lines.append(
                        f"_tools: {' → '.join(summary_bits)} · "
                        f"{s.get('iterations', '?')} iter · "
                        f"{s.get('stop_reason')}_"
                    )
                else:
                    lines.append(f"_no tools — direct answer · {s.get('stop_reason')}_")
                lines.append("")
            cits = s.get("citations") or []
            if cits:
                lines.append(f"<details><summary>citations ({len(cits)})</summary>\n")
                for c in cits[:6]:
                    snip = (c.get("snippet") or "").replace("\n", " ")[:160]
                    lines.append(f"- `{c.get('chunk_id') or c.get('citation_id')}` p{c.get('page') or c.get('page_no')}: {snip}")
                if len(cits) > 6:
                    lines.append(f"- … {len(cits) - 6} more")
                lines.append("\n</details>")
                lines.append("")
        lines.append("---")
        lines.append("")

    # ── Config snapshot ──
    lines.append("## Config Snapshot")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(config_snapshot, indent=2, default=str))
    lines.append("```")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    state = AppState(cfg)
    _wire_agent_deps(state)

    # ── Collect questions ──
    if args.questions:
        questions_data = json.loads(Path(args.questions).read_text(encoding="utf-8"))
        log.info("loaded %d questions from %s", len(questions_data), args.questions)
    else:
        log.info("generating %d questions from corpus…", args.num_questions)
        items = generate_testset(
            store=state.store, cfg=cfg, num_questions=args.num_questions
        )
        questions_data = [
            {
                "question": it.question,
                "ground_truth": it.ground_truth,
                "doc_id": it.doc_id,
                "doc_title": it.doc_title,
            }
            for it in items
        ]
        log.info("generated %d questions", len(questions_data))

    # ── Build agent loop once ──
    agent = _build_agent(state) if args.mode in ("both", "agent") else None

    # ── Run each question through both paths ──
    records: list[dict] = []
    for i, q in enumerate(questions_data, start=1):
        question = q["question"]
        log.info("[%d/%d] %s", i, len(questions_data), question[:80])
        rec = {
            "question": question,
            "ground_truth": q.get("ground_truth", ""),
            "doc_id": q.get("doc_id", ""),
            "doc_title": q.get("doc_title", ""),
        }
        if args.mode in ("both", "fixed"):
            rec["fixed"] = _run_fixed(state, question)
            if rec["fixed"].get("ok"):
                log.info("  fixed: %dms", rec["fixed"]["latency_ms"])
            else:
                log.warning("  fixed: %s", rec["fixed"].get("error"))
        if args.mode in ("both", "agent") and agent is not None:
            rec["agent"] = _run_agent(agent, state, question)
            if rec["agent"].get("ok"):
                log.info(
                    "  agent: %dms · %d tools · %d iters · %s",
                    rec["agent"]["latency_ms"],
                    rec["agent"]["tool_calls_count"],
                    rec["agent"]["iterations"],
                    rec["agent"]["stop_reason"],
                )
            else:
                log.warning("  agent: %s", rec["agent"].get("error"))
        records.append(rec)

    # ── Score ──
    if not args.skip_scoring:
        for side in ("fixed", "agent"):
            if args.mode in ("both", side):
                log.info("scoring %s side…", side)
                _score(records, side, cfg)

    # ── Aggregate ──
    fixed_scores = _aggregate_scores(records, "fixed")
    agent_scores = _aggregate_scores(records, "agent")
    fixed_lat = _aggregate_latency(records, "fixed")
    agent_lat = _aggregate_latency(records, "agent")
    agent_tool_stats = _aggregate_agent_tools(records)

    # ── Save ──
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or uuid.uuid4().hex[:12]
    json_path = out_dir / f"agent_vs_fixed_{run_id}.json"
    md_path = out_dir / f"agent_vs_fixed_{run_id}.md"

    config_snapshot = {
        "model": cfg.answering.generator.model,
        "judge_model": (
            getattr(getattr(cfg, "benchmark", None), "model", None)
            or cfg.answering.generator.model
        ),
        "num_questions": len(records),
        "mode": args.mode,
    }

    json_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "config": config_snapshot,
                "scores": {
                    "fixed": fixed_scores,
                    "agent": agent_scores,
                },
                "latency": {
                    "fixed": fixed_lat,
                    "agent": agent_lat,
                },
                "agent_tool_stats": agent_tool_stats,
                "items": records,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    md = _render_report(
        run_id=run_id,
        records=records,
        fixed_scores=fixed_scores,
        agent_scores=agent_scores,
        fixed_lat=fixed_lat,
        agent_lat=agent_lat,
        agent_tool_stats=agent_tool_stats,
        config_snapshot=config_snapshot,
    )
    md_path.write_text(md, encoding="utf-8")

    log.info("wrote %s + %s", json_path, md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
