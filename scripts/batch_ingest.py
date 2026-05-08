"""
Batch-ingest every supported file under a directory.

Minimum example:
    python scripts/batch_ingest.py /path/to/pdfs

Full example:
    python scripts/batch_ingest.py ./papers \\
        --db ./storage/opencraig.db \\
        --blob ./storage/blobs \\
        --workers 4 \\
        --embed \\
        --extensions pdf,docx

Behavior:
    - Defaults to SQLite + local blob store, no external deps needed.
    - Walks the target directory recursively, filters by extension.
    - Runs IngestionPipeline.upload_and_ingest per file.
    - Per-file errors are logged and counted; the script keeps going.
    - Content-hash dedup is automatic: uploading the same file twice
      produces two file rows but only one blob.
    - Use `--dry-run` to preview the file list without ingesting.
    - Use `--embed` to compute and store vectors (requires a working
      embedder config; by default vectors are skipped and BM25
      retrieval alone will still work).
"""

from __future__ import annotations

import argparse
import logging
import mimetypes
import os
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Let the script run from the repo root without install.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from config import (
    AppConfig,
    FilesConfig,
    LocalStorageModel,
    RelationalConfig,
    SQLiteConfig,
    StorageModel,
    load_config,
)
from ingestion import IngestionPipeline
from parser.chunker import Chunker
from parser.pipeline import ParserPipeline
from parser.tree_builder import TreeBuilder
from persistence.files import FileStore
from persistence.store import Store

log = logging.getLogger("batch_ingest")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


DEFAULT_EXTENSIONS = ("pdf", "docx", "pptx", "xlsx", "html", "htm", "png", "jpg", "jpeg")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch-ingest files into OpenCraig.",
    )
    p.add_argument(
        "directory",
        type=Path,
        help="Source directory to walk recursively.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to opencraig.yaml. If omitted, uses OPENCRAIG_CONFIG env "
        "var or falls back to --db / --blob CLI defaults.",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=Path("./storage/opencraig.db"),
        help="SQLite database path when no --config is provided.",
    )
    p.add_argument(
        "--blob",
        type=Path,
        default=Path("./storage/blobs"),
        help="Local blob store root when no --config is provided.",
    )
    p.add_argument(
        "--extensions",
        type=str,
        default=",".join(DEFAULT_EXTENSIONS),
        help="Comma-separated extensions to ingest. Default covers common formats.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel workers (default 1). Parsing dominates runtime; "
        "raise this only if you have a strong CPU.",
    )
    p.add_argument(
        "--embed",
        action="store_true",
        help="Compute and store embeddings. Requires embedder config (litellm API key or local sentence-transformers).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List files without ingesting.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N matching files (useful for testing).",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files whose content hash is already in the files table.",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose per-file logging.",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def iter_files(root: Path, extensions: Iterable[str]) -> Iterable[Path]:
    ext_set = {e.lower().lstrip(".") for e in extensions}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower().lstrip(".")
        if ext in ext_set:
            yield p


def guess_mime(path: Path) -> str:
    guess, _ = mimetypes.guess_type(str(path))
    return guess or "application/octet-stream"


# ---------------------------------------------------------------------------
# Pipeline builder
# ---------------------------------------------------------------------------


def _resolve_config(args: argparse.Namespace) -> AppConfig:
    """
    Precedence (highest first):
        1. --config path
        2. OPENCRAIG_CONFIG env var
        3. CLI flags (--db / --blob) layered on top of defaults
    """
    path: Path | None = args.config
    if path is None:
        env_path = os.environ.get("OPENCRAIG_CONFIG")
        if env_path:
            path = Path(env_path)

    if path is not None:
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")
        print(f"loaded config: {path}")
        return load_config(path)

    # Fallback: build a minimal SQLite + local blob config from CLI flags.
    cfg = AppConfig()
    cfg.persistence.relational = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(args.db)),
    )
    cfg.storage = StorageModel(
        mode="local",
        local=LocalStorageModel(
            root=str(args.blob),
            public_base_url=None,
        ),
    )
    cfg.files = FilesConfig()
    return cfg


