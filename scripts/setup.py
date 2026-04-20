"""
Interactive setup wizard for ForgeRAG.

Guides the user through a handful of high-level choices, writes a
forgerag.yaml that reflects them, validates it, and optionally
launches batch ingestion or the HTTP API.

Usage:
    python scripts/setup.py                       # interactive wizard
    python scripts/setup.py --profile dev -y      # accept dev defaults
    python scripts/setup.py --profile prod -o myconfig.yaml

Profiles:
    dev    -- SQLite + ChromaDB + local blob + OpenAI models
    prod   -- Postgres + pgvector + local blob + OpenAI models
    custom -- full wizard, no presets
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Let the wizard run from the repo root without install.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Optional backend → required package (import_name, pip_name)
# ---------------------------------------------------------------------------

_RELATIONAL_PACKAGES: dict[str, tuple[str, str]] = {
    "postgres": ("psycopg", "psycopg[binary]"),
    "mysql": ("pymysql", "pymysql"),
}

_VECTOR_PACKAGES: dict[str, tuple[str, str]] = {
    "chromadb": ("chromadb", "chromadb"),
    "qdrant": ("qdrant_client", "qdrant-client"),
    "milvus": ("pymilvus", "pymilvus"),
    "weaviate": ("weaviate", "weaviate-client"),
    "pgvector": ("psycopg", "psycopg[binary]"),
}

_BLOB_PACKAGES: dict[str, tuple[str, str]] = {
    "s3": ("boto3", "boto3"),
    "oss": ("oss2", "oss2"),
}


def _ensure_package(import_name: str, pip_name: str) -> None:
    """Install *pip_name* if *import_name* cannot be imported."""
    if importlib.util.find_spec(import_name) is not None:
        return
    print(_c(f"  '{pip_name}' is not installed — installing now…", "yellow"))
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pip_name],
            check=True,
        )
        print(_c(f"  '{pip_name}' installed successfully.", "green"))
    except subprocess.CalledProcessError as exc:
        print(_c(f"  failed to install '{pip_name}': {exc}", "magenta"))
        print(_c(f"  install it manually and re-run: pip install {pip_name}", "dim"))


def _ensure_backend_package(mapping: dict[str, tuple[str, str]], backend: str) -> None:
    """Look up *backend* in *mapping* and install its package if missing."""
    if backend not in mapping:
        return
    import_name, pip_name = mapping[backend]
    _ensure_package(import_name, pip_name)


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    return sys.stdout.isatty()


def _c(text: str, color: str) -> str:
    if not _is_tty():
        return text
    codes = {
        "bold": "1",
        "dim": "2",
        "green": "32",
        "yellow": "33",
        "blue": "34",
        "magenta": "35",
        "cyan": "36",
    }
    code = codes.get(color, "0")
    return f"\033[{code}m{text}\033[0m"


def banner(title: str) -> None:
    print()
    print(_c("━" * 60, "dim"))
    print(_c(f" {title}", "bold"))
    print(_c("━" * 60, "dim"))


def section(title: str) -> None:
    print()
    print(_c(f"▸ {title}", "cyan"))


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


class Aborted(Exception):
    pass


def ask(
    question: str,
    default: str | None = None,
    *,
    validator: Callable[[str], str | None] | None = None,
    allow_empty: bool = False,
) -> str:
    """
    Ask for free-form text input. Returns the value (or the default).
    `validator` may return an error message to force a retry.
    """
    suffix = f" [{_c(default, 'yellow')}]" if default else ""
    while True:
        try:
            raw = input(f"  {question}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Aborted()
        if not raw and default is not None:
            raw = default
        if not raw and not allow_empty:
            print(_c("  (required)", "magenta"))
            continue
        if validator:
            err = validator(raw)
            if err:
                print(_c(f"  {err}", "magenta"))
                continue
        return raw


def ask_bool(question: str, default: bool = False) -> bool:
    tip = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"  {question} [{_c(tip, 'yellow')}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise Aborted()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print(_c("  please answer y or n", "magenta"))


def ask_choice(
    question: str,
    options: list[tuple[str, str]],  # (value, description)
    default: str | None = None,
) -> str:
    """Numbered menu selection. `options[i]` is (value, description)."""
    print(f"  {question}")
    default_idx = None
    for i, (value, desc) in enumerate(options, 1):
        marker = ""
        if default == value:
            default_idx = i
            marker = _c(" (default)", "dim")
        print(f"    {i}) {_c(value, 'bold')}  {_c('— ' + desc, 'dim')}{marker}")
    default_str = str(default_idx) if default_idx else None
    while True:
        try:
            raw = input(
                f"  enter [1-{len(options)}]{' [' + _c(default_str, 'yellow') + ']' if default_str else ''}: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            raise Aborted()
        if not raw and default_str:
            raw = default_str
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        print(_c(f"  please enter a number 1-{len(options)}", "magenta"))


def ask_int(question: str, default: int, *, min_: int = 1) -> int:
    while True:
        try:
            raw = input(f"  {question} [{_c(str(default), 'yellow')}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Aborted()
        if not raw:
            return default
        try:
            v = int(raw)
            if v < min_:
                raise ValueError
            return v
        except ValueError:
            print(_c(f"  please enter an integer >= {min_}", "magenta"))


# ---------------------------------------------------------------------------
# Profiles (preset defaults)
# ---------------------------------------------------------------------------


def _profile_defaults(profile: str) -> dict[str, Any]:
    if profile == "dev":
        return {
            "relational": "sqlite",
            "sqlite_path": "./storage/forgerag.db",
            "vector": "chromadb",
            "chroma_dir": "./storage/chroma",
            "chroma_dim": 1536,
            "blob": "local",
            "blob_root": "./storage/blobs",
            "embedder_dim": 1536,
        }
    if profile == "prod":
        return {
            "relational": "postgres",
            "pg_host": "localhost",
            "pg_port": 5432,
            "pg_database": "forgerag",
            "pg_user": "forgerag",
            "pg_password_env": "PG_PASSWORD",
            "vector": "pgvector",
            "pgvector_dim": 1024,
            "blob": "local",
            "blob_root": "./storage/blobs",
            "embedder_dim": 1024,
        }
    return {}


# ---------------------------------------------------------------------------
# The wizard
# ---------------------------------------------------------------------------


def run_wizard(profile: str, non_interactive: bool) -> dict[str, Any]:
    """Return a dict of answers that the yaml builder consumes."""
    answers: dict[str, Any] = {}
    defaults = _profile_defaults(profile)

    if non_interactive:
        if not defaults:
            print("error: --non-interactive requires --profile dev|prod", file=sys.stderr)
            raise Aborted()
        return defaults

    banner("ForgeRAG setup wizard")
    print("  Answer a few questions to generate forgerag.yaml.")
    print("  Press Enter to accept defaults. Ctrl-C to abort.")

    # ----- Relational store -----
    section("1/6  Metadata database")
    # ForgeRAG production requires PostgreSQL.
    answers["relational"] = "postgres"
    answers["pg_host"] = ask("Postgres host", default=defaults.get("pg_host", "localhost"))
    answers["pg_port"] = ask_int("Postgres port", default=defaults.get("pg_port", 5432))
    answers["pg_database"] = ask("Postgres database", default=defaults.get("pg_database", "forgerag"))
    answers["pg_user"] = ask("Postgres user", default=defaults.get("pg_user", "forgerag"))
    answers["pg_password_env"] = ask(
        "Env var containing the password",
        default=defaults.get("pg_password_env", "PG_PASSWORD"),
    )

    _ensure_backend_package(_RELATIONAL_PACKAGES, answers["relational"])

    _ensure_backend_package(_RELATIONAL_PACKAGES, answers["relational"])

    # ----- Vector store -----
    section("2/6  Vector database")
    standalone_vectors = [
        ("chromadb", "ChromaDB — lightweight, backend-agnostic"),
        ("qdrant", "Qdrant — production-grade, rich filtering"),
        ("milvus", "Milvus — scalable, GPU-accelerated"),
        ("weaviate", "Weaviate — multi-modal, GraphQL API"),
    ]
    if answers["relational"] == "postgres":
        valid_vectors = [("pgvector", "pgvector — in-database, zero extra ops"), *standalone_vectors]
    else:
        valid_vectors = standalone_vectors
    default_vec = defaults.get("vector", valid_vectors[0][0])
    if default_vec not in [v for v, _ in valid_vectors]:
        default_vec = valid_vectors[0][0]
    answers["vector"] = ask_choice(
        "Which vector backend?",
        valid_vectors,
        default=default_vec,
    )
    if answers["vector"] == "chromadb":
        answers["chroma_dir"] = ask(
            "Chroma persist_directory",
            default=defaults.get("chroma_dir", "./storage/chroma"),
        )
    elif answers["vector"] == "qdrant":
        answers["qdrant_url"] = ask(
            "Qdrant server URL",
            default=defaults.get("qdrant_url", "http://localhost:6333"),
        )
    elif answers["vector"] == "milvus":
        answers["milvus_uri"] = ask(
            "Milvus server URI",
            default=defaults.get("milvus_uri", "http://localhost:19530"),
        )
    elif answers["vector"] == "weaviate":
        answers["weaviate_url"] = ask(
            "Weaviate server URL",
            default=defaults.get("weaviate_url", "http://localhost:8080"),
        )

    _ensure_backend_package(_VECTOR_PACKAGES, answers["vector"])

    # ----- Blob storage -----
    section("3/6  Blob storage (figures + uploaded files)")
    answers["blob"] = ask_choice(
        "Where should blobs live?",
        [
            ("local", "filesystem, single node"),
            ("s3", "any S3-compatible service"),
            ("oss", "Alibaba Cloud OSS"),
        ],
        default=defaults.get("blob", "local"),
    )
    if answers["blob"] == "local":
        answers["blob_root"] = ask(
            "Blob root directory",
            default=defaults.get("blob_root", "./storage/blobs"),
        )
    elif answers["blob"] == "s3":
        answers["s3_endpoint"] = ask("S3 endpoint URL", default="https://s3.amazonaws.com")
        answers["s3_bucket"] = ask("S3 bucket name")
        answers["s3_region"] = ask("S3 region", default="us-east-1")
        answers["s3_access_key_env"] = ask("Access key env var", default="S3_ACCESS_KEY")
        answers["s3_secret_key_env"] = ask("Secret key env var", default="S3_SECRET_KEY")
        answers["s3_public_base_url"] = ask(
            "Public CDN base URL (optional)",
            default="",
            allow_empty=True,
        )
    elif answers["blob"] == "oss":
        answers["oss_endpoint"] = ask(
            "OSS endpoint",
            default="https://oss-cn-hangzhou.aliyuncs.com",
        )
        answers["oss_bucket"] = ask("OSS bucket name")
        answers["oss_access_key_env"] = ask("Access key env var", default="OSS_ACCESS_KEY")
        answers["oss_secret_key_env"] = ask("Secret key env var", default="OSS_SECRET_KEY")
        answers["oss_public_base_url"] = ask(
            "Public base URL (optional)",
            default="",
            allow_empty=True,
        )

    _ensure_backend_package(_BLOB_PACKAGES, answers["blob"])

    # ----- Embedding dimension -----
    section("4/4  Embedding dimension")
    print(_c("  The embedding dimension must match your model's output.", "dim"))
    print(_c("  Common values: 1536 (OpenAI small), 3072 (OpenAI large),", "dim"))
    print(_c("  1024 (BGE-M3, Cohere), 768 (many smaller models).", "dim"))
    print(_c("  You can change the model itself later via /settings.", "dim"))
    answers["embedder_dim"] = ask_int(
        "Embedding dimension",
        default=defaults.get("embedder_dim", 1536),
    )

    section("Done!")
    print()
    print(_c("  LLM models, API keys, parsing strategy, and retrieval", "dim"))
    print(_c("  options are configured via the web UI after startup.", "dim"))
    print(_c("  Visit /settings or use PUT /settings/key/{key}.", "dim"))

    return answers


# ---------------------------------------------------------------------------
# YAML builder
# ---------------------------------------------------------------------------


def build_config_dict(a: dict[str, Any]) -> dict[str, Any]:
    cfg: dict[str, Any] = {}

    # --- parser (minimal: pymupdf always on; MinerU via /settings) ---
    cfg["parser"] = {"backends": {"pymupdf": {"enabled": True}}}

    # --- storage (blob) ---
    storage: dict[str, Any] = {"mode": a["blob"]}
    if a["blob"] == "local":
        storage["local"] = {"root": a["blob_root"]}
    elif a["blob"] == "s3":
        storage["s3"] = {
            "endpoint": a["s3_endpoint"],
            "bucket": a["s3_bucket"],
            "region": a["s3_region"],
            "access_key_env": a["s3_access_key_env"],
            "secret_key_env": a["s3_secret_key_env"],
        }
        if a.get("s3_public_base_url"):
            storage["s3"]["public_base_url"] = a["s3_public_base_url"]
    elif a["blob"] == "oss":
        storage["oss"] = {
            "endpoint": a["oss_endpoint"],
            "bucket": a["oss_bucket"],
            "access_key_env": a["oss_access_key_env"],
            "secret_key_env": a["oss_secret_key_env"],
        }
        if a.get("oss_public_base_url"):
            storage["oss"]["public_base_url"] = a["oss_public_base_url"]
    cfg["storage"] = storage

    # --- persistence ---
    rel: dict[str, Any] = {"backend": "postgres"}
    rel["postgres"] = {
        "host": a["pg_host"],
        "port": a["pg_port"],
        "database": a["pg_database"],
        "user": a["pg_user"],
        "password_env": a["pg_password_env"],
    }

    vec: dict[str, Any] = {"backend": a["vector"]}
    if a["vector"] == "pgvector":
        vec["pgvector"] = {
            "dimension": a["embedder_dim"],
            "index_type": "hnsw",
            "distance": "cosine",
        }
    elif a["vector"] == "chromadb":
        vec["chromadb"] = {
            "mode": "persistent",
            "persist_directory": a["chroma_dir"],
            "collection_name": "forgerag",
            "dimension": a["embedder_dim"],
            "distance": "cosine",
        }
    elif a["vector"] == "qdrant":
        vec["qdrant"] = {
            "url": a["qdrant_url"],
            "collection_name": "forgerag_chunks",
            "dimension": a["embedder_dim"],
            "distance": "cosine",
        }
    elif a["vector"] == "milvus":
        vec["milvus"] = {
            "uri": a["milvus_uri"],
            "collection_name": "forgerag_chunks",
            "dimension": a["embedder_dim"],
            "distance": "cosine",
            "index_type": "HNSW",
        }
    elif a["vector"] == "weaviate":
        vec["weaviate"] = {
            "url": a["weaviate_url"],
            "collection_name": "ForgeragChunks",
            "dimension": a["embedder_dim"],
            "distance": "cosine",
        }

    cfg["persistence"] = {"relational": rel, "vector": vec}

    # --- embedder (dimension only; model/key/base via /settings) ---
    cfg["embedder"] = {"dimension": a["embedder_dim"]}

    # --- files ---
    cfg["files"] = {"hash_algorithm": "sha256", "max_bytes": 524288000}

    return cfg


def write_yaml(cfg: dict[str, Any], path: Path) -> None:
    try:
        import yaml
    except ImportError:
        raise RuntimeError("pyyaml not installed: pip install pyyaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            cfg,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )


# ---------------------------------------------------------------------------
# Post-setup actions
# ---------------------------------------------------------------------------


def post_setup(config_path: Path) -> None:
    section("Next steps")

    # Run config validator (subprocess isolates us from pydantic issues)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "config", "validate", str(config_path)],
            cwd=_ROOT,
        )
        if r.returncode != 0:
            print(_c("  config validation FAILED — fix the file and re-run", "magenta"))
            return
    except FileNotFoundError:
        pass

    print()
    choice = ask_choice(
        "What do you want to do next?",
        [
            ("nothing", "just exit; run it yourself later"),
            ("batch", "batch-ingest files from a directory now"),
            ("api", "start the HTTP API (uvicorn) now"),
        ],
        default="nothing",
    )
    if choice == "nothing":
        print()
        print(_c("  done. to use this config later:", "dim"))
        print(f"    export FORGERAG_CONFIG={config_path}")
        return

    if choice == "batch":
        target = ask(
            "Directory to ingest",
            default="./papers",
            validator=lambda p: None if Path(p).exists() else f"not found: {p}",
        )
        embed = ask_bool("Compute embeddings?", default=False)
        cmd = [
            sys.executable,
            "scripts/batch_ingest.py",
            target,
            "--config",
            str(config_path),
        ]
        if embed:
            cmd.append("--embed")
        print(_c(f"\n  running: {' '.join(cmd)}\n", "dim"))
        subprocess.run(cmd, cwd=_ROOT)
        return

    if choice == "api":
        host = ask("Host", default="0.0.0.0")
        port = ask_int("Port", default=8000)
        env = os.environ.copy()
        env["FORGERAG_CONFIG"] = str(config_path)
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "api.app:app",
            "--host",
            host,
            "--port",
            str(port),
            "--reload",
        ]
        print(_c(f"\n  FORGERAG_CONFIG={config_path}", "dim"))
        print(_c(f"  running: {' '.join(cmd)}\n", "dim"))
        try:
            subprocess.run(cmd, cwd=_ROOT, env=env)
        except KeyboardInterrupt:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


_HELP_DESCRIPTION = """\
Interactive setup wizard for ForgeRAG.

