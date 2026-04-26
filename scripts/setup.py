"""
Interactive setup wizard for ForgeRAG.

Walks the user through six small steps and writes a forgerag.yaml
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
import importlib.util
import os
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
# Optional backend → required package (import_name, pip_name)
# ---------------------------------------------------------------------------

_RELATIONAL_PACKAGES: dict[str, tuple[str, str]] = {
    "postgres": ("psycopg", "psycopg[binary]"),
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
    print(_c(_t(
        f"  '{pip_name}' is not installed — installing now…",
        f"  '{pip_name}' 未安装 — 现在自动安装…",
    ), "yellow"))
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pip_name],
            check=True,
        )
        print(_c(_t(
            f"  '{pip_name}' installed successfully.",
            f"  '{pip_name}' 已安装成功。",
        ), "green"))
    except subprocess.CalledProcessError as exc:
        print(_c(_t(
            f"  failed to install '{pip_name}': {exc}",
            f"  安装 '{pip_name}' 失败：{exc}",
        ), "magenta"))
        print(_c(_t(
            f"  install it manually and re-run: pip install {pip_name}",
            f"  请手动安装后重试：pip install {pip_name}",
        ), "dim"))


def _ensure_backend_package(mapping: dict[str, tuple[str, str]], backend: str) -> None:
    """Look up *mapping* and install its package if missing."""
    if backend not in mapping:
        return
    import_name, pip_name = mapping[backend]
    _ensure_package(import_name, pip_name)


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
        print(_c(_t(
            f"  please enter a number 1-{len(options)}",
            f"  请输入 1-{len(options)} 之间的数字",
        ), "magenta"))


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
            print(_c(_t(
                f"  please enter an integer >= {min_}",
                f"  请输入 >= {min_} 的整数",
            ), "magenta"))


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
        return {
            **base,
            "vector": "chromadb",
            "chroma_dir": "./storage/chroma",
            "blob": "local",
            "blob_root": "./storage/blobs",
        }
    if profile == "prod":
        return {
            **base,
            "pg_host": "localhost",
            "pg_port": 5432,
            "pg_database": "forgerag",
            "pg_user": "forgerag",
            "pg_password_env": "PG_PASSWORD",
            "vector": "pgvector",
            "embedder_dim": 1024,
            "blob": "local",
            "blob_root": "./storage/blobs",
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


def _test_embedding(
    model: str, key: str | None, base: str | None, *, timeout: float = 30.0
) -> tuple[bool, str, int | None]:
    """Call litellm.embedding once with a short input.

    Returns ``(ok, message, dim)``. ``dim`` is the detected output
    dimension on success and None on any failure.
    """
    try:
        from litellm import embedding  # type: ignore
    except ImportError as e:
        return False, f"litellm not installed: {e}", None
    try:
        kwargs: dict[str, Any] = {
            "model": model,
            "input": ["ping"],
            "timeout": timeout,
        }
        if key:
            kwargs["api_key"] = key
        if base:
            kwargs["api_base"] = base
        resp = embedding(**kwargs)
        # Probe the response shape so a misconfigured proxy that returns
        # 200 OK with non-embedding JSON still surfaces a clear error.
        data = getattr(resp, "data", None)
        if not data:
            return False, "response had no 'data' field", None
        first = data[0]
        vec = first.get("embedding") if isinstance(first, dict) else getattr(first, "embedding", None)
        if not vec:
            return False, "first 'data' entry had no embedding", None
        dim = len(vec)
        return True, f"ok (dim={dim})", dim
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None


def _test_completion(model: str, key: str | None, base: str | None, *, timeout: float = 30.0) -> tuple[bool, str]:
    """Call litellm.completion once with a tiny prompt. Returns (ok, message)."""
    try:
        from litellm import completion  # type: ignore
    except ImportError as e:
        return False, f"litellm not installed: {e}"
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
            return False, "response had no choices"
        first = choices[0]
        msg = first.message if hasattr(first, "message") else first.get("message", {})
        text = (msg.content if hasattr(msg, "content") else msg.get("content", "")) or ""
        text = text.strip()[:60]
        return True, f"ok ({text!r})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _confirm_test_failure(target_en: str, target_zh: str, error: str) -> str:
    """
    Show the test error and ask what to do. Returns one of:
      "retry" — re-run the same step
      "back"  — go back to the previous step (raise _GoBack to caller)
      "skip"  — accept the values without a successful test
      "abort" — stop the wizard
    """
    print()
    print(_c(_t(
        f"  ✗ {target_en} connection test FAILED:",
        f"  ✗ {target_zh} 连接测试失败：",
    ), "magenta"))
    print(_c(f"    {error}", "dim"))
    return ask_choice(
        _t("What now?", "下一步？"),
        [
            ("retry", _t("fix the values and try again",                 "修改输入后重试")),
            ("back",  _t("go back to the previous step",                 "返回上一步")),
            ("skip",  _t("save anyway (you can fix it via /settings later)",
                         "跳过测试，仍然保存（之后可在 /settings 修改）")),
            ("abort", _t("exit the wizard",                              "退出向导")),
        ],
        default="retry",
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _step_postgres(answers: dict, defaults: dict) -> None:
    answers["relational"] = "postgres"
    answers["pg_host"] = ask(_t("Postgres host", "Postgres 主机"),
        default=defaults.get("pg_host", "localhost"))
    answers["pg_port"] = ask_int(_t("Postgres port", "Postgres 端口"),
        default=defaults.get("pg_port", 5432))
    answers["pg_database"] = ask(_t("Postgres database", "Postgres 数据库名"),
        default=defaults.get("pg_database", "forgerag"))
    answers["pg_user"] = ask(_t("Postgres user", "Postgres 用户名"),
        default=defaults.get("pg_user", "forgerag"))
    answers["pg_password_env"] = ask(
        _t("Env var containing the password", "存放密码的环境变量名"),
        default=defaults.get("pg_password_env", "PG_PASSWORD"),
    )
    _ensure_backend_package(_RELATIONAL_PACKAGES, answers["relational"])


def _step_vector(answers: dict, defaults: dict) -> None:
    standalone = [
        ("chromadb", _t("ChromaDB — lightweight, backend-agnostic",
                        "ChromaDB — 轻量级，后端无关")),
        ("qdrant",   _t("Qdrant — production-grade, rich filtering",
                        "Qdrant — 生产级，丰富的过滤能力")),
        ("milvus",   _t("Milvus — scalable, GPU-accelerated",
                        "Milvus — 可扩展，支持 GPU 加速")),
        ("weaviate", _t("Weaviate — multi-modal, GraphQL API",
                        "Weaviate — 多模态，GraphQL API")),
    ]
    valid = [
        ("pgvector", _t("pgvector — in-database, zero extra ops",
                        "pgvector — 直接在 Postgres 内，无额外运维")),
        *standalone,
    ]
    default_vec = defaults.get("vector", valid[0][0])
    if default_vec not in [v for v, _ in valid]:
        default_vec = valid[0][0]
    answers["vector"] = ask_choice(
        _t("Which vector backend?", "选择向量数据库后端？"),
        valid, default=default_vec,
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
    _ensure_backend_package(_VECTOR_PACKAGES, answers["vector"])


def _step_blob(answers: dict, defaults: dict) -> None:
    answers["blob"] = ask_choice(
        _t("Where should blobs live?", "Blob (上传文件 + 图片) 存放在哪里？"),
        [
            ("local", _t("filesystem, single node",  "本机文件系统")),
            ("s3",    _t("any S3-compatible service", "任意 S3 兼容服务")),
            ("oss",   _t("Alibaba Cloud OSS",        "阿里云 OSS")),
        ],
        default=defaults.get("blob", "local"),
    )
    if answers["blob"] == "local":
        answers["blob_root"] = ask(
            _t("Blob root directory", "Blob 根目录"),
            default=defaults.get("blob_root", "./storage/blobs"),
        )
    elif answers["blob"] == "s3":
        answers["s3_endpoint"] = ask(_t("S3 endpoint URL", "S3 endpoint URL"),
            default="https://s3.amazonaws.com")
        answers["s3_bucket"] = ask(_t("S3 bucket name", "S3 bucket 名称"))
        answers["s3_region"] = ask(_t("S3 region", "S3 region"), default="us-east-1")
        answers["s3_access_key_env"] = ask(
            _t("Access key env var", "Access key 的环境变量名"), default="S3_ACCESS_KEY")
        answers["s3_secret_key_env"] = ask(
            _t("Secret key env var", "Secret key 的环境变量名"), default="S3_SECRET_KEY")
        answers["s3_public_base_url"] = ask(
            _t("Public CDN base URL (optional)", "公共 CDN 基础 URL (可选)"),
            default="", allow_empty=True,
        )
    elif answers["blob"] == "oss":
        answers["oss_endpoint"] = ask(
            _t("OSS endpoint", "OSS endpoint"),
            default="https://oss-cn-hangzhou.aliyuncs.com",
        )
        answers["oss_bucket"] = ask(_t("OSS bucket name", "OSS bucket 名称"))
        answers["oss_access_key_env"] = ask(
            _t("Access key env var", "Access key 的环境变量名"), default="OSS_ACCESS_KEY")
        answers["oss_secret_key_env"] = ask(
            _t("Secret key env var", "Secret key 的环境变量名"), default="OSS_SECRET_KEY")
        answers["oss_public_base_url"] = ask(
            _t("Public base URL (optional)", "公共 base URL (可选)"),
            default="", allow_empty=True,
        )
    _ensure_backend_package(_BLOB_PACKAGES, answers["blob"])


def _ask_credentials(
    prefix_en: str, prefix_zh: str, defaults: dict, key_env: str, base_default: str
) -> tuple[str, str, str]:
    """Common credential subform for embedder + LLM steps."""
    print(_c(_t(f"  {prefix_en} authentication", f"  {prefix_zh} 认证"), "dim"))
    api_key_env = ask(
        _t("Env var containing the API key", "存放 API key 的环境变量名"),
        default=defaults.get(key_env, "OPENAI_API_KEY"),
        allow_empty=True,
    )
    api_key_plain = ""
    if api_key_env and not os.environ.get(api_key_env):
        print(_c(_t(
            f"  ! env var {api_key_env!r} is currently unset.",
            f"  ! 环境变量 {api_key_env!r} 当前未设置。",
        ), "yellow"))
        if ask_bool(
            _t("Paste the key now (saved as plaintext in yaml)?",
               "现在直接粘贴 key？(将以明文写入 yaml)"),
            default=False,
        ):
            api_key_plain = ask(_t("API key", "API key"), allow_empty=False)
            api_key_env = ""
    api_base = ask(
        _t("Custom api_base (Ollama / OpenRouter / OneAPI / Azure URL)",
           "自定义 api_base (Ollama / OpenRouter / OneAPI / Azure 地址)"),
        default=defaults.get(
            "llm_api_base" if "llm" in prefix_en.lower() else "embedder_api_base",
            base_default,
        ),
        allow_empty=True,
    )
    return api_key_env, api_key_plain, api_base


def _step_embedder(answers: dict, defaults: dict) -> None:
    while True:
        print(_c(_t(
            "  The embedding model converts text into vectors.",
            "  向量嵌入模型把文本转换为向量。",
        ), "dim"))
        print(_c(_t(
            "  Common: openai/text-embedding-3-small (1536),",
            "  常见模型: openai/text-embedding-3-small (1536),",
        ), "dim"))
        print(_c(_t(
            "  openai/text-embedding-3-large (3072), ollama/bge-m3 (1024).",
            "  openai/text-embedding-3-large (3072), ollama/bge-m3 (1024)。",
        ), "dim"))
        answers["embedder_model"] = ask(
            _t("Embedding model (litellm format)", "嵌入模型 (litellm 格式)"),
            default=answers.get("embedder_model")
                or defaults.get("embedder_model", "openai/text-embedding-3-small"),
        )
        api_key_env, api_key_plain, api_base = _ask_credentials(
            "Embedder", "嵌入模型", defaults, "embedder_api_key_env", ""
        )
        answers["embedder_api_key_env"] = api_key_env
        answers["embedder_api_key"] = api_key_plain
        answers["embedder_api_base"] = api_base

        # Live test — auto-detects the output dimension from the response.
        print()
        print(_c(_t(
            "  testing embedding endpoint (auto-detecting dimension)…",
            "  正在测试嵌入接口 (自动检测维度)…",
        ), "dim"))
        key = _resolve_key(api_key_plain, api_key_env)
        ok, msg, dim = _test_embedding(answers["embedder_model"], key, api_base or None)
        if ok and dim:
            answers["embedder_dim"] = dim
            print(_c(f"  ✓ {msg}", "green"))
            print(_c(_t(
                f"  → embedder.dimension auto-set to {dim}",
                f"  → embedder.dimension 自动设为 {dim}",
            ), "dim"))
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
        print(_c(_t(
            "  test was skipped — please enter the dimension manually.",
            "  已跳过测试 — 请手动输入维度。",
        ), "yellow"))
        answers["embedder_dim"] = ask_int(
            _t("Embedding dimension", "嵌入向量维度"),
            default=answers.get("embedder_dim") or defaults.get("embedder_dim", 1536),
        )
        return


def _step_llm(answers: dict, defaults: dict) -> None:
    while True:
        print(_c(_t(
            "  The answer-generation LLM produces the final answer text.",
            "  答案生成大模型负责输出最终的回答文本。",
        ), "dim"))
        print(_c(_t(
            "  Any litellm-compatible model works (OpenAI / Anthropic / DeepSeek /",
            "  支持任何 litellm 兼容模型 (OpenAI / Anthropic / DeepSeek /",
        ), "dim"))
        print(_c(_t(
            "  Ollama / OpenRouter / Azure / Bedrock / Vertex / ...).",
            "  Ollama / OpenRouter / Azure / Bedrock / Vertex / …)。",
        ), "dim"))
        answers["llm_model"] = ask(
            _t("Generator model (litellm format)", "生成模型 (litellm 格式)"),
            default=answers.get("llm_model")
                or defaults.get("llm_model", "openai/gpt-4o-mini"),
        )
        api_key_env, api_key_plain, api_base = _ask_credentials(
            "LLM", "大模型", defaults, "llm_api_key_env", ""
        )
        answers["llm_api_key_env"] = api_key_env
        answers["llm_api_key"] = api_key_plain
        answers["llm_api_base"] = api_base

        # Live test
        print()
        print(_c(_t(
            "  testing generator endpoint (one short completion call)…",
            "  正在测试生成接口 (一次短补全调用)…",
        ), "dim"))
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
    print(_c(_t(
        "  Pick the PDF parser. PyMuPDF is fast and ships with the project;",
        "  选择 PDF 解析器。PyMuPDF 快且无额外依赖；",
    ), "dim"))
    print(_c(_t(
        "  MinerU adds layout-aware parsing (tables / formulas / multi-column);",
        "  MinerU 启用版面感知解析（表格 / 公式 / 多栏）；",
    ), "dim"))
    print(_c(_t(
        "  MinerU-VLM uses a vision model — best for scanned / handwritten /",
        "  MinerU-VLM 走视觉模型 — 最适合扫描件 / 手写 /",
    ), "dim"))
    print(_c(_t(
        "  very complex layouts. Both MinerU options pull GBs of model weights.",
        "  极复杂版面。MinerU 两档都需下载几 GB 模型权重。",
    ), "dim"))
    answers["parser_backend"] = ask_choice(
        _t("Which parser backend?", "选择解析器后端？"),
        [
            ("pymupdf",    _t("fast, no extra deps",
                             "快、无额外依赖")),
            ("mineru",     _t("layout-aware (MinerU pipeline)",
                             "版面感知 (MinerU pipeline 模式)")),
            ("mineru-vlm", _t("vision-model (MinerU VLM) — heaviest",
                             "视觉模型 (MinerU VLM) — 最重")),
        ],
        default=defaults.get("parser_backend", "pymupdf"),
    )
    if answers["parser_backend"] in ("mineru", "mineru-vlm"):
        from importlib.util import find_spec
        if find_spec("mineru") is None:
            print(_c(_t(
                "  Note: 'mineru' Python package not detected — will be installed on first run.",
                "  提示：未检测到 'mineru' Python 包 — 首次运行时会自动安装。",
            ), "yellow"))
    if answers["parser_backend"] == "mineru-vlm":
        url = ask(
            _t("Remote VLM server URL (leave blank for local inference)",
               "远端 VLM 服务器 URL（留空走本地推理）"),
            default=defaults.get("mineru_server_url", ""),
            allow_empty=True,
        )
        answers["mineru_server_url"] = url


def _step_image_enrichment(answers: dict, defaults: dict) -> None:
    """Optional: enable VLM image enrichment (per-figure OCR + description).

    Reuses the answer-LLM credentials so the user doesn't have to enter
    them a second time. The LLM model itself must be vision-capable;
    if not, the user can change ``image_enrichment.model`` later by
    editing yaml. We default to the same model the user just picked.
    """
    print(_c(_t(
        "  Image enrichment uses a vision LLM to OCR + describe figures",
        "  图片增强会用视觉大模型对每张图做 OCR 与描述",
    ), "dim"))
    print(_c(_t(
        "  during ingestion, so the figure becomes searchable as text.",
        "  注入到 chunk 文本里，图片内容也能被检索到。",
    ), "dim"))
    print(_c(_t(
        "  Costs an extra LLM call per figure block — skip if you're",
        "  每张图多一次 LLM 调用 — 文档图很少时可以跳过。",
    ), "dim"))
    print(_c(_t(
        "  ingesting text-heavy documents.",
        "",
    ), "dim"))
    enable = ask_bool(
        _t("Enable image enrichment?", "启用图片增强？"),
        default=False,
    )
    answers["image_enrichment_enabled"] = enable
    if not enable:
        return
    answers["image_enrichment_model"] = ask(
        _t("Vision LLM model (must be vision-capable)",
           "视觉大模型 (需要支持视觉输入)"),
        default=answers.get("llm_model") or "openai/gpt-4o-mini",
    )


# ---------------------------------------------------------------------------
# The wizard
# ---------------------------------------------------------------------------


_STEPS: list[tuple[str, str, Callable[[dict, dict], None]]] = [
    ("Metadata database (PostgreSQL)", "元数据库 (PostgreSQL)",     _step_postgres),
    ("Vector database",                "向量数据库",                _step_vector),
    ("Blob storage",                   "Blob 存储",                 _step_blob),
    ("Parser backend",                 "PDF 解析器后端",            _step_parser_backend),
    ("Embedding model",                "向量嵌入模型",              _step_embedder),
    ("Answer-generation LLM",          "答案生成大模型",            _step_llm),
    ("Image enrichment (optional)",    "图片增强 (可选)",            _step_image_enrichment),
]


def _non_interactive_defaults(profile: str) -> dict[str, Any]:
    d = _profile_defaults(profile)
    if not d:
        return d
    # Fill in fields the per-step functions would normally set so
    # build_config_dict has everything it needs without prompting.
    d.setdefault("relational", "postgres")
    d.setdefault("pg_host", "localhost")
    d.setdefault("pg_port", 5432)
    d.setdefault("pg_database", "forgerag")
    d.setdefault("pg_user", "forgerag")
    d.setdefault("pg_password_env", "PG_PASSWORD")
    d.setdefault("blob_root", "./storage/blobs")
    if d.get("vector") == "chromadb":
        d.setdefault("chroma_dir", "./storage/chroma")
    d.setdefault("embedder_api_key", "")
    d.setdefault("llm_api_key", "")
    return d


def run_wizard(profile: str, non_interactive: bool) -> dict[str, Any]:
    """Return a dict of answers that the yaml builder consumes."""
    defaults = _profile_defaults(profile)

    if non_interactive:
        d = _non_interactive_defaults(profile)
        if not d:
            print("error: --non-interactive requires --profile dev|prod", file=sys.stderr)
            raise Aborted()
        return d

    banner(_t("ForgeRAG setup wizard", "ForgeRAG 安装向导"))
    print(_c(_t(
        "  Press Enter to accept the default in [yellow].",
        "  按回车接受 [黄色] 中的默认值。",
    ), "dim"))
    print(_c(_t(
        "  Type 'b' / 'back' / '<' to re-open the previous step.",
        "  在任何提问处输入 'b' / 'back' / '<' 可返回上一步。",
    ), "dim"))
    print(_c(_t("  Ctrl-C to abort.", "  按 Ctrl-C 中止。"), "dim"))

    answers: dict[str, Any] = {}
    i = 0
    while i < len(_STEPS):
        title_en, title_zh, fn = _STEPS[i]
        section(f"{i + 1}/{len(_STEPS)}  {_t(title_en, title_zh)}")
        try:
            fn(answers, defaults)
        except _GoBack:
            if i == 0:
                print(_c(_t(
                    "  already at the first step — nowhere to go back.",
                    "  已经在第一步了 — 无法再返回。",
                ), "yellow"))
                continue
            i -= 1
            continue
        i += 1

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

    # --- retrieval: thread the same LLM creds into the always-on
    #     subsystems (query_understanding, rerank, kg_extraction, kg_path)
    #     so they don't fail-loud on the first query for a missing key. ---
    def _llm_creds_block() -> dict[str, Any]:
        block: dict[str, Any] = {"model": a["llm_model"]}
        if a.get("llm_api_key"):
            block["api_key"] = a["llm_api_key"]
        elif a.get("llm_api_key_env"):
            block["api_key_env"] = a["llm_api_key_env"]
        if a.get("llm_api_base"):
            block["api_base"] = a["llm_api_base"]
        return block

    cfg["retrieval"] = {
        "query_understanding": _llm_creds_block(),
        "rerank":              _llm_creds_block(),
        "kg_extraction":       _llm_creds_block(),
        "kg_path":             _llm_creds_block(),
    }

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
            print(_c(_t(
                "  config validation FAILED — fix the file and re-run",
                "  配置校验失败 — 请修正后重新运行",
            ), "magenta"))
            return
    except FileNotFoundError:
        pass

    print()
    choice = ask_choice(
        _t("What do you want to do next?", "接下来要做什么？"),
        [
            ("nothing", _t("just exit; run it yourself later",
                           "什么都不做；稍后自己启动")),
            ("batch",   _t("batch-ingest files from a directory now",
                           "立刻批量导入指定目录的文件")),
            ("api",     _t("start the HTTP API (uvicorn) now",
                           "立刻启动 HTTP API (uvicorn)")),
        ],
        default="nothing",
    )
    if choice == "nothing":
        print()
        print(_c(_t(
            "  done. to use this config later:",
            "  完成。下次使用此配置：",
        ), "dim"))
        print(f"    export FORGERAG_CONFIG={config_path}")
        return

    if choice == "batch":
        target = ask(
            _t("Directory to ingest", "要导入的目录"),
            default="./papers",
            validator=lambda p: None if Path(p).exists()
                else _t(f"not found: {p}", f"目录不存在: {p}"),
        )
        embed = ask_bool(_t("Compute embeddings?", "同时计算向量嵌入？"),
                         default=False)
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

Walks through five small steps and writes a forgerag.yaml that wires
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
        help="Preset defaults. dev=chromadb+local; prod=pgvector+local.",
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

    # Pick the wizard's display language before any other output. Skipped
    # entirely in non-interactive mode (CI / Docker) where English is the
    # safest default for log greppability.
    global _LANG
    if not args.non_interactive:
        _LANG = _select_language()

    if args.output.exists() and not args.force:
        print(_c(_t(
            f"  {args.output} already exists. Use --force to overwrite.",
            f"  {args.output} 已存在。使用 --force 强制覆盖。",
        ), "magenta"))
        return 2

    try:
        answers = run_wizard(args.profile, args.non_interactive)
    except Aborted:
        print(_t("\n  aborted.", "\n  已中止。"))
        return 130

    cfg_dict = build_config_dict(answers)
    try:
        write_yaml(cfg_dict, args.output)
    except Exception as e:
        print(_c(_t(
            f"  failed to write {args.output}: {e}",
            f"  写入 {args.output} 失败：{e}",
        ), "magenta"))
        return 1

    print()
    print(_c(_t(f"  wrote {args.output}", f"  已写入 {args.output}"), "green"))
    print()

    try:
        post_setup(args.output)
    except Aborted:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