def build_pipeline(args: argparse.Namespace) -> tuple[IngestionPipeline, Store]:
    cfg = _resolve_config(args)

    rel = Store(cfg.persistence.relational)
    rel.connect()
    rel.ensure_schema()

    # Blob store: reuse the config-level StorageModel so S3 / OSS work
    from parser.blob_store import make_blob_store

    blob = make_blob_store(cfg.storage.to_dataclass())

    file_store = FileStore(cfg.files, blob, rel)

    parser = ParserPipeline.from_config(cfg)
    tree_builder = TreeBuilder(cfg.parser.tree_builder)
    chunker = Chunker(cfg.parser.chunker)

    embedder = None
    vector = None
    if args.embed:
        from embedder.base import make_embedder
        from persistence.vector.base import make_vector_store

        embedder = make_embedder(cfg.embedder)
        # pgvector needs postgres -- auto-fallback to chromadb for sqlite test fixtures
        if cfg.persistence.vector.backend == "pgvector" and cfg.persistence.relational.backend != "postgres":
            from config import ChromaConfig, VectorConfig

            print(
                "note: vector.backend=pgvector incompatible with "
                f"{cfg.persistence.relational.backend}; "
                "auto-switching to chromadb"
            )
            cfg.persistence.vector = VectorConfig(
                backend="chromadb",
                chromadb=ChromaConfig(
                    persist_directory="./storage/chroma",
                    dimension=cfg.embedder.dimension,
                ),
            )
        vector = make_vector_store(cfg.persistence.vector, relational_store=rel)
        vector.connect()
        vector.ensure_schema()

    pipeline = IngestionPipeline(
        file_store=file_store,
        parser=parser,
        tree_builder=tree_builder,
        chunker=chunker,
        relational_store=rel,
        vector_store=vector,
        embedder=embedder,
    )
    return pipeline, rel


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def ingest_one(
    pipeline: IngestionPipeline,
    rel: Store,
    path: Path,
    *,
    skip_existing: bool,
) -> dict:
    """Process one file; return a small result dict for reporting."""
    started = time.time()
    size = path.stat().st_size
    original_name = path.name
    mime = guess_mime(path)

    result = {
        "path": str(path),
        "name": original_name,
        "size": size,
        "ok": False,
        "skipped": False,
        "error": None,
        "file_id": None,
        "doc_id": None,
        "num_chunks": 0,
        "elapsed_ms": 0,
    }

    try:
        if skip_existing:
            # Cheap dedup check: hash without reading the whole file again
            # would require FileStore.hash_file, but the simplest path is
            # to compute once and look up by content_hash.
            import hashlib

            h = hashlib.sha256()
            with open(path, "rb") as f:
                while buf := f.read(1 << 20):
                    h.update(buf)
            digest = h.hexdigest()
            existing = rel.get_file_by_hash(digest)
            if existing is not None:
                result.update(
                    skipped=True,
                    ok=True,
                    file_id=existing["file_id"],
                    elapsed_ms=int((time.time() - started) * 1000),
                )
                return result

        ingest_result = pipeline.upload_and_ingest(
            path,
            original_name=original_name,
            mime_type=mime,
        )
        result.update(
            ok=True,
            file_id=ingest_result.file_id,
            doc_id=ingest_result.doc_id,
            num_chunks=ingest_result.num_chunks,
        )
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.directory.exists() or not args.directory.is_dir():
        print(f"error: {args.directory} is not a directory", file=sys.stderr)
        return 2

    extensions = [e.strip() for e in args.extensions.split(",") if e.strip()]
    files = list(iter_files(args.directory, extensions))
    if args.limit:
        files = files[: args.limit]

    total = len(files)
    if total == 0:
        print(f"no matching files under {args.directory}")
        return 0

    print(f"found {total} file(s) matching extensions: {', '.join(extensions)}")

    if args.dry_run:
        for p in files:
            print(f"  {p}")
        return 0

    # Ensure target dirs exist
    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.blob.mkdir(parents=True, exist_ok=True)

    pipeline, rel = build_pipeline(args)

    try:
        results = _run(files, pipeline, rel, args)
    finally:
        rel.close()

    _print_summary(results, total)
    return 0 if all(r["ok"] for r in results) else 1


def _run(
    files: list[Path],
    pipeline: IngestionPipeline,
    rel: Store,
    args: argparse.Namespace,
) -> list[dict]:
    results: list[dict] = []

    if args.workers <= 1:
        for i, p in enumerate(files, 1):
            r = ingest_one(pipeline, rel, p, skip_existing=args.skip_existing)
            _report(i, len(files), r, verbose=args.verbose)
            results.append(r)
        return results

    # Parallel: note that parser model handles are shared across workers,
    # and the SQLite store's internal lock serializes writes. Workers > 1
    # is most useful when parser CPU is the bottleneck.
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(
                ingest_one,
                pipeline,
                rel,
                p,
                skip_existing=args.skip_existing,
            ): p
            for p in files
        }
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            _report(i, len(files), r, verbose=args.verbose)
            results.append(r)
    return results


def _report(idx: int, total: int, r: dict, *, verbose: bool) -> None:
    tag = "SKIP" if r["skipped"] else ("OK  " if r["ok"] else "FAIL")
    mb = r["size"] / (1024 * 1024)
    line = f"[{idx:>4}/{total}] {tag} {r['name']} ({mb:.1f} MB, {r['elapsed_ms']} ms)"
    if r["ok"]:
        if r["file_id"]:
            line += f"  file={r['file_id'][:8]}"
        if r["doc_id"]:
            line += f"  doc={r['doc_id']} chunks={r['num_chunks']}"
    else:
        line += f"  error={r['error']}"
    print(line)
    if verbose and r["error"]:
        log.debug("full error for %s: %s", r["path"], r["error"])


def _print_summary(results: list[dict], total: int) -> None:
    ok = sum(1 for r in results if r["ok"] and not r["skipped"])
    skipped = sum(1 for r in results if r["skipped"])
    failed = sum(1 for r in results if not r["ok"])
    total_ms = sum(r["elapsed_ms"] for r in results)
    total_chunks = sum(r["num_chunks"] for r in results)
    print()
    print("=" * 60)
    print(f"  total:   {total}")
    print(f"  ok:      {ok}")
    print(f"  skipped: {skipped}")
    print(f"  failed:  {failed}")
    print(f"  chunks:  {total_chunks}")
    print(f"  elapsed: {total_ms / 1000:.1f} s")
    print("=" * 60)
    if failed:
        print("\nfailures:")
        for r in results:
            if not r["ok"]:
                print(f"  {r['path']}: {r['error']}")


if __name__ == "__main__":
    raise SystemExit(main())