Walks through six questions and generates a forgerag.yaml config
file that wires together the relational store, vector store, blob
storage, embedder, answer LLM, and optional MinerU parser. After
writing the file, the wizard validates it and offers to batch-ingest
a directory or start the HTTP API right away.

All LLM / embedding backends go through litellm, which accepts custom
endpoints via an api_base setting -- so Ollama, vLLM, OneAPI,
OpenRouter, DeepSeek, any OpenAI-compatible server all work with the
same model string.
"""

_HELP_EPILOG = """\
Profiles
--------
  dev    SQLite + ChromaDB + local blob.
         Zero infrastructure, good for local experimentation.

  prod   Postgres + pgvector + local blob.
         Recommended for a single production node.

  custom Full wizard, no presets. Use when you know what you want.

LLM / model / retrieval configuration
--------------------------------------
  These are NOT set in the yaml or the wizard. They are managed
  at runtime via the /settings API (DB-backed, frontend-editable):

    GET  /settings              — view all settings grouped
    PUT  /settings/key/{key}    — change one setting instantly

  On first startup, defaults are seeded into the DB automatically.
  Configure your API keys, models, and retrieval strategy there.

Typical runs
------------
  # Interactive wizard with prod preset, save to myconfig.yaml:
  python scripts/setup.py --profile prod -o myconfig.yaml

  # Non-interactive dev (zero prompts, for CI / Docker):
  python scripts/setup.py --profile dev -y

  # Full custom wizard:
  python scripts/setup.py --profile custom

