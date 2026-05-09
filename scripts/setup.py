"""
Interactive setup wizard for OpenCraig.

Walks the user through six small steps and writes a opencraig.yaml
that wires the relational store, vector store, blob storage,
embedder, and answer-generation LLM end-to-end. The embedder and
LLM steps each finish with a real connection test (live API call)
so a typo in api_base or a wrong key surfaces immediately, before
the user discovers it the first time they try to ingest a document.

Navigation:
    Enter          accept the default in [yellow]
    b / back / <   re-open the previous step
    Ctrl-C         abort

Usage:
    python scripts/setup.py                       # interactive wizard
    python scripts/setup.py --profile dev -y      # accept dev defaults
    python scripts/setup.py --profile prod -o myconfig.yaml

Profiles:
    dev    -- ChromaDB + local blob + OpenAI defaults
    prod   -- pgvector + local blob + OpenAI defaults
    custom -- full wizard, no presets
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Force UTF-8 on stdout/stderr so the box-drawing / arrow characters in
# banners render on Windows consoles where the default codepage is GBK
# (otherwise UnicodeEncodeError aborts the wizard mid-prompt).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass

# Let the wizard run from the repo root without install.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Yaml → optional pip package list
#
# All optional backends (vector stores, graph, blob, parser, embedder) ship
# with their pip dependency NOT in requirements.txt. The user only installs
# what their yaml actually selects. ``dependencies_for(cfg)`` is the single
# source of truth — both the wizard and ``--sync-deps`` use it.
# ---------------------------------------------------------------------------


def dependencies_for(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """Map a yaml config dict to the (import_name, pip_name) pairs the
    deployment needs at runtime. Order matters only for log readability;
    the resolver de-dups on import_name so pgvector + postgres don't
    install psycopg twice.
    """
    out: list[tuple[str, str]] = []

    rel_backend = (cfg.get("persistence", {}).get("relational", {}).get("backend")) or "postgres"
    if rel_backend == "postgres":
        out.append(("psycopg", "psycopg[binary]"))

    vec_backend = (cfg.get("persistence", {}).get("vector", {}).get("backend")) or "pgvector"
    vec_pkg = {
        "pgvector": ("psycopg", "psycopg[binary]"),
        "chromadb": ("chromadb", "chromadb"),
        "qdrant": ("qdrant_client", "qdrant-client"),
        "milvus": ("pymilvus", "pymilvus"),
        "weaviate": ("weaviate", "weaviate-client"),
    }.get(vec_backend)
    if vec_pkg:
        out.append(vec_pkg)

    graph_backend = (cfg.get("graph", {}).get("backend")) or "neo4j"
    if graph_backend == "neo4j":
        out.append(("neo4j", "neo4j"))

    blob_mode = (cfg.get("storage", {}).get("mode")) or "local"
    if blob_mode == "s3":
        out.append(("boto3", "boto3"))
    elif blob_mode == "oss":
        out.append(("oss2", "oss2"))

    parser_backend = (cfg.get("parser", {}).get("backend")) or "pymupdf"
    if parser_backend in ("mineru", "mineru-vlm"):
        out.append(("mineru", "mineru"))

    embedder_backend = (cfg.get("embedder", {}).get("backend")) or "litellm"
    if embedder_backend == "sentence_transformers":
        out.append(("sentence_transformers", "sentence-transformers"))

    # NOTE: ``docker`` (Python Engine API client) is intentionally NOT
    # listed here. It's a soft dependency for the Workbench agent's
    # sandbox container path — when present + daemon reachable, the
    # agent gets real Read / Edit / Write / Bash / python_exec; when
    # absent, ``api/state.py`` falls back to in-process degraded
    # mode (Library-search tools still work). Operators who want
    # sandbox install it themselves: ``pip install docker``. The
    # diagnostic in ``api/state.py`` distinguishes "SDK missing"
    # from "daemon unreachable" so the right action is obvious.

    # De-dup on import_name (preserves first-seen order)
    seen = set()
    deduped: list[tuple[str, str]] = []
    for imp, pip_name in out:
        if imp in seen:
            continue
        seen.add(imp)
        deduped.append((imp, pip_name))
    return deduped


def _ensure_package(import_name: str, pip_name: str) -> bool:
    """Install *pip_name* if *import_name* cannot be imported.
    Returns True if a fresh install was performed, False if already present."""
    if importlib.util.find_spec(import_name) is not None:
        return False
    print(
        _c(
            _t(
                f"  '{pip_name}' is not installed — installing now…",
                f"  '{pip_name}' 未安装 — 现在自动安装…",
            ),
            "yellow",
        )
    )
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pip_name],
            check=True,
        )
        print(
            _c(
                _t(
                    f"  '{pip_name}' installed successfully.",
                    f"  '{pip_name}' 已安装成功。",
                ),
                "green",
            )
        )
        return True
    except subprocess.CalledProcessError as exc:
        print(
            _c(
                _t(
                    f"  failed to install '{pip_name}': {exc}",
                    f"  安装 '{pip_name}' 失败：{exc}",
                ),
                "magenta",
            )
        )
        print(
            _c(
                _t(
                    f"  install it manually and re-run: pip install {pip_name}",
                    f"  请手动安装后重试：pip install {pip_name}",
                ),
                "dim",
            )
        )
        return False


def sync_dependencies(cfg: dict[str, Any]) -> tuple[int, int]:
    """Walk dependencies_for(cfg) and install each missing one.
    Returns (newly_installed_count, total_required_count)."""
    deps = dependencies_for(cfg)
    if not deps:
        print(
            _c(
                _t(
                    "  No optional packages required for this configuration.",
                    "  当前配置无需安装额外的可选依赖。",
                ),
                "dim",
            )
        )
        return 0, 0
    print(
        _c(
            _t(
                f"  Resolving {len(deps)} optional package(s) from yaml…",
                f"  根据 yaml 解析 {len(deps)} 个可选依赖…",
            ),
            "dim",
        )
    )
    installed = 0
    for imp, pip_name in deps:
        if _ensure_package(imp, pip_name):
            installed += 1
    return installed, len(deps)


# ---------------------------------------------------------------------------
# Localisation — single source of truth: a per-call _t(en, zh) helper.
# Translations live next to the call site so a missing zh string is
# obvious in code review (no scattered dict to fall out of sync).
# ---------------------------------------------------------------------------


_LANG = "en"  # set by _select_language() at startup


def _t(en: str, zh: str) -> str:
    return zh if _LANG == "zh" else en


# ---------------------------------------------------------------------------
# Arrow-key cross-platform menu
# ---------------------------------------------------------------------------


def _read_key() -> str:
    """Block until one logical key is read. Returns one of:
        "UP" "DOWN" "ENTER" "BACK" "ABORT" "OTHER"
    No echo. Restores terminal state on POSIX."""
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getch()
        # Arrow keys / function keys: \xe0 prefix on most consoles, \x00 on some.
        if ch in (b"\xe0", b"\x00"):
            ch2 = msvcrt.getch()
            if ch2 == b"H":
                return "UP"
            if ch2 == b"P":
                return "DOWN"
            return "OTHER"
        if ch in (b"\r", b"\n"):
            return "ENTER"
        if ch == b"\x03":  # Ctrl-C in raw-ish mode
            return "ABORT"
        if ch in (b"b", b"B", b"<"):
            return "BACK"
        return "OTHER"
    # POSIX
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":  # ESC — could start an arrow CSI or be lone ESC
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 == "A":
                    return "UP"
                if ch3 == "B":
                    return "DOWN"
                return "OTHER"
            return "ABORT"  # bare ESC
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == "\x03":
            return "ABORT"
        if ch in ("b", "B", "<"):
            return "BACK"
        return "OTHER"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _arrow_select(
    options: list[tuple[str, str]],
    *,
    default_idx: int = 0,
    allow_back: bool = True,
) -> str:
    """Up/Down navigation, Enter to confirm. ``options`` is [(value, label)].

    Falls back to numbered input when stdin/stdout aren't both TTYs (CI,
    piped tests, IDE consoles), so the wizard stays scriptable. Raises
    ``_GoBack`` on 'b' / Backspace and ``Aborted`` on Ctrl-C / ESC.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _numbered_fallback(options, default_idx=default_idx, allow_back=allow_back)

    idx = max(0, min(default_idx, len(options) - 1))
    n = len(options)

    def _draw() -> None:
        for i, (_value, label) in enumerate(options):
            if i == idx:
                line = _c(f"  ❯ {label}", "cyan")
            else:
                line = f"    {label}"
            print(line)
        hint = _t(
            "  ↑/↓ to move · Enter to select · b to go back · Ctrl-C to abort",
            "  ↑/↓ 移动 · Enter 确认 · b 返回上一步 · Ctrl-C 中止",
        )
        if not allow_back:
            hint = _t(
                "  ↑/↓ to move · Enter to select · Ctrl-C to abort",
                "  ↑/↓ 移动 · Enter 确认 · Ctrl-C 中止",
            )
        print(_c(hint, "dim"))

    def _undraw() -> None:
        # n option lines + 1 hint line
        for _ in range(n + 1):
            sys.stdout.write("\033[F\033[2K")
        sys.stdout.flush()

    _draw()
    while True:
        try:
            key = _read_key()
        except (EOFError, KeyboardInterrupt):
            raise Aborted()
        if key == "ABORT":
            raise Aborted()
        if key == "BACK":
            if not allow_back:
                continue
            raise _GoBack()
        if key == "UP":
            idx = (idx - 1) % n
        elif key == "DOWN":
            idx = (idx + 1) % n
        elif key == "ENTER":
            _undraw()
            value, label = options[idx]
            print(f"  ❯ {_c(label, 'green')}")
            return value
        else:
            continue
        _undraw()
        _draw()


