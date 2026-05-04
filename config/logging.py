"""
Logging configuration.

Provides a ``LoggingConfig`` pydantic model and a ``setup_logging()``
helper that configures the Python root logger with:

  - **Console handler** — coloured, concise format for development.
  - **File handler** — daily-rotated UTF-8 log files under ``logs/``.

Usage::

    from config.logging import LoggingConfig, setup_logging
    setup_logging(LoggingConfig(level="DEBUG"))
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class LoggingConfig(BaseModel):
    """Logging section of ``opencraig.yaml``."""

    level: Literal[
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
        "debug",
        "info",
        "warning",
        "error",
        "critical",
    ] = "INFO"

    dir: str = Field(
        default="./logs",
        description="Directory for log files. Relative paths are resolved from CWD.",
    )

    # How many daily log files to keep before auto-deletion.
    retention_days: int = Field(default=30, ge=1)

    # Whether to also log to the console (stderr).
    console: bool = True


# ── Format ────────────────────────────────────────────────────────────
_FILE_FMT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
_CONSOLE_FMT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(cfg: LoggingConfig | None = None) -> None:
    """
    Configure Python root logger.

    Safe to call multiple times — idempotent (removes previous ForgeRAG
    handlers before adding new ones).
    """
    if cfg is None:
        cfg = LoggingConfig()

    level = getattr(logging, cfg.level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any handlers we previously attached (idempotent re-init).
    for h in list(root.handlers):
        if getattr(h, "_opencraig_managed", False):
            root.removeHandler(h)
            h.close()

    # ── File handler (daily rotation) ─────────────────────────────────
    log_dir = Path(cfg.dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # TimedRotatingFileHandler with suffix creates files like:
    #   logs/opencraig.log           (current)
    #   logs/opencraig.log.2026-04-11  (yesterday)
    # We rename via namer so rotated files become:
    #   logs/2026-04-11.log
    log_file = log_dir / "opencraig.log"
    fh = TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=cfg.retention_days,
        encoding="utf-8",
        utc=False,
    )
    fh.suffix = "%Y-%m-%d"

    # Custom namer: logs/opencraig.log.2026-04-11 -> logs/2026-04-11.log
    def _namer(default_name: str) -> str:
        # default_name = ".../logs/opencraig.log.2026-04-11"
        parts = default_name.rsplit(".", 1)  # [..., "2026-04-11"]
        if len(parts) == 2:
            base_dir = os.path.dirname(parts[0])
            date_str = parts[1]
            return os.path.join(base_dir, f"{date_str}.log")
        return default_name

    fh.namer = _namer
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))
    fh._opencraig_managed = True  # type: ignore[attr-defined]
    root.addHandler(fh)

    # ── Console handler ───────────────────────────────────────────────
    if cfg.console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(level)
        ch.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_DATE_FMT))
        ch._opencraig_managed = True  # type: ignore[attr-defined]
        root.addHandler(ch)

    # ── Suppress noisy third-party loggers ────────────────────────────
    for noisy in (
        "httpcore",
        "httpx",
        "urllib3",
        "asyncio",
        "chromadb",
        "sentence_transformers",
        "uvicorn.access",
    ):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    logging.getLogger("forgerag.main").debug(
        "Logging initialised: level=%s dir=%s retention=%dd console=%s",
        cfg.level.upper(),
        log_dir.resolve(),
        cfg.retention_days,
        cfg.console,
    )
