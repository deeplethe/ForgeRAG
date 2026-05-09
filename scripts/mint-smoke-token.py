"""One-shot — mint an API token for smoke testing.

Writes the raw bearer to ./.smoke-token (gitignored automatically
because it doesn't match a tracked path) and prints ONLY a status
line to stdout. The token value never crosses the terminal so it
doesn't end up in shell history, conversation transcripts, etc.

Usage:
    .venv/Scripts/python.exe scripts/mint-smoke-token.py

Exit codes:
    0 — token written to .smoke-token
    1 — no admin user found in auth_users
    2 — DB connection failed
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from api.auth.primitives import generate_sk, hash_prefix, hash_sk  # noqa: E402
from persistence.models import AuthToken, AuthUser, Base  # noqa: E402


DB_PATH = ROOT / "storage" / "forgerag.db"
TOKEN_FILE = ROOT / ".smoke-token"


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 2

    engine = create_engine(f"sqlite:///{DB_PATH}")
    Session = sessionmaker(bind=engine)
    sess = Session()

    try:
        admin = sess.execute(
            select(AuthUser).where(AuthUser.role == "admin").limit(1)
        ).scalar_one_or_none()
        if admin is None:
            print("No admin user in auth_users", file=sys.stderr)
            return 1

        raw = generate_sk()
        token = AuthToken(
            token_id=uuid4().hex[:32],
            user_id=admin.user_id,
            name="smoke-test (auto-mint)",
            token_hash=hash_sk(raw),
            hash_prefix=hash_prefix(raw),
            role="admin",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
        )
        sess.add(token)
        sess.commit()

        TOKEN_FILE.write_text(raw, encoding="utf-8")
        try:
            TOKEN_FILE.chmod(0o600)
        except OSError:
            pass

        print(
            f"OK — token for user_id={admin.user_id} written to "
            f"{TOKEN_FILE.relative_to(ROOT)} (4h expiry, prefix "
            f"{token.hash_prefix})"
        )
        return 0
    finally:
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
