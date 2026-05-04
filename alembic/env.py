"""
Alembic environment — resolves the DB URL from ForgeRAG config
so migrations work against whichever backend the user has configured.
"""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make project root importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    """Resolve DB URL from ForgeRAG config, same as the app does."""
    try:
        from config.loader import load_config
        from persistence.engine import build_engine

        cfg = load_config(os.environ.get("OPENCRAIG_CONFIG") or os.environ.get("FORGERAG_CONFIG"))
        engine = build_engine(cfg.persistence.relational)
        return str(engine.url)
    except Exception:
        # Fallback: try env var (legacy FORGERAG_DATABASE_URL still accepted)
        url = os.environ.get("OPENCRAIG_DATABASE_URL") or os.environ.get("FORGERAG_DATABASE_URL")
        if url:
            return url
        raise RuntimeError("Cannot resolve database URL. Set OPENCRAIG_CONFIG or OPENCRAIG_DATABASE_URL.")


def run_migrations_offline():
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    url = _get_url()
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = url

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
