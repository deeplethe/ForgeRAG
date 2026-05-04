"""
Config CLI.

Usage:
    python -m config validate [path/to/opencraig.yaml]
    python -m config dump     [path/to/opencraig.yaml]
    python -m config schema

validate  -- load a YAML file through pydantic and print every
             resolved setting that could matter at runtime. Exits
             non-zero on any validation error, so it works in CI.

dump      -- pretty-print the full, defaults-expanded AppConfig
             as JSON. Great for diffing two configs or feeding to
             jq for inspection.

schema    -- dump the JSON Schema of AppConfig. Lets editors
             (VS Code / JetBrains) give auto-complete on yaml.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .app import AppConfig
from .loader import load_config


def _resolve_path(argv: list[str]) -> Path | None:
    if len(argv) >= 2:
        return Path(argv[1])
    env = os.environ.get("OPENCRAIG_CONFIG")
    return Path(env) if env else None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_validate(argv: list[str]) -> int:
    path = _resolve_path(argv)
    if path is None:
        print(
            "usage: python -m config validate <path>       (or set OPENCRAIG_CONFIG env var)",
            file=sys.stderr,
        )
        return 2
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2

    try:
        cfg = load_config(path)
    except ValidationError as e:
        print(f"INVALID: {path}\n", file=sys.stderr)
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            print(f"  {loc}: {err['msg']}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {path}: {e}", file=sys.stderr)
        return 1

    _print_summary(path, cfg)
    return 0


def cmd_dump(argv: list[str]) -> int:
    path = _resolve_path(argv)
    try:
        cfg = load_config(path) if path else AppConfig()
    except ValidationError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    print(cfg.model_dump_json(indent=2))
    return 0


def cmd_schema(_argv: list[str]) -> int:
    print(json.dumps(AppConfig.model_json_schema(), indent=2, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


def _print_summary(path: Path, cfg: AppConfig) -> None:
    """Human-readable snapshot of the resolved config."""
    lines: list[str] = []
    ok = "\033[32mOK\033[0m" if sys.stdout.isatty() else "OK"
    lines.append(f"{ok}  {path}")
    lines.append("")

    # Parser
    pcfg = cfg.parser.backends
    lines.append("parser")
    lines.append(f"  backend               : {cfg.parser.backend}")
    if cfg.parser.backend in ("mineru", "mineru-vlm"):
        lines.append(f"  mineru device         : {pcfg.mineru.device}")
        if pcfg.mineru.server_url:
            lines.append(f"  mineru server_url     : {pcfg.mineru.server_url}")
    lines.append(f"  tree_builder LLM      : {cfg.parser.tree_builder.llm_enabled}")
    lines.append(f"  chunker target/max    : {cfg.parser.chunker.target_tokens}/{cfg.parser.chunker.max_tokens} tokens")
    lines.append("")

    # Storage (blob)
    lines.append("storage (blob)")
    lines.append(f"  mode                  : {cfg.storage.mode}")
    if cfg.storage.mode == "local" and cfg.storage.local:
        lines.append(f"  root                  : {cfg.storage.local.root}")
        lines.append(f"  public_base_url       : {cfg.storage.local.public_base_url or '(none, stream from API)'}")
    elif cfg.storage.mode == "s3" and cfg.storage.s3:
        lines.append(f"  bucket                : {cfg.storage.s3.bucket}")
        lines.append(f"  endpoint              : {cfg.storage.s3.endpoint}")
        lines.append(f"  public_base_url       : {cfg.storage.s3.public_base_url or '(presigned)'}")
    elif cfg.storage.mode == "oss" and cfg.storage.oss:
        lines.append(f"  bucket                : {cfg.storage.oss.bucket}")
        lines.append(f"  endpoint              : {cfg.storage.oss.endpoint}")
    lines.append("")

    # Files
    lines.append("files")
    lines.append(f"  hash_algorithm        : {cfg.files.hash_algorithm}")
    lines.append(f"  max_bytes             : {cfg.files.max_bytes / 1024 / 1024:.0f} MiB")
    lines.append("")

    # Persistence
    rel = cfg.persistence.relational
    vec = cfg.persistence.vector
    lines.append("persistence.relational")
    lines.append(f"  backend               : {rel.backend}")
    if rel.backend == "sqlite" and rel.sqlite:
        lines.append(f"  path                  : {rel.sqlite.path}")
    elif rel.backend == "postgres" and rel.postgres:
        lines.append(f"  host                  : {rel.postgres.host}:{rel.postgres.port}/{rel.postgres.database}")
        lines.append(f"  user                  : {rel.postgres.user}")
        cred = rel.postgres.password_env or "(plaintext in config)"
        lines.append(f"  password              : {cred}")

    lines.append("")
    lines.append("persistence.vector")
    lines.append(f"  backend               : {vec.backend}")
    if vec.backend == "pgvector" and vec.pgvector:
        lines.append(f"  dimension             : {vec.pgvector.dimension}")
        lines.append(f"  index                 : {vec.pgvector.index_type} / {vec.pgvector.distance}")
    elif vec.backend == "chromadb" and vec.chromadb:
        lines.append(f"  mode                  : {vec.chromadb.mode}")
        lines.append(f"  collection            : {vec.chromadb.collection_name}")
        lines.append(f"  dimension             : {vec.chromadb.dimension}")
        if vec.chromadb.mode == "persistent":
            lines.append(f"  persist_directory     : {vec.chromadb.persist_directory}")
        else:
            lines.append(f"  host                  : {vec.chromadb.http_host}:{vec.chromadb.http_port}")
    lines.append("")

    # Embedder
    emb = cfg.embedder
    lines.append("embedder")
    lines.append(f"  backend               : {emb.backend}")
    lines.append(f"  dimension             : {emb.dimension}")
    lines.append(f"  batch_size            : {emb.batch_size}")
    if emb.backend == "litellm" and emb.litellm:
        lines.append(f"  model                 : {emb.litellm.model}")
        lines.append(f"  api_key_env           : {emb.litellm.api_key_env or '(none)'}")
    elif emb.backend == "sentence_transformers" and emb.sentence_transformers:
        st = emb.sentence_transformers
        lines.append(f"  model                 : {st.model_name}")
        lines.append(f"  device                : {st.device}")
    lines.append("")

    # Retrieval
    r = cfg.retrieval
    lines.append("retrieval")
    lines.append(f"  vector.top_k          : {r.vector.top_k}")
    lines.append(f"  tree_path.enabled     : {r.tree_path.enabled} (llm_nav={r.tree_path.llm_nav_enabled})")
    lines.append(f"  merge.rrf_k           : {r.merge.rrf_k}")
    lines.append(f"  sibling_expansion     : {r.merge.sibling_expansion_enabled}")
    lines.append(f"  crossref_expansion    : {r.merge.crossref_expansion_enabled}")
    lines.append(f"  rerank                : backend={r.rerank.backend} top_k={r.rerank.top_k}")
    lines.append("")

    # Answering
    ans = cfg.answering
    lines.append("answering")
    lines.append(f"  generator backend     : {ans.generator.backend}")
    lines.append(f"  generator model       : {ans.generator.model}")
    lines.append(f"  max_chunks            : {ans.max_chunks}")
    lines.append(f"  max_context_chars     : {ans.generator.max_context_chars}")
    lines.append("")

    # Cross-section sanity (already validated by pydantic; re-state for clarity)
    lines.append("cross-checks")
    if vec.backend == "pgvector" and vec.pgvector:
        match = "✓" if vec.pgvector.dimension == emb.dimension else "✗"
        lines.append(f"  {match} embedder.dim == pgvector.dim ({emb.dimension} vs {vec.pgvector.dimension})")
    if vec.backend == "chromadb" and vec.chromadb:
        match = "✓" if vec.chromadb.dimension == emb.dimension else "✗"
        lines.append(f"  {match} embedder.dim == chromadb.dim ({emb.dimension} vs {vec.chromadb.dimension})")
    if vec.backend == "pgvector" and rel.backend != "postgres":
        lines.append(f"  ✗ pgvector requires postgres, got {rel.backend}")
    else:
        lines.append(f"  ✓ relational/vector combo ({rel.backend} + {vec.backend})")

    print("\n".join(lines))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m config {validate|dump|schema} [path]", file=sys.stderr)
        return 2

    cmd = argv[0]
    rest = argv[1:]
    if cmd == "validate":
        return cmd_validate([cmd, *rest])
    if cmd == "dump":
        return cmd_dump([cmd, *rest])
    if cmd == "schema":
        return cmd_schema(rest)

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    print("usage: python -m config {validate|dump|schema} [path]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