def _numbered_fallback(
    options: list[tuple[str, str]],
    *,
    default_idx: int = 0,
    allow_back: bool = True,
) -> str:
    """Numbered prompt used when stdin/stdout aren't TTYs."""
    for i, (_value, label) in enumerate(options, 1):
        marker = _c(_t(" (default)", " (默认)"), "dim") if i - 1 == default_idx else ""
        print(f"    {i}) {label}{marker}")
    default_str = str(default_idx + 1)
    enter_label = _t("enter", "选择")
    while True:
        try:
            raw = input(f"  {enter_label} [1-{len(options)}] [{_c(default_str, 'yellow')}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Aborted()
        if allow_back and raw.lower() in _BACK_TOKENS:
            raise _GoBack()
        if not raw:
            raw = default_str
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        print(
            _c(
                _t(
                    f"  please enter a number 1-{len(options)}",
                    f"  请输入 1-{len(options)} 之间的数字",
                ),
                "magenta",
            )
        )


def _select_language() -> str:
    """First prompt of the wizard. Returns 'en' or 'zh'.

    Uses arrow-key selection on a TTY; numbered fallback otherwise.
    Defaults to English on EOF / Ctrl-C / piped non-interactive use.
    """
    print()
    print("  Language / 语言")
    try:
        return _arrow_select(
            [
                ("en", "English"),
                ("zh", "中文"),
            ],
            default_idx=0,
            allow_back=False,
        )
    except (Aborted, _GoBack):
        return "en"


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
    """Ctrl-C / EOF — stop the whole wizard."""


class _GoBack(Exception):
    """User typed 'b' / 'back' / '<' — re-run the previous step."""


# Tokens that trigger _GoBack when entered at any prompt.
_BACK_TOKENS = {"b", "back", "<"}


def _check_back(raw: str) -> None:
    if raw.lower() in _BACK_TOKENS:
        raise _GoBack()


def ask(
    question: str,
    default: str | None = None,
    *,
    validator: Callable[[str], str | None] | None = None,
    allow_empty: bool = False,
) -> str:
    """
    Ask for free-form text input. Returns the value (or the default).
    `validator` may return an error message to force a retry. Typing
    ``b`` / ``back`` / ``<`` raises ``_GoBack``.
    """
    suffix = f" [{_c(default, 'yellow')}]" if default else ""
    while True:
        try:
            raw = input(f"  {question}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Aborted()
        _check_back(raw)
        if not raw and default is not None:
            raw = default
        if not raw and not allow_empty:
            print(_c(_t("  (required)", "  （必填）"), "magenta"))
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
        _check_back(raw)
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print(_c(_t("  please answer y or n", "  请回答 y 或 n"), "magenta"))


def ask_choice(
    question: str,
    options: list[tuple[str, str]],  # (value, description)
    default: str | None = None,
) -> str:
    """Arrow-key menu (numbered fallback when stdin isn't a TTY).

    ``options[i]`` is ``(value, description)``. The shown label is
    ``f"{value}  — {desc}"`` so the returned token stays visible
    while the user is choosing.
    """
    print(f"  {question}")
    labelled = [(value, f"{value}  — {desc}") for value, desc in options]
    default_idx = 0
    if default is not None:
        for i, (v, _l) in enumerate(labelled):
            if v == default:
                default_idx = i
                break
    return _arrow_select(labelled, default_idx=default_idx)


def ask_int(question: str, default: int, *, min_: int = 1) -> int:
    while True:
        try:
            raw = input(f"  {question} [{_c(str(default), 'yellow')}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Aborted()
        _check_back(raw)
        if not raw:
            return default
        try:
            v = int(raw)
            if v < min_:
                raise ValueError
            return v
        except ValueError:
            print(
                _c(
                    _t(
                        f"  please enter an integer >= {min_}",
                        f"  请输入 >= {min_} 的整数",
                    ),
                    "magenta",
                )
            )


# ---------------------------------------------------------------------------
# Input sanitisers
# ---------------------------------------------------------------------------


_TRAILING_PARENS_RE = re.compile(r"\s*\(.*\)\s*$")


def _sanitize_model_name(s: str) -> str:
    """Strip stuff users sometimes accidentally copy from the example
    hints alongside the model id:

      * "openai/bge-m3 (1024)"             → "openai/bge-m3"
      * "openai/gpt-4o-mini # OpenAI"      → "openai/gpt-4o-mini"
      * "openai/X  + api_base=https://..." → "openai/X"
      * surrounding whitespace
    """
    s = s.strip()
    # Comment after '#'
    if "#" in s:
        s = s.split("#", 1)[0].rstrip()
    # "+ api_base=..." trailing annotation
    if "+" in s:
        s = s.split("+", 1)[0].rstrip()
    # Trailing parenthetical e.g. "(1024-dim)"
    s = _TRAILING_PARENS_RE.sub("", s).strip()
    return s


# ---------------------------------------------------------------------------
# Profiles (preset defaults)
# ---------------------------------------------------------------------------


def _profile_defaults(profile: str) -> dict[str, Any]:
    base = {
        "embedder_model": "openai/text-embedding-3-small",
        "embedder_dim": 1536,
        "embedder_api_key_env": "OPENAI_API_KEY",
        "embedder_api_base": "",
        "llm_model": "openai/gpt-4o-mini",
        "llm_api_key_env": "OPENAI_API_KEY",
        "llm_api_base": "",
    }
    if profile == "dev":
        # Zero extra services — single-process, file-persisted, fast bootstrap.
        return {
            **base,
            "relational": "sqlite",
            "sqlite_path": "./storage/opencraig.db",
            "vector": "chromadb",
            "chroma_dir": "./storage/chroma",
            "graph_backend": "networkx",
            "networkx_path": "./storage/kg.json",
            "blob": "local",
            "blob_root": "./storage/blobs",
            "parser_backend": "pymupdf",
        }
    if profile == "prod":
        return {
            **base,
            "relational": "postgres",
            "pg_host": "localhost",
            "pg_port": 5432,
            "pg_database": "opencraig",
            "pg_user": "opencraig",
            "pg_password_env": "PG_PASSWORD",
            "vector": "pgvector",
            "embedder_dim": 1024,
            "graph_backend": "neo4j",
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password_env": "NEO4J_PASSWORD",
            "neo4j_database": "neo4j",
            "blob": "local",
            "blob_root": "./storage/blobs",
            "parser_backend": "pymupdf",
        }
    return {}


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------


def _resolve_key(api_key: str | None, api_key_env: str | None) -> str | None:
    """Return the effective key: explicit > env. None if neither is set."""
    if api_key:
        return api_key
    if api_key_env:
        return os.environ.get(api_key_env)
    return None


@contextlib.contextmanager
def _captured_litellm_debug():
    """Run a litellm call with verbose debug captured to a buffer.

    On success the buffer is discarded so the wizard stays clean; on
    failure the caller flushes the buffer to stderr so the user sees
    the full HTTP request / response that explains the error.

    Yields a callable that returns the captured text.
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(name)s:%(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    # LiteLLM logs under several names. Capture them all + httpx/httpcore
    # for HTTP request traces. Critically, LiteLLM pre-attaches its own
    # StreamHandler directly to the "LiteLLM" logger that writes to the
    # *original* stderr — propagate=False alone isn't enough; we also
    # have to detach those handlers for the duration of the capture and
    # restore them afterwards. Same trick for the root logger so any
    # ``logging.basicConfig()`` handler doesn't leak through.
    logger_names = ["LiteLLM", "litellm", "httpx", "httpcore", ""]
    saved: list[tuple[logging.Logger, int, bool, list]] = []
    for name in logger_names:
        lg = logging.getLogger(name)
        saved.append((lg, lg.level, lg.propagate, list(lg.handlers)))
        # Detach existing handlers (they hold strong refs to the original
        # stderr / stdout, so reassigning sys.stdout doesn't redirect them).
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        lg.addHandler(handler)

    # litellm also writes raw to stdout (``set_verbose=True`` print()s
    # things like the curl trace) AND to stderr ("Provider List: …").
    # Redirect both into the same buffer so chronological order is
    # preserved and nothing leaks to the console.
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    sys.stdout = buf
    sys.stderr = buf

    saved_verbose = None
    try:
        import litellm  # type: ignore

        saved_verbose = getattr(litellm, "set_verbose", False)
        litellm.set_verbose = True
    except Exception:
        litellm = None  # type: ignore

    try:
        yield lambda: buf.getvalue()
    finally:
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
        for lg, lvl, prop, handlers in saved:
            try:
                lg.removeHandler(handler)
            except ValueError:
                pass
            lg.setLevel(lvl)
            lg.propagate = prop
            # Restore original handlers detached on entry.
            for h in handlers:
                if h not in lg.handlers:
                    lg.addHandler(h)
        if litellm is not None and saved_verbose is not None:
            try:
                litellm.set_verbose = saved_verbose
            except Exception:
                pass


def _format_exception(e: Exception) -> str:
    """Render an exception with as much actionable detail as we can pull
    out of common litellm / openai-style error shapes."""
    parts = [f"{type(e).__name__}: {e}"]
    # OpenAI-compatible HTTPException usually has .response with body.
    resp = getattr(e, "response", None)
    if resp is not None:
        body = None
        try:
            body = resp.json()
        except Exception:
            try:
                body = resp.text
            except Exception:
                pass
        if body:
            parts.append(f"  response body: {body}")
        status = getattr(resp, "status_code", None)
        if status is not None:
            parts.append(f"  http status:   {status}")
    # litellm sometimes attaches .llm_provider and .model
    for attr in ("llm_provider", "model"):
        val = getattr(e, attr, None)
        if val:
            parts.append(f"  {attr:14} {val}")
    return "\n".join(parts)


def _test_embedding(
    model: str, key: str | None, base: str | None, *, timeout: float = 30.0
) -> tuple[bool, str, int | None]:
    """Call litellm.embedding once with a short input.

    Returns ``(ok, message, dim)``. ``dim`` is the detected output
    dimension on success and None on any failure. Verbose litellm
    debug output is buffered and only flushed to stderr on failure.
    """
    try:
        from litellm import embedding  # type: ignore
    except ImportError as e:
        return False, f"litellm not installed: {e}", None
    with _captured_litellm_debug() as get_debug:
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "input": ["ping"],
                "timeout": timeout,
                # LiteLLM defaults to ``base64`` for OpenAI; some providers
                # (notably SiliconFlow) only accept ``float`` and respond
                # with 400 'parameter is invalid'. ``float`` is universally
                # supported, so override the default explicitly.
                "encoding_format": "float",
            }
            if key:
                kwargs["api_key"] = key
            if base:
                kwargs["api_base"] = base
            resp = embedding(**kwargs)
            data = getattr(resp, "data", None)
            if not data:
                result: tuple[bool, str, int | None] = (False, "response had no 'data' field", None)
            else:
                first = data[0]
                vec = first.get("embedding") if isinstance(first, dict) else getattr(first, "embedding", None)
                if not vec:
                    result = (False, "first 'data' entry had no embedding", None)
                else:
                    dim = len(vec)
                    result = (True, f"ok (dim={dim})", dim)
        except Exception as e:
            result = (False, _format_exception(e), None)
        debug_output = get_debug() if not result[0] else ""
    if debug_output:
        sys.stderr.write(debug_output)
        sys.stderr.flush()
    return result


def _test_completion(model: str, key: str | None, base: str | None, *, timeout: float = 30.0) -> tuple[bool, str]:
    """Call litellm.completion once with a tiny prompt. Returns (ok, message).

    Verbose litellm debug output is buffered and only flushed to
    stderr on failure.
    """
    try:
        from litellm import completion  # type: ignore
    except ImportError as e:
        return False, f"litellm not installed: {e}"
    with _captured_litellm_debug() as get_debug:
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                "max_tokens": 10,
                "temperature": 0.0,
                "timeout": timeout,
            }
            if key:
                kwargs["api_key"] = key
            if base:
                kwargs["api_base"] = base
            resp = completion(**kwargs)
            choices = getattr(resp, "choices", None)
            if not choices:
                result: tuple[bool, str] = (False, "response had no choices")
            else:
                first = choices[0]
                msg = first.message if hasattr(first, "message") else first.get("message", {})
                text = (msg.content if hasattr(msg, "content") else msg.get("content", "")) or ""
                text = text.strip()[:60]
                result = (True, f"ok ({text!r})")
        except Exception as e:
            result = (False, _format_exception(e))
        debug_output = get_debug() if not result[0] else ""
    if debug_output:
        sys.stderr.write(debug_output)
        sys.stderr.flush()
    return result


def _test_rerank(model: str, key: str | None, base: str | None, *, timeout: float = 15.0) -> tuple[bool, str]:
    """Call litellm.rerank() once with two short documents.

    Mirrors what ``RerankApiReranker.probe`` does at startup, so a
    successful wizard test means the same call path that runs in
    production will succeed.
    """
    try:
        from litellm import rerank  # type: ignore
    except ImportError as e:
        return False, f"litellm not installed: {e}"
    with _captured_litellm_debug() as get_debug:
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "query": "ping",
                "documents": ["the quick brown fox", "hello world"],
                "top_n": 2,
                # Match the production call — see retrieval/rerank.py.
                "return_documents": False,
                "timeout": timeout,
            }
            if key:
                kwargs["api_key"] = key
            if base:
                kwargs["api_base"] = base
            resp = rerank(**kwargs)
            results = getattr(resp, "results", None)
            if results is None and isinstance(resp, dict):
                results = resp.get("results")
            if not results:
                result: tuple[bool, str] = (False, "rerank response had no 'results'")
            else:
                top = results[0]
                idx = top.get("index") if isinstance(top, dict) else getattr(top, "index", None)
                score = top.get("relevance_score") if isinstance(top, dict) else getattr(top, "relevance_score", None)
                result = (True, f"ok (top index={idx}, score={score:.4g})" if score is not None else "ok")
        except Exception as e:
            result = (False, _format_exception(e))
        debug_output = get_debug() if not result[0] else ""
    if debug_output:
        sys.stderr.write(debug_output)
        sys.stderr.flush()
    return result


def _confirm_test_failure(target_en: str, target_zh: str, error: str) -> str:
    """
    Show the test error and ask what to do. Returns one of:
      "retry" — re-run the same step
      "back"  — go back to the previous step (raise _GoBack to caller)
      "skip"  — accept the values without a successful test
      "abort" — stop the wizard
    """
    print()
    print(
        _c(
            _t(
                f"  ✗ {target_en} connection test FAILED:",
                f"  ✗ {target_zh} 连接测试失败：",
            ),
            "magenta",
        )
    )
    # The error block was previously dim-coloured, which on some Windows
    # consoles renders close to the background. Use yellow so it's always
    # visible — the same colour we already use elsewhere for warnings.
    for line in str(error).splitlines() or [str(error)]:
        print(_c(f"    {line}", "yellow"))
    print(
        _c(
            _t(
                "    (See litellm debug output above for the full HTTP request/response.)",
                "    (上方 litellm 调试日志包含完整请求/响应。)",
            ),
            "dim",
        )
    )
    return ask_choice(
        _t("What now?", "下一步？"),
        [
            ("retry", _t("fix the values and try again", "修改输入后重试")),
            ("back", _t("go back to the previous step", "返回上一步")),
            (
                "skip",
                _t("save anyway (you can fix it via /settings later)", "跳过测试，仍然保存（之后可在 /settings 修改）"),
            ),
            ("abort", _t("exit the wizard", "退出向导")),
        ],
        default="retry",
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _step_relational(answers: dict, defaults: dict) -> None:
    answers["relational"] = ask_choice(
        _t("Which metadata database?", "选择元数据库后端？"),
        [
            ("postgres", _t("multi-worker safe, recommended for production", "多 worker 安全，生产推荐")),
            ("sqlite", _t("single-process, zero extra services (dev / demo)", "单进程，无需额外服务（dev / demo）")),
        ],
        default=defaults.get("relational", "postgres"),
    )
    if answers["relational"] == "postgres":
        answers["pg_host"] = ask(_t("Postgres host", "Postgres 主机"), default=defaults.get("pg_host", "localhost"))
        answers["pg_port"] = ask_int(_t("Postgres port", "Postgres 端口"), default=defaults.get("pg_port", 5432))
        answers["pg_database"] = ask(
            _t("Postgres database", "Postgres 数据库名"), default=defaults.get("pg_database", "opencraig")
        )
        answers["pg_user"] = ask(_t("Postgres user", "Postgres 用户名"), default=defaults.get("pg_user", "opencraig"))
        answers["pg_password_env"] = ask(
            _t("Env var containing the password", "存放密码的环境变量名"),
            default=defaults.get("pg_password_env", "PG_PASSWORD"),
        )
    else:
        answers["sqlite_path"] = ask(
            _t("SQLite database file path", "SQLite 数据库文件路径"),
            default=defaults.get("sqlite_path", "./storage/opencraig.db"),
        )


def _step_vector(answers: dict, defaults: dict) -> None:
    standalone = [
        ("chromadb", _t("lightweight, backend-agnostic", "轻量级，后端无关")),
        ("qdrant", _t("production-grade, rich filtering", "生产级，丰富的过滤能力")),
        ("milvus", _t("scalable, GPU-accelerated", "可扩展，支持 GPU 加速")),
        ("weaviate", _t("multi-modal, GraphQL API", "多模态，GraphQL API")),
    ]
    # pgvector lives inside Postgres — only offer it when relational is postgres.
    if answers.get("relational") == "postgres":
        valid = [
            ("pgvector", _t("in-database, zero extra ops", "直接在 Postgres 内，无额外运维")),
            *standalone,
        ]
    else:
        valid = standalone
    default_vec = defaults.get("vector", valid[0][0])
    if default_vec not in [v for v, _ in valid]:
        default_vec = valid[0][0]
    answers["vector"] = ask_choice(
        _t("Which vector backend?", "选择向量数据库后端？"),
        valid,
        default=default_vec,
    )
    if answers["vector"] == "chromadb":
        answers["chroma_dir"] = ask(
            _t("Chroma persist_directory", "Chroma 持久化目录"),
            default=defaults.get("chroma_dir", "./storage/chroma"),
        )
    elif answers["vector"] == "qdrant":
        answers["qdrant_url"] = ask(
            _t("Qdrant server URL", "Qdrant 服务器 URL"),
            default=defaults.get("qdrant_url", "http://localhost:6333"),
        )
    elif answers["vector"] == "milvus":
        answers["milvus_uri"] = ask(
            _t("Milvus server URI", "Milvus 服务器 URI"),
            default=defaults.get("milvus_uri", "http://localhost:19530"),
        )
    elif answers["vector"] == "weaviate":
        answers["weaviate_url"] = ask(
            _t("Weaviate server URL", "Weaviate 服务器 URL"),
            default=defaults.get("weaviate_url", "http://localhost:8080"),
        )


def _step_blob(answers: dict, defaults: dict) -> None:
    answers["blob"] = ask_choice(
        _t("Where should blobs live?", "Blob (上传文件 + 图片) 存放在哪里？"),
        [
            ("local", _t("filesystem, single node", "本机文件系统")),
            ("s3", _t("any S3-compatible service", "任意 S3 兼容服务")),
            ("oss", _t("Alibaba Cloud OSS", "阿里云 OSS")),
        ],
        default=defaults.get("blob", "local"),
    )
    if answers["blob"] == "local":
        answers["blob_root"] = ask(
            _t("Blob root directory", "Blob 根目录"),
            default=defaults.get("blob_root", "./storage/blobs"),
        )
    elif answers["blob"] == "s3":
        answers["s3_endpoint"] = ask(_t("S3 endpoint URL", "S3 endpoint URL"), default="https://s3.amazonaws.com")
        answers["s3_bucket"] = ask(_t("S3 bucket name", "S3 bucket 名称"))
        answers["s3_region"] = ask(_t("S3 region", "S3 region"), default="us-east-1")
        answers["s3_access_key_env"] = ask(_t("Access key env var", "Access key 的环境变量名"), default="S3_ACCESS_KEY")
        answers["s3_secret_key_env"] = ask(_t("Secret key env var", "Secret key 的环境变量名"), default="S3_SECRET_KEY")
        answers["s3_public_base_url"] = ask(
            _t("Public CDN base URL (optional)", "公共 CDN 基础 URL (可选)"),
            default="",
            allow_empty=True,
        )
    elif answers["blob"] == "oss":
        answers["oss_endpoint"] = ask(
            _t("OSS endpoint", "OSS endpoint"),
            default="https://oss-cn-hangzhou.aliyuncs.com",
        )
        answers["oss_bucket"] = ask(_t("OSS bucket name", "OSS bucket 名称"))
        answers["oss_access_key_env"] = ask(
            _t("Access key env var", "Access key 的环境变量名"), default="OSS_ACCESS_KEY"
        )
        answers["oss_secret_key_env"] = ask(
            _t("Secret key env var", "Secret key 的环境变量名"), default="OSS_SECRET_KEY"
        )
        answers["oss_public_base_url"] = ask(
            _t("Public base URL (optional)", "公共 base URL (可选)"),
            default="",
            allow_empty=True,
        )


_PROVIDER_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
    "voyage": "VOYAGE_API_KEY",
    "ollama": "",  # local, no key by default
}


def _env_for_model(model: str | None) -> str:
    """Pick the conventional env var name for the model's provider prefix."""
    if not model:
        return "OPENAI_API_KEY"
    prefix = model.split("/", 1)[0].lower()
    return _PROVIDER_KEY_ENVS.get(prefix, "OPENAI_API_KEY")


def _ask_credentials(
    prefix_en: str,
    prefix_zh: str,
    defaults: dict,
    key_env: str,
    base_default: str,
    model: str | None = None,
) -> tuple[str, str, str]:
    """Common credential subform for embedder + LLM steps.

    Default flow optimised for the most common case (user has a key,
    wants to paste it):

      1. "API key" — paste it; saved plaintext into yaml. Done.
      2. If left blank, fall through to "Env var name" — useful when
         the operator already exported a key into the shell or wants
         to keep the secret out of the yaml file.
    """
    print(_c(_t(f"  {prefix_en} authentication", f"  {prefix_zh} 认证"), "dim"))

    default_env = defaults.get(key_env) or _env_for_model(model)
    env_already_set = bool(os.environ.get(default_env)) if default_env else False
    if env_already_set:
        key_prompt = _t(
            f"API key (paste it, or leave blank to use {default_env} env var — currently set)",
            f"API key (直接粘贴；留空则使用环境变量 {default_env} — 当前已设置)",
        )
    else:
        key_prompt = _t(
            "API key (paste it, or leave blank to use an env var instead)",
            "API key (直接粘贴；留空则改用环境变量)",
        )

    api_key_plain = ask(key_prompt, allow_empty=True)
    api_key_env = ""
    if not api_key_plain:
        api_key_env = ask(
            _t("Env var name holding the API key", "存放 API key 的环境变量名"),
            default=default_env,
            allow_empty=True,
        )
        if api_key_env and not os.environ.get(api_key_env):
            print(
                _c(
                    _t(
                        f"  ! env var {api_key_env!r} is currently unset — set it before running the server.",
                        f"  ! 环境变量 {api_key_env!r} 当前未设置 — 启动服务前需要设置。",
                    ),
                    "yellow",
                )
            )

    api_base = ask(
        _t(
            "Custom api_base (only needed for OpenAI-compatible / self-hosted endpoints — leave blank to use the provider default)",
            "自定义 api_base (仅当使用 OpenAI 兼容 / 自托管端点时填写 — 留空使用所选 provider 的默认端点)",
        ),
        default=defaults.get(
            "llm_api_base" if "llm" in prefix_en.lower() else "embedder_api_base",
            base_default,
        ),
        allow_empty=True,
    )
    return api_key_env, api_key_plain, api_base


def _step_graph(answers: dict, defaults: dict) -> None:
    """Pick the knowledge-graph backend.

    NetworkX is single-process JSON-persisted (no extra deps). Neo4j is
    the production choice for multi-worker deployments + larger KGs.
    Both are feature-equivalent — the same retrieval logic runs on top.
    """
    print(
        _c(
            _t(
                "  The knowledge graph stores entity-relation triples extracted",
                "  知识图谱存储 KG 抽取的实体关系三元组",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  during ingestion. Neo4j is the production-grade choice;",
                "  入库时生成。Neo4j 适合生产部署；",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  NetworkX is a single-process JSON-persisted alternative for",
                "  NetworkX 是单进程 JSON 持久化的轻量替代，适合",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  dev / demo / single-worker deployments (no extra service).",
                "  dev / demo / 单 worker 部署（不需要额外服务）。",
            ),
            "dim",
        )
    )
    answers["graph_backend"] = ask_choice(
        _t("Which graph backend?", "选择图数据库后端？"),
        [
            ("networkx", _t("single-process JSON, zero extra services", "单进程 JSON，无需额外服务")),
            ("neo4j", _t("multi-worker safe, recommended for production (5.11+)", "多 worker 安全，生产推荐（5.11+）")),
        ],
        default=defaults.get("graph_backend", "networkx"),
    )
    if answers["graph_backend"] == "neo4j":
        answers["neo4j_uri"] = ask(
            _t("Neo4j Bolt URI", "Neo4j Bolt URI"),
            default=defaults.get("neo4j_uri", "bolt://localhost:7687"),
        )
        answers["neo4j_user"] = ask(
            _t("Neo4j user", "Neo4j 用户"),
            default=defaults.get("neo4j_user", "neo4j"),
        )
        answers["neo4j_password_env"] = ask(
            _t("Env var holding the Neo4j password", "存放 Neo4j 密码的环境变量名"),
            default=defaults.get("neo4j_password_env", "NEO4J_PASSWORD"),
        )
        answers["neo4j_database"] = ask(
            _t("Neo4j database", "Neo4j 数据库名"),
            default=defaults.get("neo4j_database", "neo4j"),
        )
    else:
        answers["networkx_path"] = ask(
            _t("NetworkX JSON dump path", "NetworkX JSON 持久化文件路径"),
            default=defaults.get("networkx_path", "./storage/kg.json"),
        )


def _step_embedder(answers: dict, defaults: dict) -> None:
    while True:
        print(
            _c(
                _t(
                    "  The embedding model converts text into vectors. Examples:",
                    "  向量嵌入模型把文本转换为向量。常见模型：",
                ),
                "dim",
            )
        )
        print(
            _c(
                _t(
                    "    openai/text-embedding-3-small        # OpenAI, 1536-dim",
                    "    openai/text-embedding-3-small        # OpenAI, 1536-dim",
                ),
                "dim",
            )
        )
        print(
            _c(
                _t(
                    "    openai/text-embedding-3-large        # OpenAI, 3072-dim",
                    "    openai/text-embedding-3-large        # OpenAI, 3072-dim",
                ),
                "dim",
            )
        )
        print(
            _c(
                _t(
                    "    openai/BAAI/bge-m3                   # SiliconFlow, 1024-dim  (api_base = https://api.siliconflow.cn/v1)",
                    "    openai/BAAI/bge-m3                   # SiliconFlow, 1024-dim  (api_base = https://api.siliconflow.cn/v1)",
                ),
                "dim",
            )
        )
        print(
            _c(
                _t(
                    "    ollama/bge-m3                        # local Ollama, 1024-dim  (set api_base below)",
                    "    ollama/bge-m3                        # 本地 Ollama, 1024-dim （下一步设置 api_base）",
                ),
                "dim",
            )
        )
        answers["embedder_model"] = _sanitize_model_name(
            ask(
                _t("Embedding model (litellm format)", "嵌入模型 (litellm 格式)"),
                default=answers.get("embedder_model")
                or defaults.get("embedder_model", "openai/text-embedding-3-small"),
            )
        )
        api_key_env, api_key_plain, api_base = _ask_credentials(
            "Embedder",
            "嵌入模型",
            defaults,
            "embedder_api_key_env",
            "",
            model=answers["embedder_model"],
        )
        answers["embedder_api_key_env"] = api_key_env
        answers["embedder_api_key"] = api_key_plain
        answers["embedder_api_base"] = api_base

        # Live test — auto-detects the output dimension from the response.
        print()
        print(
            _c(
                _t(
                    "  testing embedding endpoint (auto-detecting dimension)…",
                    "  正在测试嵌入接口 (自动检测维度)…",
                ),
                "dim",
            )
        )
        key = _resolve_key(api_key_plain, api_key_env)
        ok, msg, dim = _test_embedding(answers["embedder_model"], key, api_base or None)
        if ok and dim:
            answers["embedder_dim"] = dim
            print(_c(f"  ✓ {msg}", "green"))
            print(
                _c(
                    _t(
                        f"  → embedder.dimension auto-set to {dim}",
                        f"  → embedder.dimension 自动设为 {dim}",
                    ),
                    "dim",
                )
            )
            return
        choice = _confirm_test_failure("Embedding", "嵌入模型", msg)
        if choice == "retry":
            continue
        if choice == "back":
            raise _GoBack()
        if choice == "abort":
            raise Aborted()
        # "skip" — fall back to asking for the dimension since we couldn't
        # detect it from a live call.
        print(
            _c(
                _t(
                    "  test was skipped — please enter the dimension manually.",
                    "  已跳过测试 — 请手动输入维度。",
                ),
                "yellow",
            )
        )
        answers["embedder_dim"] = ask_int(
            _t("Embedding dimension", "嵌入向量维度"),
            default=answers.get("embedder_dim") or defaults.get("embedder_dim", 1536),
        )
        return


def _step_llm(answers: dict, defaults: dict) -> None:
    while True:
        print(
            _c(
                _t(
                    "  The answer-generation LLM produces the final answer text. Examples:",
                    "  答案生成大模型负责输出最终的回答文本。常见模型：",
                ),
                "dim",
            )
        )
        print(
            _c(
                _t(
                    "    openai/gpt-4o-mini                       # OpenAI",
                    "    openai/gpt-4o-mini                       # OpenAI",
                ),
                "dim",
            )
        )
        print(
            _c(
                _t(
                    "    anthropic/claude-3-5-sonnet-latest       # Anthropic",
                    "    anthropic/claude-3-5-sonnet-latest       # Anthropic",
                ),
                "dim",
            )
        )
        print(
            _c(
                _t(
                    "    deepseek/deepseek-v4-flash               # DeepSeek V4-flash (official)",
                    "    deepseek/deepseek-v4-flash               # DeepSeek V4-flash (官方)",
                ),
                "dim",
            )
        )
        print(
            _c(
                _t(
                    "    deepseek/deepseek-v4-pro                 # DeepSeek V4-pro (official)",
                    "    deepseek/deepseek-v4-pro                 # DeepSeek V4-pro (官方)",
                ),
                "dim",
            )
        )
        print(
            _c(
                _t(
                    "    openai/deepseek-ai/DeepSeek-V4-Pro       # via SiliconFlow  (api_base = https://api.siliconflow.cn/v1)",
                    "    openai/deepseek-ai/DeepSeek-V4-Pro       # 通过 SiliconFlow  (api_base = https://api.siliconflow.cn/v1)",
                ),
                "dim",
            )
        )
        print(
            _c(
                _t(
                    "    ollama/qwen2.5                           # local Ollama  (set api_base below)",
                    "    ollama/qwen2.5                           # 本地 Ollama （下一步设置 api_base）",
                ),
                "dim",
            )
        )
        answers["llm_model"] = _sanitize_model_name(
            ask(
                _t("Generator model (litellm format)", "生成模型 (litellm 格式)"),
                default=answers.get("llm_model") or defaults.get("llm_model", "openai/gpt-4o-mini"),
            )
        )
        api_key_env, api_key_plain, api_base = _ask_credentials(
            "LLM",
            "大模型",
            defaults,
            "llm_api_key_env",
            "",
            model=answers["llm_model"],
        )
        answers["llm_api_key_env"] = api_key_env
        answers["llm_api_key"] = api_key_plain
        answers["llm_api_base"] = api_base

        # Live test
        print()
        print(
            _c(
                _t(
                    "  testing generator endpoint (one short completion call)…",
                    "  正在测试生成接口 (一次短补全调用)…",
                ),
                "dim",
            )
        )
        key = _resolve_key(api_key_plain, api_key_env)
        ok, msg = _test_completion(answers["llm_model"], key, api_base or None)
        if ok:
            print(_c(f"  ✓ {msg}", "green"))
            return
        choice = _confirm_test_failure("LLM", "大模型", msg)
        if choice == "retry":
            continue
        if choice == "back":
            raise _GoBack()
        if choice == "abort":
            raise Aborted()
        # "skip" — accept and move on
        return


def _step_parser_backend(answers: dict, defaults: dict) -> None:
    """Pick the PDF parser backend (no probe-driven fallback)."""
    print(
        _c(
            _t(
                "  Pick the PDF parser. PyMuPDF is fast and ships with the project;",
                "  选择 PDF 解析器。PyMuPDF 快且无额外依赖；",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  MinerU adds layout-aware parsing (tables / formulas / multi-column);",
                "  MinerU 启用版面感知解析（表格 / 公式 / 多栏）；",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  MinerU-VLM uses a vision model — best for scanned / handwritten /",
                "  MinerU-VLM 走视觉模型 — 最适合扫描件 / 手写 /",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  very complex layouts. Both MinerU options pull GBs of model weights.",
                "  极复杂版面。MinerU 两档都需下载几 GB 模型权重。",
            ),
            "dim",
        )
    )
    answers["parser_backend"] = ask_choice(
        _t("Which parser backend?", "选择解析器后端？"),
        [
            ("pymupdf", _t("fast, no extra deps", "快、无额外依赖")),
            ("mineru", _t("layout-aware (MinerU pipeline)", "版面感知 (MinerU pipeline 模式)")),
            ("mineru-vlm", _t("vision-model (MinerU VLM) — heaviest", "视觉模型 (MinerU VLM) — 最重")),
        ],
        default=defaults.get("parser_backend", "pymupdf"),
    )
    # 'mineru' install (if missing) is handled by sync_dependencies after
    # the wizard finishes — no per-step check needed.
    if answers["parser_backend"] == "mineru-vlm":
        url = ask(
            _t("Remote VLM server URL (leave blank for local inference)", "远端 VLM 服务器 URL（留空走本地推理）"),
            default=defaults.get("mineru_server_url", ""),
            allow_empty=True,
        )
        answers["mineru_server_url"] = url


def _print_dim_lines(*pairs: tuple[str, str]) -> None:
    """Print bilingual ``dim``-coloured lines, one (en, zh) tuple per line."""
    for en, zh in pairs:
        print(_c(_t(en, zh), "dim"))


def _ask_subsystem_override(
    answers: dict,
    defaults: dict,
    *,
    answer_key: str,
    label_en: str,
    label_zh: str,
) -> None:
    """Common tail of every subsystem step: ask reuse-vs-override and,
    on override, collect+test the new chat model. Rerank is the
    exception (it has a backend choice) and lives in ``_step_rerank``.
    """
    base_model = answers.get("llm_model") or "?"
    override = ask_bool(
        _t(
            f"Use a separate model for {label_en}? [N reuses {base_model}]",
            f"为 {label_zh} 单独配置模型？[N 复用 {base_model}]",
        ),
        default=False,
    )
    if not override:
        return
    _collect_chat_with_test(
        answers,
        defaults,
        base_model,
        answer_key=answer_key,
        label_en=label_en,
        label_zh=label_zh,
    )


# ---------------------------------------------------------------------------
# Per-subsystem steps. Each one starts with the rationale for overriding so
# the user can decide based on the actual cost/latency/quality tradeoff,
# not just because the wizard offers the option.
# ---------------------------------------------------------------------------


def _step_query_understanding(answers: dict, defaults: dict) -> None:
    """LLM that runs intent classification + 2-3 query paraphrases."""
    _print_dim_lines(
        (
            "  query_understanding runs on EVERY retrieve() call to extract intent",
            "  query_understanding 在每次检索时调用，分析查询意图并",
        ),
        (
            "  and generate 2-3 paraphrases. It's a tiny task, so a cheaper/faster",
            "  生成 2-3 个查询改写。任务很轻，用更便宜/更快的模型",
        ),
        (
            "  model here cuts query latency 30-50% with no impact on answer quality.",
            "  能降低 30-50% 检索延迟，不影响最终回答质量。",
        ),
        (
            "    benefit: lower per-query cost + lower TTFT (time-to-first-token)",
            "    收益：单次查询更便宜 + 首字延迟更低",
        ),
        (
            "    candidates: deepseek/deepseek-v4-flash, anthropic/claude-haiku, openai/gpt-4o-mini",
            "    候选：deepseek/deepseek-v4-flash、anthropic/claude-haiku、openai/gpt-4o-mini",
        ),
    )
    _ask_subsystem_override(
        answers,
        defaults,
        answer_key="query_understanding",
        label_en="query_understanding",
        label_zh="查询理解",
    )


def _step_rerank(answers: dict, defaults: dict) -> None:
    """Three backends with very different model conventions — special-cased."""
    _print_dim_lines(
        (
            "  rerank reorders the merged candidate set so the most relevant chunks",
            "  rerank 重新排序合并后的候选集，让最相关的 chunk",
        ),
        (
            "  reach the answer-LLM first. Three backends with different tradeoffs:",
            "  优先送进 answer-LLM。三种后端，权衡不同：",
        ),
        (
            "    llm_as_reranker  reuse a chat LLM as the rank judge — zero extra setup,",
            "    llm_as_reranker  复用聊天 LLM 当裁判 — 零额外配置，",
        ),
        (
            "                      but slow (1 LLM call per query) and cost scales with",
            "                      但慢（每次查询 1 次 LLM 调用），成本随候选数线性增长",
        ),
        ("                      candidate count.", ""),
        (
            "    rerank_api       dedicated cross-encoder (BGE / Cohere / Jina / Voyage) —",
            "    rerank_api       专用 cross-encoder（BGE / Cohere / Jina / Voyage）—",
        ),
        (
            "                      10-100x faster + cheaper, recommended for production.",
            "                      快 10-100 倍 + 便宜，生产环境推荐。",
        ),
        (
            "    passthrough      no rerank — RRF order goes straight through. Use for",
            "    passthrough      不重排 — RRF 顺序直出。仅用于",
        ),
        ("                      benchmark A/B baselines only.", "                      benchmark A/B 基线对比。"),
    )
    base_model = answers.get("llm_model") or "?"
    _customize_rerank(answers, defaults, base_model)


def _step_kg_extraction(answers: dict, defaults: dict) -> None:
    """Ingestion-time entity/relation extraction — runs once per chunk."""
    _print_dim_lines(
        (
            "  kg_extraction runs ONCE per chunk during ingest to extract entities",
            "  kg_extraction 入库时对每个 chunk 调用一次，抽取实体",
        ),
        (
            "  and relations into the knowledge graph. It's the most token-heavy",
            "  和关系写入知识图谱。这是 OpenCraig 中最耗 token 的",
        ),
        (
            "  subsystem in OpenCraig — a 1000-page corpus can hit it 20K+ times.",
            "  子系统 — 1000 页语料会触发 2 万+ 次调用。",
        ),
        (
            "    benefit: switching to a cheaper model can cut ingest cost 5-10x without",
            "    收益：换更便宜的模型可降 5-10 倍入库成本，",
        ),
        (
            "    a noticeable quality hit (entity extraction is well-handled by mid-tier).",
            "    质量几乎无损（实体抽取中等档位模型就能胜任）。",
        ),
        (
            "    candidates: deepseek/deepseek-v4-flash, openai/gpt-4o-mini",
            "    候选：deepseek/deepseek-v4-flash、openai/gpt-4o-mini",
        ),
    )
    _ask_subsystem_override(
        answers,
        defaults,
        answer_key="kg_extraction",
        label_en="kg_extraction",
        label_zh="KG 抽取",
    )


def _step_kg_path(answers: dict, defaults: dict) -> None:
    """Query-time entity extraction for graph traversal."""
    _print_dim_lines(
        (
            "  kg_path extracts entities from the user's query so the KG can",
            "  kg_path 从用户查询中抽取实体，让知识图谱能",
        ),
        (
            "  do graph traversal (1-hop neighbour lookup). Runs once per query,",
            "  做图遍历（1-hop 邻居查找）。每次查询调用一次，",
        ),
        (
            "  small task — same cheap-model rationale as query_understanding.",
            "  任务很轻 — 同 query_understanding，便宜模型即可。",
        ),
        ("    benefit: lower per-query cost", "    收益：单次查询成本更低"),
    )
    _ask_subsystem_override(
        answers,
        defaults,
        answer_key="kg_path",
        label_en="kg_path",
        label_zh="KG 检索",
    )


def _step_tree_builder_llm(answers: dict, defaults: dict) -> None:
    """Ingest-time tree builder — single page-group LLM pass produces
    the document hierarchy + per-section summaries."""
    _print_dim_lines(
        (
            "  tree_builder runs ONCE per document during ingest. The page-group",
            "  tree_builder 入库时每篇文档调用一次。page-group 策略",
        ),
        (
            "  strategy reads block excerpts in 5-page windows and emits the doc's",
            "  按 5 页一窗读取 block 摘要，让 LLM 给出文档的",
        ),
        (
            "  section structure + per-node titles + summaries in one shot — the",
            "  章节结构 + 节标题 + 节摘要，一次出齐 ——",
        ),
        (
            "  source format's headings (markdown #, PDF TOC) are passed only as",
            "  源格式的 heading（markdown #、PDF TOC）只作为提示，",
        ),
        (
            "  hints; the LLM owns structural decisions.",
            "  最终结构由 LLM 决定。",
        ),
        (
            "    benefit: same structural-task tier as KG extraction — a cheaper",
            "    收益：和 KG 抽取同档（结构化任务），便宜模型即可，",
        ),
        (
            "    model is fine; cost is ~1-3 LLM calls per doc (auto-batches when",
            "    单篇 ~1-3 次调用（超过 40k 字符自动分批）。",
        ),
        (
            "    text exceeds 40k chars).",
            "",
        ),
        (
            "    candidates: deepseek/deepseek-v4-flash, openai/gpt-4o-mini",
            "    候选：deepseek/deepseek-v4-flash、openai/gpt-4o-mini",
        ),
    )
    _ask_subsystem_override(
        answers,
        defaults,
        answer_key="tree_builder",
        label_en="tree_builder",
        label_zh="树构建",
    )


def _step_tree_path_nav(answers: dict, defaults: dict) -> None:
    """LLM tree navigator (PageIndex-style retrieval)."""
    _print_dim_lines(
        (
            "  tree_path.nav drives the PageIndex-style tree navigator: given the",
            "  tree_path.nav 驱动 PageIndex 风格树形导航器：根据查询",
        ),
        (
            "  query and the document outline, the LLM picks which sub-trees to",
            "  和文档大纲，LLM 选择该深入哪些子树（每文档 1-2 次调用）。",
        ),
        (
            "  descend into (1-2 calls per document). This is QUALITY-SENSITIVE —",
            "  这一步是 quality-sensitive — 选错子树意味着检索",
        ),
        ("  bad navigation = retrieval misses the right section entirely.", "  完全错过正确章节。"),
        (
            "    benefit: a STRONGER model here improves recall on long documents,",
            "    收益：在这里用更强的模型可提升长文档召回，",
        ),
        ("    even if your answer-LLM is already cheap.", "    即使你的 answer-LLM 已经很便宜也值得升级这一步。"),
        (
            "    candidates: openai/gpt-4o, anthropic/claude-3-5-sonnet, deepseek/deepseek-v4-pro",
            "    候选：openai/gpt-4o、anthropic/claude-3-5-sonnet、deepseek/deepseek-v4-pro",
        ),
    )
    _ask_subsystem_override(
        answers,
        defaults,
        answer_key="tree_path_nav",
        label_en="tree_path.nav",
        label_zh="树形导航",
    )


def _collect_chat_with_test(
    answers: dict,
    defaults: dict,
    base_model: str,
    *,
    answer_key: str,
    label_en: str,
    label_zh: str,
) -> None:
    """Prompt for a chat-model + creds and live-test with one completion.

    On test failure offers retry / back / skip / abort, mirroring the
    answer-LLM step. Persists the values into ``answers`` keyed by
    ``{answer_key}_model``, ``{answer_key}_api_key`` etc.
    """
    while True:
        model = _sanitize_model_name(
            ask(
                _t(f"  {label_en} model (litellm format)", f"  {label_zh} 模型 (litellm 格式)"),
                default=answers.get(f"{answer_key}_model") or base_model,
            )
        )
        api_key_env, api_key_plain, api_base = _ask_credentials(
            label_en,
            label_zh,
            defaults,
            f"{answer_key}_api_key_env",
            "",
            model=model,
        )
        print()
        print(
            _c(
                _t(
                    f"  testing {label_en} endpoint…",
                    f"  正在测试 {label_zh} 接口…",
                ),
                "dim",
            )
        )
        key_resolved = _resolve_key(api_key_plain, api_key_env)
        ok, msg = _test_completion(model, key_resolved, api_base or None)
        if ok:
            answers[f"{answer_key}_model"] = model
            answers[f"{answer_key}_api_key"] = api_key_plain
            answers[f"{answer_key}_api_key_env"] = api_key_env
            answers[f"{answer_key}_api_base"] = api_base
            print(_c(f"  ✓ {msg}", "green"))
            return
        choice = _confirm_test_failure(label_en, label_zh, msg)
        if choice == "retry":
            continue
        if choice == "back":
            raise _GoBack()
        if choice == "abort":
            raise Aborted()
        # skip — accept anyway
        answers[f"{answer_key}_model"] = model
        answers[f"{answer_key}_api_key"] = api_key_plain
        answers[f"{answer_key}_api_key_env"] = api_key_env
        answers[f"{answer_key}_api_base"] = api_base
        return


def _collect_rerank_api_with_test(
    answers: dict,
    defaults: dict,
) -> None:
    """Prompt for rerank-API model + creds and live-test with one rerank call."""
    while True:
        model = _sanitize_model_name(
            ask(
                _t("  rerank model (litellm rerank format)", "  rerank 模型 (litellm rerank 格式)"),
                default=answers.get("rerank_model") or defaults.get("rerank_model", "infinity/BAAI/bge-reranker-v2-m3"),
            )
        )
        api_key_env, api_key_plain, api_base = _ask_credentials(
            "rerank",
            "rerank",
            defaults,
            "rerank_api_key_env",
            "",
            model=model,
        )
        print()
        print(
            _c(
                _t(
                    "  testing rerank endpoint…",
                    "  正在测试 rerank 接口…",
                ),
                "dim",
            )
        )
        key_resolved = _resolve_key(api_key_plain, api_key_env)
        ok, msg = _test_rerank(model, key_resolved, api_base or None)
        if ok:
            answers["rerank_model"] = model
            answers["rerank_api_key"] = api_key_plain
            answers["rerank_api_key_env"] = api_key_env
            answers["rerank_api_base"] = api_base
            print(_c(f"  ✓ {msg}", "green"))
            return
        choice = _confirm_test_failure("rerank", "rerank", msg)
        if choice == "retry":
            continue
        if choice == "back":
            raise _GoBack()
        if choice == "abort":
            raise Aborted()
        answers["rerank_model"] = model
        answers["rerank_api_key"] = api_key_plain
        answers["rerank_api_key_env"] = api_key_env
        answers["rerank_api_base"] = api_base
        return


def _customize_rerank(answers: dict, defaults: dict, base_model: str) -> None:
    """Rerank has three backends with very different model conventions, so
    it's special-cased: ask which backend, then prompt for backend-specific
    model + credentials.

      * llm_as_reranker — chat LLM acts as a rank judge. Reuses the
        answer-LLM by default.
      * rerank_api      — dedicated cross-encoder endpoint via
        ``litellm.rerank()``. Model prefix MUST be one of
        ``huggingface/``, ``cohere/``, ``jina_ai/``, ``voyage/``,
        ``together_ai/`` — using ``openai/...`` here trips the
        ``Model does not exist`` error users hit at startup probe.
      * passthrough     — no rerank; RRF order goes straight through.
    """
    print(
        _c(
            _t(
                "  rerank — three backends:",
                "  rerank — 三种后端：",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "    llm_as_reranker  chat LLM acts as a rank judge (default, reuses answer-LLM)",
                "    llm_as_reranker  让聊天 LLM 当裁判排序（默认，复用 answer-LLM）",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "    rerank_api       dedicated cross-encoder (BGE / Cohere / Jina / Voyage)",
                "    rerank_api       专用 cross-encoder（BGE / Cohere / Jina / Voyage）",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "    passthrough      no rerank — RRF order goes through unchanged",
                "    passthrough      跳过 rerank — RRF 顺序直接输出",
            ),
            "dim",
        )
    )

    backend = ask_choice(
        _t("rerank backend?", "rerank 后端？"),
        [
            ("llm_as_reranker", _t("LLM acts as rank judge (reuse answer-LLM)", "LLM 当裁判（复用 answer-LLM）")),
            (
                "rerank_api",
                _t("dedicated cross-encoder (separate model+key)", "专用 cross-encoder（单独配置模型+key）"),
            ),
            ("passthrough", _t("no rerank — RRF order goes through", "跳过 rerank — RRF 顺序直出")),
        ],
        default=defaults.get("rerank_backend", "llm_as_reranker"),
    )
    answers["rerank_backend"] = backend

    if backend == "passthrough":
        return
    if backend == "llm_as_reranker":
        # Optionally pick a different chat model. Default = reuse answer-LLM.
        override = ask_bool(
            _t("Use a separate chat model for rerank?", "为 rerank 单独配置 chat 模型？"),
            default=False,
        )
        if not override:
            return
        _collect_chat_with_test(
            answers,
            defaults,
            base_model,
            answer_key="rerank",
            label_en="rerank",
            label_zh="rerank",
        )
        return

    # backend == "rerank_api" — dedicated cross-encoder endpoint.
    print()
    print(
        _c(
            _t(
                "  Cross-encoder model examples (prefix MUST be litellm rerank-compatible):",
                "  cross-encoder 模型示例（前缀必须是 litellm rerank 支持的）：",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "    infinity/BAAI/bge-reranker-v2-m3           # SiliconFlow BGE rerank  (api_base = https://api.siliconflow.cn/v1)",
                "    infinity/BAAI/bge-reranker-v2-m3           # SiliconFlow BGE rerank  (api_base = https://api.siliconflow.cn/v1)",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "    cohere/rerank-v3.5                         # Cohere",
                "    cohere/rerank-v3.5                         # Cohere",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "    jina_ai/jina-reranker-v2-base-multilingual # Jina AI",
                "    jina_ai/jina-reranker-v2-base-multilingual # Jina AI",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "    voyage/rerank-2                            # Voyage",
                "    voyage/rerank-2                            # Voyage",
            ),
            "dim",
        )
    )
    _collect_rerank_api_with_test(answers, defaults)


def _step_image_enrichment(answers: dict, defaults: dict) -> None:
    """Optional: enable VLM image enrichment (per-figure OCR + description).

    Reuses the answer-LLM credentials so the user doesn't have to enter
    them a second time. The LLM model itself must be vision-capable;
    if not, the user can change ``image_enrichment.model`` later by
    editing yaml. We default to the same model the user just picked.
    """
    print(
        _c(
            _t(
                "  Image enrichment uses a vision LLM to OCR + describe figures",
                "  图片增强会用视觉大模型对每张图做 OCR 与描述",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  during ingestion, so the figure becomes searchable as text.",
                "  注入到 chunk 文本里，图片内容也能被检索到。",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  Costs an extra LLM call per figure block — skip if you're",
                "  每张图多一次 LLM 调用 — 文档图很少时可以跳过。",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  ingesting text-heavy documents.",
                "",
            ),
            "dim",
        )
    )
    enable = ask_bool(
        _t("Enable image enrichment?", "启用图片增强？"),
        default=False,
    )
    answers["image_enrichment_enabled"] = enable
    if not enable:
        return
    print(
        _c(
            _t(
                "  Vision-capable model examples:",
                "  视觉大模型常见示例：",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "    openai/gpt-4o-mini                          # OpenAI",
                "    openai/gpt-4o-mini                          # OpenAI",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "    openai/Qwen/Qwen2-VL-72B-Instruct           # SiliconFlow  (api_base = https://api.siliconflow.cn/v1)",
                "    openai/Qwen/Qwen2-VL-72B-Instruct           # SiliconFlow  (api_base = https://api.siliconflow.cn/v1)",
            ),
            "dim",
        )
    )
    answers["image_enrichment_model"] = _sanitize_model_name(
        ask(
            _t("Vision LLM model (must be vision-capable)", "视觉大模型 (需要支持视觉输入)"),
            default=answers.get("llm_model") or "openai/gpt-4o-mini",
        )
    )


# ---------------------------------------------------------------------------
# The wizard
# ---------------------------------------------------------------------------


_STEPS: list[tuple[str, str, Callable[[dict, dict], None]]] = [
    ("Metadata database", "元数据库", _step_relational),
    ("Vector database", "向量数据库", _step_vector),
    ("Blob storage", "Blob 存储", _step_blob),
    ("Knowledge graph database", "知识图谱数据库", _step_graph),
    ("Parser backend", "PDF 解析器后端", _step_parser_backend),
    ("Embedding model", "向量嵌入模型", _step_embedder),
    ("Answer-generation LLM", "答案生成大模型", _step_llm),
    ("Query understanding LLM", "查询理解 LLM", _step_query_understanding),
    ("Rerank backend", "结果重排后端", _step_rerank),
    ("KG extraction LLM", "KG 抽取 LLM", _step_kg_extraction),
    ("KG retrieval LLM", "KG 检索 LLM", _step_kg_path),
    ("Tree builder LLM", "树构建 LLM", _step_tree_builder_llm),
    ("Tree navigator LLM", "树形导航 LLM", _step_tree_path_nav),
    ("Image enrichment (optional)", "图片增强 (可选)", _step_image_enrichment),
]


# ---------------------------------------------------------------------------
# Checkpoint / resume
#
# After every step we persist the running ``answers`` dict next to the
# would-be output yaml as ``<yaml>.wizard-state.json``. If the wizard is
# interrupted (Ctrl-C, pip-install crash, terminal close) the next run
# offers to resume from the last completed step instead of redoing
# everything. Erased on successful yaml write.
# ---------------------------------------------------------------------------


_CHECKPOINT_VERSION = 1


def _checkpoint_path(yaml_path: Path) -> Path:
    return yaml_path.with_suffix(yaml_path.suffix + ".wizard-state.json")


def _save_checkpoint(
    yaml_path: Path,
    answers: dict[str, Any],
    *,
    profile: str,
    lang: str,
    next_step_idx: int,
) -> None:
    import json

    payload = {
        "version": _CHECKPOINT_VERSION,
        "profile": profile,
        "lang": lang,
        "next_step_idx": next_step_idx,
        "answers": answers,
    }
    cp = _checkpoint_path(yaml_path)
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        # Checkpointing is best-effort — never abort the wizard for it.
        print(
            _c(
                _t(
                    f"  (warning: failed to save wizard checkpoint: {e})",
                    f"  （警告：保存向导进度失败：{e}）",
                ),
                "dim",
            )
        )


def _load_checkpoint(yaml_path: Path) -> dict[str, Any] | None:
    import json

    cp = _checkpoint_path(yaml_path)
    if not cp.exists():
        return None
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("version") != _CHECKPOINT_VERSION:
        return None
    return data


def _delete_checkpoint(yaml_path: Path) -> None:
    cp = _checkpoint_path(yaml_path)
    try:
        cp.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass  # not worth surfacing


def _non_interactive_defaults(profile: str) -> dict[str, Any]:
    d = _profile_defaults(profile)
    if not d:
        return d
    # Fill in fields the per-step functions would normally set so
    # build_config_dict has everything it needs without prompting.
    if d.get("relational") == "sqlite":
        d.setdefault("sqlite_path", "./storage/opencraig.db")
    else:
        d.setdefault("relational", "postgres")
        d.setdefault("pg_host", "localhost")
        d.setdefault("pg_port", 5432)
        d.setdefault("pg_database", "opencraig")
        d.setdefault("pg_user", "opencraig")
        d.setdefault("pg_password_env", "PG_PASSWORD")
    d.setdefault("blob_root", "./storage/blobs")
    if d.get("vector") == "chromadb":
        d.setdefault("chroma_dir", "./storage/chroma")
    d.setdefault("graph_backend", "networkx")
    if d["graph_backend"] == "networkx":
        d.setdefault("networkx_path", "./storage/kg.json")
    d.setdefault("embedder_api_key", "")
    d.setdefault("llm_api_key", "")
    return d


def run_wizard(
    profile: str,
    non_interactive: bool,
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Return a dict of answers that the yaml builder consumes.

    ``output_path`` is the path where the yaml will eventually be written;
    used as the anchor for the wizard-state.json checkpoint file. If
    omitted, no checkpointing happens (e.g. non-interactive runs).
    """
    defaults = _profile_defaults(profile)

    if non_interactive:
        d = _non_interactive_defaults(profile)
        if not d:
            print("error: --non-interactive requires --profile dev|prod", file=sys.stderr)
            raise Aborted()
        return d

    banner(_t("OpenCraig setup wizard", "OpenCraig 安装向导"))

    answers: dict[str, Any] = {}
    i = 0

    # ── Resume from a previous checkpoint? ──
    if output_path is not None:
        checkpoint = _load_checkpoint(output_path)
        if checkpoint is not None:
            cp_profile = checkpoint.get("profile", "?")
            cp_step = int(checkpoint.get("next_step_idx", 0))
            cp_step = max(0, min(cp_step, len(_STEPS)))
            cp_step_name_en = _STEPS[cp_step][0] if cp_step < len(_STEPS) else "(done)"
            cp_step_name_zh = _STEPS[cp_step][1] if cp_step < len(_STEPS) else "（已完成）"
            print(
                _c(
                    _t(
                        f"  Found unfinished setup from a previous run "
                        f"(profile={cp_profile}, next step: {cp_step_name_en}).",
                        f"  检测到上次未完成的安装进度 (profile={cp_profile}，下一步：{cp_step_name_zh})。",
                    ),
                    "yellow",
                )
            )
            choice = ask_choice(
                _t("Resume or start fresh?", "继续还是重新开始？"),
                [
                    ("resume", _t("resume from where I left off", "从中断处继续")),
                    ("fresh", _t("discard the checkpoint and start over", "丢弃进度，重新开始")),
                ],
                default="resume",
            )
            if choice == "resume":
                answers = dict(checkpoint.get("answers") or {})
                # If the saved language differs from the active one, keep
                # the active one (user just selected it on this run).
                i = cp_step
                if i >= len(_STEPS):
                    # Saved checkpoint says all steps done — caller will
                    # write yaml directly. Don't re-prompt anything.
                    return answers
            else:
                _delete_checkpoint(output_path)

    print(
        _c(
            _t(
                "  Press Enter to accept the default in [yellow].",
                "  按回车接受 [黄色] 中的默认值。",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  Type 'b' / 'back' / '<' to re-open the previous step.",
                "  在任何提问处输入 'b' / 'back' / '<' 可返回上一步。",
            ),
            "dim",
        )
    )
    print(
        _c(
            _t(
                "  Ctrl-C to abort. Progress is auto-saved after each step.",
                "  按 Ctrl-C 中止。每步完成后自动保存进度。",
            ),
            "dim",
        )
    )

    while i < len(_STEPS):
        title_en, title_zh, fn = _STEPS[i]
        section(f"{i + 1}/{len(_STEPS)}  {_t(title_en, title_zh)}")
        try:
            fn(answers, defaults)
        except _GoBack:
            if i == 0:
                print(
                    _c(
                        _t(
                            "  already at the first step — nowhere to go back.",
                            "  已经在第一步了 — 无法再返回。",
                        ),
                        "yellow",
                    )
                )
                continue
            i -= 1
            continue
        i += 1
        # Persist progress after each successfully completed step so a
        # subsequent crash / Ctrl-C can resume from i (the next pending one).
        if output_path is not None:
            _save_checkpoint(
                output_path,
                answers,
                profile=profile,
                lang=_LANG,
                next_step_idx=i,
            )

    section(_t("Done!", "完成！"))
    return answers


# ---------------------------------------------------------------------------
# YAML builder
# ---------------------------------------------------------------------------


def build_config_dict(a: dict[str, Any]) -> dict[str, Any]:
    cfg: dict[str, Any] = {}

    # --- parser (single explicit backend choice — no fallback chain) ---
    parser_block: dict[str, Any] = {"backend": a.get("parser_backend", "pymupdf")}
    if a.get("parser_backend") == "mineru-vlm" and a.get("mineru_server_url"):
        parser_block["backends"] = {"mineru": {"server_url": a["mineru_server_url"]}}
    cfg["parser"] = parser_block

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
    rel_backend = a.get("relational", "postgres")
    rel: dict[str, Any] = {"backend": rel_backend}
    if rel_backend == "postgres":
        rel["postgres"] = {
            "host": a["pg_host"],
            "port": a["pg_port"],
            "database": a["pg_database"],
            "user": a["pg_user"],
            "password_env": a["pg_password_env"],
        }
    else:  # sqlite
        rel["sqlite"] = {"path": a.get("sqlite_path", "./storage/opencraig.db")}

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
            "collection_name": "opencraig",
            "dimension": a["embedder_dim"],
            "distance": "cosine",
        }
    elif a["vector"] == "qdrant":
        vec["qdrant"] = {
            "url": a["qdrant_url"],
            "collection_name": "opencraig_chunks",
            "dimension": a["embedder_dim"],
            "distance": "cosine",
        }
    elif a["vector"] == "milvus":
        vec["milvus"] = {
            "uri": a["milvus_uri"],
            "collection_name": "opencraig_chunks",
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

    # --- knowledge graph ---
    graph_backend = a.get("graph_backend", "networkx")
    graph_block: dict[str, Any] = {"backend": graph_backend}
    if graph_backend == "neo4j":
        graph_block["neo4j"] = {
            "uri": a.get("neo4j_uri", "bolt://localhost:7687"),
            "user": a.get("neo4j_user", "neo4j"),
            "password_env": a.get("neo4j_password_env", "NEO4J_PASSWORD"),
            "database": a.get("neo4j_database", "neo4j"),
        }
    else:
        graph_block["networkx"] = {
            "path": a.get("networkx_path", "./storage/kg.json"),
        }
    cfg["graph"] = graph_block

    # --- embedder ---
    embedder_litellm: dict[str, Any] = {"model": a["embedder_model"]}
    if a.get("embedder_api_key"):
        embedder_litellm["api_key"] = a["embedder_api_key"]
    elif a.get("embedder_api_key_env"):
        embedder_litellm["api_key_env"] = a["embedder_api_key_env"]
    if a.get("embedder_api_base"):
        embedder_litellm["api_base"] = a["embedder_api_base"]
    cfg["embedder"] = {
        "backend": "litellm",
        "dimension": a["embedder_dim"],
        "litellm": embedder_litellm,
    }

    # --- answering generator ---
    generator: dict[str, Any] = {"backend": "litellm", "model": a["llm_model"]}
    if a.get("llm_api_key"):
        generator["api_key"] = a["llm_api_key"]
    elif a.get("llm_api_key_env"):
        generator["api_key_env"] = a["llm_api_key_env"]
    if a.get("llm_api_base"):
        generator["api_base"] = a["llm_api_base"]
    cfg["answering"] = {"generator": generator}

    # --- retrieval: every LLM-using subsystem either inherits the
    #     answer-LLM creds (default) or uses an explicit override
    #     captured by ``_step_llm_subsystems``. ---
    def _llm_creds_block() -> dict[str, Any]:
        block: dict[str, Any] = {"model": a["llm_model"]}
        if a.get("llm_api_key"):
            block["api_key"] = a["llm_api_key"]
        elif a.get("llm_api_key_env"):
            block["api_key_env"] = a["llm_api_key_env"]
        if a.get("llm_api_base"):
            block["api_base"] = a["llm_api_base"]
        return block

    def _subsystem_creds(key: str) -> dict[str, Any]:
        """Return creds block for a subsystem, honouring per-subsystem
        overrides from the LLM-routing step; falls back to answer-LLM
        creds when no override was given."""
        if not a.get(f"{key}_model"):
            return _llm_creds_block()
        block: dict[str, Any] = {"model": a[f"{key}_model"]}
        if a.get(f"{key}_api_key"):
            block["api_key"] = a[f"{key}_api_key"]
        elif a.get(f"{key}_api_key_env"):
            block["api_key_env"] = a[f"{key}_api_key_env"]
        if a.get(f"{key}_api_base"):
            block["api_base"] = a[f"{key}_api_base"]
        return block

    # rerank has a backend choice (passthrough / rerank_api / llm_as_reranker)
    # captured separately by ``_customize_rerank``. Default backend is
    # llm_as_reranker (which reuses answer-LLM creds via _subsystem_creds).
    rerank_block: dict[str, Any] = {}
    rerank_backend = a.get("rerank_backend", "llm_as_reranker")
    if rerank_backend != "llm_as_reranker":
        rerank_block["backend"] = rerank_backend
    if rerank_backend == "passthrough":
        # No model/creds needed; just pin the backend.
        pass
    else:
        rerank_block.update(_subsystem_creds("rerank"))

    cfg["retrieval"] = {
        "query_understanding": _subsystem_creds("query_understanding"),
        "rerank": rerank_block,
        "kg_extraction": _subsystem_creds("kg_extraction"),
        "kg_path": _subsystem_creds("kg_path"),
        "tree_path": {"nav": _subsystem_creds("tree_path_nav")},
    }

    # Tree builder lives under ``parser.tree_builder`` (ingest-time, not
    # retrieval). Emit it here AFTER ``_subsystem_creds`` is in scope so
    # the reuse-vs-override resolution matches every other LLM stage.
    # ``llm_enabled: true`` is set explicitly so a user reading the
    # generated yaml sees the kill-switch — flip to false to skip the
    # LLM entirely and fall back to flat-tree (chunking still works).
    cfg["parser"]["tree_builder"] = {"llm_enabled": True, **_subsystem_creds("tree_builder")}

    # --- image enrichment (optional — wizard step asks) ---
    if a.get("image_enrichment_enabled"):
        ie: dict[str, Any] = {
            "enabled": True,
            "model": a.get("image_enrichment_model") or a["llm_model"],
        }
        # Reuse the answer-LLM credentials.
        if a.get("llm_api_key"):
            ie["api_key"] = a["llm_api_key"]
        elif a.get("llm_api_key_env"):
            ie["api_key_env"] = a["llm_api_key_env"]
        if a.get("llm_api_base"):
            ie["api_base"] = a["llm_api_base"]
        cfg["image_enrichment"] = ie

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


def _child_env() -> dict[str, str]:
    """Subprocess env that inherits ours plus forces UTF-8 stdio so child
    Python processes don't crash on box-drawing / arrow chars under
    Windows GBK consoles."""
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def post_setup(config_path: Path) -> None:
    section(_t("Next steps", "下一步"))

    # Run config validator (subprocess isolates us from pydantic issues)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "config", "validate", str(config_path)],
            cwd=_ROOT,
            env=_child_env(),
        )
        if r.returncode != 0:
            print(
                _c(
                    _t(
                        "  config validation FAILED — fix the file and re-run",
                        "  配置校验失败 — 请修正后重新运行",
                    ),
                    "magenta",
                )
            )
            return
    except FileNotFoundError:
        pass

    print()
    choice = ask_choice(
        _t("What do you want to do next?", "接下来要做什么？"),
        [
            ("nothing", _t("just exit; run it yourself later", "什么都不做；稍后自己启动")),
            ("batch", _t("batch-ingest files from a directory now", "立刻批量导入指定目录的文件")),
            ("api", _t("start the HTTP API (uvicorn) now", "立刻启动 HTTP API (uvicorn)")),
        ],
        default="nothing",
    )
    if choice == "nothing":
        print()
        print(
            _c(
                _t(
                    "  done. to use this config later:",
                    "  完成。下次使用此配置：",
                ),
                "dim",
            )
        )
        print(f"    export OPENCRAIG_CONFIG={config_path}")
        return

    if choice == "batch":
        target = ask(
            _t("Directory to ingest", "要导入的目录"),
            default="./papers",
            validator=lambda p: None if Path(p).exists() else _t(f"not found: {p}", f"目录不存在: {p}"),
        )
        embed = ask_bool(_t("Compute embeddings?", "同时计算向量嵌入？"), default=False)
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
        subprocess.run(cmd, cwd=_ROOT, env=_child_env())
        return

    if choice == "api":
        host = ask(_t("Host", "监听地址"), default="0.0.0.0")
        port = ask_int(_t("Port", "监听端口"), default=8000)
        env = _child_env()
        env["OPENCRAIG_CONFIG"] = str(config_path)
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
        print(_c(f"\n  OPENCRAIG_CONFIG={config_path}", "dim"))
        print(_c(f"  running: {' '.join(cmd)}\n", "dim"))
        try:
            subprocess.run(cmd, cwd=_ROOT, env=env)
        except KeyboardInterrupt:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


_HELP_DESCRIPTION = """\
Interactive setup wizard for OpenCraig.

Walks through five small steps and writes a opencraig.yaml that wires
together the relational store, vector store, blob storage, embedder,
and answer-generation LLM. The embedder and LLM steps each finish with
a real connection test (live API call) so a typo in api_base or a
wrong key surfaces immediately.

Type 'b' / 'back' / '<' at any prompt to re-open the previous step.

All LLM / embedding backends go through litellm, which accepts custom
endpoints via an api_base setting -- so Ollama, vLLM, OneAPI,
OpenRouter, DeepSeek, Azure, any OpenAI-compatible server all work
with the same model string.
"""

_HELP_EPILOG = """\
Profiles
--------
  dev    ChromaDB + local blob + OpenAI defaults.
         Zero infrastructure beyond Postgres, good for local exper.

  prod   pgvector + local blob + OpenAI defaults.
         Recommended for a single production node.

  custom Full wizard, no presets. Use when you know what you want.

LLM / model configuration
-------------------------
  Model + api_key + api_base ARE set here so the wizard can verify
  the connection live. Other tunables (temperature, prompts,
  retrieval strategy) remain runtime-editable via /settings.

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
  python -m config validate opencraig.yaml

  # Point everything at it:
  export OPENCRAIG_CONFIG=./opencraig.yaml

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
        help="Preset defaults. dev=chromadb+local; prod=pgvector+local.",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("./opencraig.yaml"),
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
    p.add_argument(
        "--sync-deps",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Skip the wizard. Read PATH (existing opencraig.yaml), "
            "compute the optional pip packages it requires, and "
            "install any that are missing. Useful after a yaml edit."
        ),
    )
    return p.parse_args()


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as e:
        raise RuntimeError("pyyaml not installed: pip install pyyaml") from e
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    args = parse_args()

    # ── --sync-deps: standalone path, no wizard ──
    if args.sync_deps is not None:
        path = args.sync_deps
        if not path.exists():
            print(
                _c(
                    _t(
                        f"  {path} not found.",
                        f"  未找到 {path}。",
                    ),
                    "magenta",
                )
            )
            return 2
        try:
            cfg_dict = _load_yaml(path)
        except Exception as e:
            print(
                _c(
                    _t(
                        f"  failed to read {path}: {e}",
                        f"  读取 {path} 失败：{e}",
                    ),
                    "magenta",
                )
            )
            return 1
        section(_t("Syncing optional dependencies", "同步可选依赖"))
        installed, total = sync_dependencies(cfg_dict)
        print()
        print(
            _c(
                _t(
                    f"  {installed}/{total} package(s) installed (others already present).",
                    f"  本次安装 {installed}/{total} 个包（其余已就位）。",
                ),
                "green",
            )
        )
        return 0

    # Pick the wizard's display language before any other output. Skipped
    # entirely in non-interactive mode (CI / Docker) where English is the
    # safest default for log greppability.
    global _LANG
    if not args.non_interactive:
        _LANG = _select_language()

    if args.output.exists() and not args.force:
        print(
            _c(
                _t(
                    f"  {args.output} already exists. Use --force to overwrite.",
                    f"  {args.output} 已存在。使用 --force 强制覆盖。",
                ),
                "magenta",
            )
        )
        return 2

    try:
        answers = run_wizard(
            args.profile,
            args.non_interactive,
            output_path=None if args.non_interactive else args.output,
        )
    except Aborted:
        print(
            _t(
                "\n  aborted. Progress saved — re-run the wizard to resume.",
                "\n  已中止。进度已保存 — 重新运行向导可继续。",
            )
        )
        return 130

    cfg_dict = build_config_dict(answers)
    try:
        write_yaml(cfg_dict, args.output)
    except Exception as e:
        print(
            _c(
                _t(
                    f"  failed to write {args.output}: {e}",
                    f"  写入 {args.output} 失败：{e}",
                ),
                "magenta",
            )
        )
        return 1

    # Yaml is durable now — the wizard's job is done. Remove the
    # checkpoint so the next run doesn't offer a stale resume.
    if not args.non_interactive:
        _delete_checkpoint(args.output)

    print()
    print(_c(_t(f"  wrote {args.output}", f"  已写入 {args.output}"), "green"))
    print()

    # Now that we know exactly what backends the user picked, install only
    # those optional packages — the user did not have to ``pip install -r``
    # anything beyond the small core set.
    section(_t("Installing optional dependencies", "安装可选依赖"))
    sync_dependencies(cfg_dict)

    try:
        post_setup(args.output)
    except Aborted:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
