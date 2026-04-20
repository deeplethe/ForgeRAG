"""
SQLAlchemy Engine factory.

Builds a connection URL from RelationalConfig and returns a
configured sync Engine. Backend-specific quirks (WAL on SQLite,
utf8mb4 on MySQL, pool sizing on Postgres) are handled here so
the Store class stays dialect-agnostic.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.url import URL

from config import RelationalConfig

log = logging.getLogger(__name__)


def _resolve_password(cfg) -> str:
    env = getattr(cfg, "password_env", None)
    if env:
        val = os.environ.get(env)
        if val is None:
            raise RuntimeError(f"password_env={env} not set")
        return val
    return getattr(cfg, "password", "") or ""


def make_engine(cfg: RelationalConfig) -> Engine:
    kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}

    if cfg.backend == "postgres":
        assert cfg.postgres is not None
        pg = cfg.postgres
        url = URL.create(
            drivername="postgresql+psycopg",
            username=pg.user,
            password=_resolve_password(pg),
            host=pg.host,
            port=pg.port,
            database=pg.database,
            query={"connect_timeout": str(pg.connect_timeout)},
        )
        kwargs.update(pool_size=pg.pool_min, max_overflow=pg.pool_max - pg.pool_min)
        engine = create_engine(url, **kwargs)

    elif cfg.backend == "sqlite":
        assert cfg.sqlite is not None
        sq = cfg.sqlite
        Path(sq.path).parent.mkdir(parents=True, exist_ok=True)
        url = URL.create(drivername="sqlite+pysqlite", database=sq.path)
        engine = create_engine(
            url,
            future=True,
            connect_args={
                "timeout": sq.timeout,
                "check_same_thread": False,
            },
        )

        # Turn on WAL + enforce foreign keys per-connection
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute(f"PRAGMA journal_mode={sq.journal_mode}")
            cur.execute(f"PRAGMA synchronous={sq.synchronous}")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    else:
        raise ValueError(f"unknown relational backend: {cfg.backend!r}")

    log.info("engine created: backend=%s", cfg.backend)
    return engine
