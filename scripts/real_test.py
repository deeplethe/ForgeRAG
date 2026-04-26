"""
Real end-to-end test with a live LLM.

    python scripts/real_test.py --limit 5

Runs the same pipeline as smoke_test but with a REAL embedder and
REAL generator (via litellm). Requires OPENAI_API_KEY (or whatever
your config points at).

Differences from smoke_test:
    - Uses litellm for embedding (real vectors, real cosine search)
    - Uses litellm for answer generation (real LLM output)
    - Stores data in ./storage/ (persistent, so you can inspect it
      afterwards with sqlite3 or the API)
    - Asks multiple queries so you can see how structure-aware
      retrieval compares to what you'd expect

Set env vars before running:
    export OPENAI_API_KEY=sk-...
    python scripts/real_test.py --limit 5

Or use a custom config:
    python scripts/real_test.py --config forgerag.yaml --limit 5
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from answering.generator import make_generator
from answering.pipeline import AnsweringPipeline
from config import (
    AppConfig,
    LocalStorageModel,
    RelationalConfig,
    SQLiteConfig,
    StorageModel,
    load_config,
)
from embedder.base import make_embedder
from ingestion import IngestionPipeline
from parser.blob_store import make_blob_store
from parser.chunker import Chunker
from parser.pipeline import ParserPipeline
from parser.tree_builder import TreeBuilder
from persistence.files import FileStore
from persistence.store import Store
from persistence.vector.base import make_vector_store
from retrieval.pipeline import RetrievalPipeline, build_bm25_index

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------


def _c(text, color):
    if not sys.stdout.isatty():
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "dim": "2", "bold": "1", "cyan": "36", "magenta": "35"}
    return f"\033[{codes.get(color, '0')}m{text}\033[0m"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def build_env(cfg: AppConfig):
    rel = Store(cfg.persistence.relational)
    rel.connect()
    rel.ensure_schema()

    blob = make_blob_store(cfg.storage.to_dataclass())
    file_store = FileStore(cfg.files, blob, rel)

    embedder = make_embedder(cfg.embedder)

    # Vector store
    if cfg.persistence.vector.backend == "pgvector" and cfg.persistence.relational.backend != "postgres":
        # Auto-fallback to chromadb
        from config import ChromaConfig, VectorConfig

        cfg.persistence.vector = VectorConfig(
            backend="chromadb",
            chromadb=ChromaConfig(
                persist_directory="./storage/chroma",
                dimension=cfg.embedder.dimension,
            ),
        )
    vec = make_vector_store(cfg.persistence.vector, relational_store=rel)
    vec.connect()
    vec.ensure_schema()

    parser = ParserPipeline.from_config(cfg)
    tree_builder = TreeBuilder(cfg.parser.tree_builder)
    chunker = Chunker(cfg.parser.chunker)

    pipeline = IngestionPipeline(
        file_store=file_store,
        parser=parser,
        tree_builder=tree_builder,
        chunker=chunker,
        relational_store=rel,
        vector_store=vec,
        embedder=embedder,
    )
    return rel, vec, blob, embedder, pipeline


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


DEFAULT_QUERIES = [
    "What is the main contribution or novelty of this paper?",
    "What datasets or benchmarks are used for evaluation?",
    "What are the key results and how do they compare to baselines?",
    "Describe the proposed method or architecture in detail.",
    "What are the limitations mentioned by the authors?",
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(
        description="Real end-to-end test with live LLM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dir", type=Path, default=_ROOT / "tests" / "pdfs")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--queries", nargs="*", default=None, help="Custom queries. If omitted, uses 5 built-in ones.")
    p.add_argument("--skip-ingest", action="store_true", help="Skip ingestion (reuse existing data in ./storage/)")
    p.add_argument("--skip-summary", action="store_true", help="Skip Phase 2b summary enrichment (slow with many docs)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname).1s %(name)s: %(message)s",
    )

    # --- Resolve config ---
    # Precedence: --config > $FORGERAG_CONFIG > ./forgerag.yaml > examples/forgerag.dev.yaml > hardcoded fallback
    if args.config:
        cfg = load_config(args.config)
        print(f"  config:     {args.config}")
    elif os.environ.get("FORGERAG_CONFIG"):
        cfg = load_config(os.environ["FORGERAG_CONFIG"])
        print(f"  config:     {os.environ['FORGERAG_CONFIG']} (from $FORGERAG_CONFIG)")
    elif (_ROOT / "forgerag.yaml").exists():
        cfg = load_config(_ROOT / "forgerag.yaml")
        print(f"  config:     {_ROOT / 'forgerag.yaml'}")
    elif (_ROOT / "examples" / "forgerag.dev.yaml").exists():
        cfg = load_config(_ROOT / "examples" / "forgerag.dev.yaml")
        print(f"  config:     {_ROOT / 'examples' / 'forgerag.dev.yaml'} (dev fallback)")
    else:
        print("  config:     (hardcoded minimal defaults)")
        cfg = AppConfig()
        cfg.persistence.relational = RelationalConfig(
            backend="sqlite",
            sqlite=SQLiteConfig(path="./storage/real_test.db"),
        )
        cfg.storage = StorageModel(
            mode="local",
            local=LocalStorageModel(root="./storage/blobs"),
        )

    print(_c(f"\n{'=' * 70}", "dim"))
    print(_c(" Real end-to-end test (live LLM)", "bold"))
    print(_c(f"{'=' * 70}", "dim"))
    print(f"  embedder:   {cfg.embedder.backend} / dim={cfg.embedder.dimension}")
    print(f"  generator:  {cfg.answering.generator.model}")
    print(f"  relational: {cfg.persistence.relational.backend}")
    print(f"  vector:     {cfg.persistence.vector.backend}")
    print()

    rel, vec, _blob, embedder, pipeline = build_env(cfg)

    try:
        # --- Phase 1: Ingest ---
        if not args.skip_ingest:
            pdfs = sorted(args.dir.glob("*.pdf"))[: args.limit]
            if not pdfs:
                print(f"no PDFs in {args.dir}")
                return 2

            print(_c(f"Phase 1: Ingest ({len(pdfs)} PDFs)", "cyan"))
            for i, pdf in enumerate(pdfs, 1):
                t0 = time.time()
                try:
                    r = pipeline.upload_and_ingest(
                        pdf,
                        original_name=pdf.name,
                        mime_type="application/pdf",
                    )
                    ms = int((time.time() - t0) * 1000)
                    print(
                        f"  [{i:>2}/{len(pdfs)}] {_c('OK', 'green')}   {pdf.name[:35]:<35} "
                        f"{ms:>5}ms  blocks={r.num_blocks:>3} chunks={r.num_chunks:>3}"
                    )
                except Exception as e:
                    ms = int((time.time() - t0) * 1000)
                    print(f"  [{i:>2}/{len(pdfs)}] {_c('FAIL', 'red')} {pdf.name[:35]:<35} {ms:>5}ms  {e}")
                    if args.verbose:
                        traceback.print_exc()
            print()
        else:
            print(_c("Phase 1: Skipped (--skip-ingest)", "cyan"))
            print()

        # --- Phase 2: Stats ---
        doc_ids = rel.list_document_ids()
        if not doc_ids:
            print(_c("  no documents in store. Run without --skip-ingest first.", "yellow"))
            return 1

        total_chunks = 0
        for d in doc_ids:
            doc = rel.get_document(d)
            if doc:
                total_chunks += len(rel.get_chunks(d, doc["active_parse_version"]))

        print(_c("Phase 2: Index stats", "cyan"))
        print(f"  documents:  {len(doc_ids)}")
        print(f"  chunks:     {total_chunks}")
        print()

        # --- Phase 2b: Enrich tree summaries ---
        print()
        if args.skip_summary:
            print(_c("Phase 2b: Skipped (--skip-summary)", "cyan"))
        else:
            print(_c("Phase 2b: Enrich tree summaries (LLM)", "cyan"))
            try:
                from parser.summary import enrich_tree_summaries, make_summary_fn
                from persistence.serde import tree_from_dict, tree_to_dict

                gen_cfg = cfg.answering.generator
                summary_fn = make_summary_fn(
                    model=gen_cfg.model,
                    api_key=gen_cfg.api_key,
                    api_key_env=gen_cfg.api_key_env,
                    api_base=gen_cfg.api_base,
                )
                enriched_total = 0
                failed_docs = 0
                skipped_docs = 0
                for di, d in enumerate(doc_ids, 1):
                    doc_row = rel.get_document(d)
                    if not doc_row:
                        continue
                    pv = doc_row["active_parse_version"]
                    tree_dict = rel.load_tree(d, pv)
                    if not tree_dict:
                        continue

                    # Quick check: are all nodes already summarized?
                    tree_obj = tree_from_dict(tree_dict)
                    unsummarized = sum(1 for n in tree_obj.walk_preorder() if not n.summary)
                    if unsummarized == 0:
                        skipped_docs += 1
                        continue

                    # Need ParsedDocument for block text — reconstruct minimally
                    from persistence.serde import row_to_block

                    block_rows = rel.get_blocks(d, pv)
                    blocks = [row_to_block(r) for r in block_rows]
                    from parser.schema import DocFormat, DocProfile, ParsedDocument, ParseTrace

                    mini_doc = ParsedDocument(
                        doc_id=d,
                        filename="",
                        format=DocFormat.PDF,
                        parse_version=pv,
                        profile=DocProfile(
                            page_count=0,
                            format=DocFormat.PDF,
                            file_size_bytes=0,
                        ),
                        parse_trace=ParseTrace(),
                        pages=[],
                        blocks=blocks,
                    )

                    n, fails = enrich_tree_summaries(
                        tree_obj,
                        mini_doc,
                        generate_fn=summary_fn,
                        max_failures=2,
                    )
                    if n > 0:
                        rel.save_tree(
                            doc_id=d,
                            parse_version=pv,
                            root_id=tree_obj.root_id,
                            quality_score=tree_obj.quality_score,
                            generation_method=tree_obj.generation_method,
                            tree_json=tree_to_dict(tree_obj),
                        )
                    enriched_total += n
                    if fails > 0:
                        failed_docs += 1
                    # Progress
                    if di % 10 == 0 or di == len(doc_ids):
                        print(
                            f"    [{di}/{len(doc_ids)}] summarized={enriched_total} failed_docs={failed_docs} skipped={skipped_docs}"
                        )
                print(
                    f"  done: {enriched_total} nodes summarized, {failed_docs} docs with failures, {skipped_docs} already done"
                )
            except Exception as e:
                print(f"  summary enrichment skipped: {e}")

        # --- Phase 3: BM25 ---
        print()
        print(_c("Phase 3: Build BM25", "cyan"))
        t0 = time.time()
        bm25 = build_bm25_index(rel, cfg.retrieval.bm25)
        print(f"  indexed {len(bm25)} chunks in {int((time.time() - t0) * 1000)}ms")
        print()

        # --- Phase 4: Retrieval + Answer ---
        # Build LLM tree navigator if configured
        tree_nav = None
        tp = cfg.retrieval.tree_path
        if tp.llm_nav_enabled:
            from retrieval.tree_navigator import LLMTreeNavigator

            tree_nav = LLMTreeNavigator(
                model=tp.nav.model,
                api_key=tp.nav.api_key or cfg.answering.generator.api_key,
                api_key_env=tp.nav.api_key_env or cfg.answering.generator.api_key_env,
                api_base=tp.nav.api_base or cfg.answering.generator.api_base,
                max_nodes=tp.nav.max_nodes,
            )
            print(f"  tree_nav:     LLM ({tp.nav.model})")
        else:
            print("  tree_nav:     BM25 fallback")

        retrieval = RetrievalPipeline(
            cfg.retrieval,
            embedder=embedder,
            vector_store=vec,
            relational_store=rel,
            bm25_index=bm25,
            tree_navigator=tree_nav,
        )
        generator = make_generator(cfg.answering.generator)
        answering = AnsweringPipeline(
            cfg.answering,
            retrieval=retrieval,
            generator=generator,
        )

        queries = args.queries or DEFAULT_QUERIES
        print(_c(f"Phase 4: Ask {len(queries)} questions (real LLM)", "cyan"))
        print()

        for qi, query in enumerate(queries, 1):
            print(_c(f"  Q{qi}: {query}", "bold"))
            t0 = time.time()
            try:
                answer = answering.ask(query)
                ms = int((time.time() - t0) * 1000)

                print(f"  {_c('A:', 'green')} {answer.text[:500]}")
                if len(answer.text) > 500:
                    print(f"     ... ({len(answer.text)} chars total)")
                print()

                # Citations detail
                if answer.citations_used:
                    print(f"  {_c('Citations used:', 'dim')}")
                    for c in answer.citations_used[:5]:
                        hl = c.highlights[0] if c.highlights else None
                        page = f"p{hl.page_no}" if hl else "?"
                        bbox = (
                            (f"bbox=({hl.bbox[0]:.0f},{hl.bbox[1]:.0f},{hl.bbox[2]:.0f},{hl.bbox[3]:.0f})")
                            if hl
                            else ""
                        )
                        print(f"    {c.citation_id}  {page} {bbox}")
                        print(f"      {_c(c.snippet[:100], 'dim')}")
                    print()

                # Sources breakdown
                sources = {}
                for m in answer.citations_all:
                    sources[m.citation_id] = "retrieved"
                for m in answer.citations_used:
                    sources[m.citation_id] = "used"

                # Show expanded queries if present
                eq = answer.stats.get("retrieval", {}).get("expanded_queries")
                if eq and len(eq) > 1:
                    print(f"  {_c('Expanded queries:', 'dim')}")
                    for i, eq_q in enumerate(eq):
                        tag = "(original)" if i == 0 else f"(variant {i})"
                        print(f"    {tag} {eq_q}")
                    print()

                print(
                    f"  {_c('Stats:', 'dim')} {ms}ms | "
                    f"ctx_chunks={answer.stats.get('context_chunks', '?')} | "
                    f"finish={answer.finish_reason} | "
                    f"model={answer.model}"
                )
                if answer.stats.get("usage"):
                    u = answer.stats["usage"]
                    print(
                        f"         tokens: prompt={u.get('prompt_tokens', '?')} "
                        f"completion={u.get('completion_tokens', '?')}"
                    )
                ret_stats = answer.stats.get("retrieval", {})
                if ret_stats:
                    print(
                        f"         retrieval: vec={ret_stats.get('vector_hits', '?')} "
                        f"tree={ret_stats.get('tree_hits', '?')} "
                        f"merged={ret_stats.get('merged_count', '?')} "
                        f"vec_docs={ret_stats.get('vector_doc_ids', '?')}"
                    )
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                print(f"  {_c('ERROR:', 'red')} {e} ({ms}ms)")
                if args.verbose:
                    traceback.print_exc()

            # Show trace_id (trace is persisted to DB by AnsweringPipeline)
            trace_data = answer.stats.get("retrieval", {}).get("trace", {})
            answer.stats.get("trace_id")
            if trace_data:
                print(
                    f"  {_c('Trace:', 'dim')} total_llm_calls={trace_data.get('total_llm_calls', '?')} "
                    f"total_llm_ms={trace_data.get('total_llm_ms', '?')}"
                )

            print(_c("  " + "─" * 66, "dim"))
            print()

        # --- Summary ---
        print(_c(f"{'=' * 70}", "dim"))
        print(_c(" Test complete", "bold"))
        print(f"  documents: {len(doc_ids)}  chunks: {total_chunks}")
        print(f"  queries:   {len(queries)}")
        print("  tip: run with --skip-ingest to re-query without re-parsing")
        print("  tip: inspect DB: sqlite3 ./storage/real_test.db")
        print(_c(f"{'=' * 70}\n", "dim"))

    finally:
        rel.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
