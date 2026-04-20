"""
AuditLog — append-only record of folder/document mutations.

Phase 1: actor_id is always 'local'. The log is written regardless so
we already have history when auth is enabled. Entries go to the
`audit_log` table (see migration 20260418_folder_tree).

Recommended actions:
    folder.create    folder.rename    folder.move
    folder.trash     folder.restore   folder.purge
    document.move    document.upload  document.trash
    document.restore document.purge
    trash.empty      trash.autopurge
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLogRow

log = logging.getLogger(__name__)


class AuditLog:
    def __init__(self, store, *, actor_id: str = "local"):
        self.store = store
        self.actor_id = actor_id

    def write(
        self,
        action: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        details: dict[str, Any] | None = None,
        session: Session | None = None,
    ) -> None:
        """Persist an audit event. Safe to call from inside an active
        session (pass it explicitly) or standalone (spawns its own)."""
        row = AuditLogRow(
            actor_id=self.actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        if session is not None:
            session.add(row)
            return
        try:
            with self.store.transaction() as sess:
                sess.add(row)
        except Exception as e:
            # Audit failures should never break the user-facing operation.
            log.warning("audit write failed: %s", e)
