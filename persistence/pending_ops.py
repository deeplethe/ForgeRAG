"""
Helpers for working with the ``pending_folder_ops`` deferred queue.

Two jobs:

1. **Query-time OR fallback** — when Chroma / Neo4j are still catching
   up on a big rename, a query scoped to the *new* path would miss
   hits that still carry the *old* path in their denormalised metadata.
   :func:`or_fallback_prefixes` returns the set of OLD prefixes that
   overlap a given query scope so retrieval can OR-match them.

2. **Consumer API** — :func:`claim_next_batch` / :func:`mark_done` /
   :func:`mark_failed` give the nightly maintenance script a simple
   state machine for draining the queue.

All functions take a SQLAlchemy ``Session`` and are synchronous —
the caller owns transaction boundaries.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import PendingFolderOp

# ---------------------------------------------------------------------------
# Query-time OR fallback
# ---------------------------------------------------------------------------


def or_fallback_prefixes(sess: Session, path_prefix: str | None) -> list[str]:
    """Return old-path prefixes that a query scoped to *path_prefix*
    should ALSO match in Chroma / Neo4j, because a pending rename hasn't
    yet propagated there.

    The returned list is suitable for an ``OR`` filter: a chunk whose
    denormalised metadata path starts with any entry in the list (or
    with *path_prefix* itself) should be returned.

    If *path_prefix* is ``None`` (global query) we still surface all
    pending old paths — a global query has no scope to narrow against,
    so pending lag is harmless but returning the list keeps the contract
    uniform for callers.
    """
    if not path_prefix:
        rows = sess.execute(
            select(PendingFolderOp.old_path).where(PendingFolderOp.status.in_(("pending", "running")))
        ).all()
        return [r[0] for r in rows if r[0]]

    # Match pending ops where:
    #   - new_path equals the query prefix, OR is an ancestor of it, OR
    #   - new_path lives under the query prefix (narrower rename).
    # In all three cases the matching old_path (possibly rebased) is a
    # prefix the query might need to OR-match against.
    q_pfx = path_prefix.rstrip("/") or "/"
    rows = sess.execute(
        select(PendingFolderOp.old_path, PendingFolderOp.new_path).where(
            PendingFolderOp.status.in_(("pending", "running"))
        )
    ).all()
    out: list[str] = []
    for old_path, new_path in rows:
        if not old_path or not new_path:
            continue
        n = new_path.rstrip("/") or "/"
        # (a) Exact or ancestor match: query /Legal/Contracts, op /Legal → /LegalV2
        if q_pfx == n or q_pfx.startswith(n + "/"):
            # Rebase: the old-world equivalent of q_pfx is
            #   old_path + q_pfx[len(new_path):]
            rebased = old_path + q_pfx[len(n) :]
            out.append(rebased)
        # (b) Narrower match: query /Legal, op /Legal/2024 → /LegalV2/2024
        elif n.startswith(q_pfx + "/") or q_pfx == "/":
            out.append(old_path)
    return out


# ---------------------------------------------------------------------------
# Consumer API (used by scripts/nightly_maintenance.py)
# ---------------------------------------------------------------------------


def claim_next_batch(sess: Session, *, limit: int = 10) -> list[PendingFolderOp]:
    """Atomically mark up to *limit* pending ops as ``running`` and
    return them. Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` on
    Postgres (ignored on SQLite) so two workers don't claim the same
    rows.

    Caller is expected to commit the session after this returns so
    the ``running`` state is durable before work starts.
    """
    stmt = (
        select(PendingFolderOp)
        .where(PendingFolderOp.status == "pending")
        .order_by(PendingFolderOp.queued_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    try:
        claimed = list(sess.execute(stmt).scalars())
    except Exception:
        # SKIP LOCKED unsupported (SQLite, etc.) — fall back.
        claimed = list(
            sess.execute(
                select(PendingFolderOp)
                .where(PendingFolderOp.status == "pending")
                .order_by(PendingFolderOp.queued_at)
                .limit(limit)
            ).scalars()
        )
    now = datetime.utcnow()
    for op in claimed:
        op.status = "running"
        op.started_at = now
    return claimed


def mark_done(sess: Session, op_id: str) -> None:
    sess.execute(
        update(PendingFolderOp)
        .where(PendingFolderOp.op_id == op_id)
        .values(status="done", finished_at=datetime.utcnow(), error_msg=None)
    )


def mark_failed(sess: Session, op_id: str, error: str) -> None:
    sess.execute(
        update(PendingFolderOp)
        .where(PendingFolderOp.op_id == op_id)
        .values(
            status="failed",
            finished_at=datetime.utcnow(),
            error_msg=error[:2000],
        )
    )


def purge_completed(sess: Session, *, older_than_days: int = 7) -> int:
    """Delete ``done`` rows whose ``finished_at`` is older than the cutoff.

    Called from nightly maintenance. ``failed`` rows are intentionally
    NOT purged here — they're useful for incident review; an operator
    decides when to drop them.

    Returns the number of rows deleted.
    """
    from datetime import timedelta

    from sqlalchemy import delete

    cutoff = datetime.utcnow() - timedelta(days=max(0, older_than_days))
    res = sess.execute(
        delete(PendingFolderOp).where(
            PendingFolderOp.status == "done",
            PendingFolderOp.finished_at.is_not(None),
            PendingFolderOp.finished_at < cutoff,
        )
    )
    return res.rowcount or 0
