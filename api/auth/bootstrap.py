"""
First-run admin auto-provisioning.

Called once at server startup (from ``lifespan``). If ``auth.enabled=true``
and the ``auth_users`` table is empty, we create a single admin with:

  * username = "admin"
  * password = ``cfg.auth.initial_password`` (default "forgerag")
  * must_change_password = true
  * one initial API token named "bootstrap" with full admin role

Both credentials are printed once to stdout in a highlighted block so
operators running ``docker-compose up`` can grab them from the logs.
They are never re-derivable after this point.

If the table already has a user, this is a no-op (idempotent).
"""

from __future__ import annotations

import logging
import uuid

from .primitives import generate_sk, hash_password, hash_prefix, hash_sk

log = logging.getLogger(__name__)


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def bootstrap_if_empty(cfg, store) -> None:
    """
    ``cfg`` is the full ``AppConfig``; ``store`` is the connected
    ``persistence.store.Store``. We read ``cfg.auth`` only.

    Safe to call repeatedly — checks the table and returns immediately
    when an admin already exists.
    """
    if not cfg.auth.enabled:
        return

    from sqlalchemy import func, select
    from sqlalchemy.exc import IntegrityError

    from persistence.models import AuthToken, AuthUser

    try:
        with store.transaction() as sess:
            existing = sess.execute(select(func.count()).select_from(AuthUser)).scalar() or 0
            if existing > 0:
                return

            # Create admin user
            user_id = _new_id()
            admin = AuthUser(
                user_id=user_id,
                username="admin",
                password_hash=hash_password(cfg.auth.initial_password),
                must_change_password=True,
                role="admin",
                is_active=True,
                status="active",
            )
            sess.add(admin)
            # Flush so the user row exists before we insert the token that
            # FK-references it (SQLAlchemy's unit-of-work ordering isn't
            # guaranteed across independent objects in the same commit).
            sess.flush()

            # Mint one initial token
            raw = generate_sk()
            token = AuthToken(
                token_id=_new_id(),
                user_id=user_id,
                name="bootstrap",
                token_hash=hash_sk(raw),
                hash_prefix=hash_prefix(raw),
                role="admin",
            )
            sess.add(token)

            # Take ownership of __root__ so subsequent uploads /
            # subfolder creation under the existing single-user UX
            # land with a real owner_user_id. __trash__ stays
            # ownerless (admins manage it via role bypass).
            from persistence.models import Folder

            root = sess.get(Folder, "__root__")
            if root is not None and root.owner_user_id is None:
                root.owner_user_id = user_id
    except IntegrityError:
        # Another worker won the bootstrap race — the username UNIQUE
        # constraint kicked in. Idempotent: just return so startup
        # continues normally instead of crashing the second worker.
        log.info("auth bootstrap: another worker already created the admin user; skipping")
        return

    # One-time stdout banner — use print, not logger, so it's visible
    # even when log level hides INFO, and the format survives JSON
    # log formatters.
    banner = [
        "",
        "=" * 78,
        "  FIRST-RUN ADMIN CREATED — save these, they won't appear again",
        "-" * 78,
        "  Username:   admin",
        f"  Password:   {cfg.auth.initial_password}    (change required on first web login)",
        f"  API Token:  {raw}    (use for CLI / SDK bearer)",
        "-" * 78,
        "  Web:  POST /api/v1/auth/login  with username/password",
        "  CLI:  export OPENCRAIG_API_TOKEN=" + raw,
        "=" * 78,
        "",
    ]
    print("\n".join(banner), flush=True)
    log.info(
        "auth bootstrapped: admin user + initial token created "
        "(hash_prefix=%s); password-change required on first login",
        hash_prefix(raw),
    )