Using the generated config afterwards
-------------------------------------
  # Validate:
  python -m config validate forgerag.yaml

  # Point everything at it:
  export FORGERAG_CONFIG=./forgerag.yaml

  # Batch-ingest some files:
  python scripts/batch_ingest.py ./papers

  # Launch the HTTP API:
  uvicorn api.app:app --host 0.0.0.0 --port 8000

"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="setup.py",
        description=_HELP_DESCRIPTION,
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--profile",
        choices=("dev", "prod", "custom"),
        default="custom",
        help="Preset defaults. dev=sqlite+chroma; prod=postgres+pgvector.",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("./forgerag.yaml"),
        help="Where to write the generated config.",
    )
    p.add_argument(
        "-y",
        "--non-interactive",
        action="store_true",
        help="Accept profile defaults without prompting. Requires --profile.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite output file if it already exists.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.output.exists() and not args.force:
        print(_c(f"  {args.output} already exists. Use --force to overwrite.", "magenta"))
        return 2

    try:
        answers = run_wizard(args.profile, args.non_interactive)
    except Aborted:
        print("\n  aborted.")
        return 130

    cfg_dict = build_config_dict(answers)
    try:
        write_yaml(cfg_dict, args.output)
    except Exception as e:
        print(_c(f"  failed to write {args.output}: {e}", "magenta"))
        return 1

    print()
    print(_c(f"  wrote {args.output}", "green"))
    print()

    try:
        post_setup(args.output)
    except Aborted:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
