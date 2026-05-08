"""
PathScopeResolver — resolves an API-level ``path_filters`` (list) into
the representations every retriever needs.

Inputs come from the request-level ``filter`` dict on two reserved keys:

  ``_path_filters``  (list[str], new) — caller's resolved scope. The
                                        AuthorizationService normalises
                                        request bodies to this shape;
                                        admins may legally pass
                                        anything.

  ``_path_filter``   (str, legacy)    — single-prefix alias kept for
                                        old callers / tests. Treated
                                        as ``[_path_filter]``.

Outputs:

  ``path_prefixes``       — flat list of prefixes the metadata-aware
                            backends (pgvector, Chroma, Neo4j) OR
                            together natively. Empty list means
                            "no user-visible scope — match anything
                            except trash".

  ``allowed_doc_ids``     — snapshot whitelist for path-unaware Python
                            backends (in-memory BM25, tree nav). The
                            UNION of doc_ids under any prefix. ``None``
                            when no scope is set (saves a query).

  ``or_fallback_prefixes`` — extra prefixes to OR-match for stale
                             denormalised paths during a pending
                             folder rename. Per-prefix lookups are
                             unioned + deduped.

  ``trashed_doc_ids``      — set of doc_ids under ``/__trash__/...``.
                             Always populated; downstream uses it as
                             a final post-filter so a path-prefix
                             query that accidentally lands inside
                             trash drops the hits.

The resolver normalises duplicates and the ``/`` special case ("/"
in path_prefixes collapses to "no scope" because root absorbs every
descendant).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..telemetry import get_tracer

_tracer = get_tracer()


@dataclass
class PathScope:
    path_prefixes: list[str] = field(default_factory=list)
    allowed_doc_ids: set[str] | None = None
    or_fallback_prefixes: list[str] = field(default_factory=list)
    trashed_doc_ids: set[str] = field(default_factory=set)


def _normalise_prefixes(raw: list[str] | str | None) -> list[str]:
    """Coerce caller input into a clean prefix list.

    * ``None`` / empty list → ``[]`` (no scope).
    * Single string → ``[that string]`` (legacy back-compat path).
    * Any prefix equal to ``/`` collapses the whole list to ``[]``
      because root absorbs every descendant — keeping ``/`` as a
      first-class entry would force every backend to special-case
      "but only for the root prefix".
    * Trailing slashes are stripped so ``/legal`` and ``/legal/`` are
      indistinguishable.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for p in raw:
        if not p or not isinstance(p, str):
            continue
        if p == "/":
            return []
        out.append(p.rstrip("/"))
    # Dedup while preserving order — list shape matters for consumers
    # that build OR-clauses; a stable order keeps query plans stable.
    seen: dict[str, bool] = {}
    for p in out:
        seen.setdefault(p, True)
    return list(seen.keys())


class PathScopeResolver:
    """
    Stateless (aside from the injected relational store). Safe to share
    across requests.
    """

    def __init__(self, rel):
        self.rel = rel

    def run(self, filter: dict | None = None) -> PathScope:
        with _tracer.start_as_current_span("opencraig.path_scope") as span:
            from sqlalchemy import or_, select

            from persistence.folder_service import TRASH_PATH
            from persistence.models import Document
            from persistence.pending_ops import or_fallback_prefixes

            f = filter or {}
            # Prefer ``_path_filters`` (list); fall back to legacy
            # ``_path_filter`` (str). Either may be absent.
            raw = f.get("_path_filters", None)
            if raw is None:
                raw = f.get("_path_filter", None)
            path_prefixes = _normalise_prefixes(raw)

            span.set_attribute("opencraig.path_filters", path_prefixes)
            span.set_attribute("opencraig.path_filters_count", len(path_prefixes))

            with self.rel.transaction() as sess:
                allowed_doc_ids: set[str] | None = None
                if path_prefixes:
                    # UNION across all prefixes — a doc qualifies if it
                    # lives under any of them.
                    clauses = []
                    for p in path_prefixes:
                        clauses.append(Document.path == p)
                        clauses.append(Document.path.like(p + "/%"))
                    allowed_doc_ids = set(
                        sess.execute(
                            select(Document.doc_id).where(or_(*clauses))
                        ).scalars()
                    )

                # Trashed doc_ids — always excluded from all paths
                trashed = set(
                    sess.execute(
                        select(Document.doc_id).where(
                            Document.path.like(TRASH_PATH + "/%")
                        )
                    ).scalars()
                )

                # Pending-rename OR-fallbacks: union per-prefix lookups.
                or_pfx_set: set[str] = set()
                if path_prefixes:
                    for p in path_prefixes:
                        for fb in or_fallback_prefixes(sess, p) or []:
                            if fb:
                                or_pfx_set.add(fb)
                else:
                    # No scope → we still surface all pending old paths
                    # to keep the contract uniform for downstream callers.
                    for fb in or_fallback_prefixes(sess, None) or []:
                        if fb:
                            or_pfx_set.add(fb)
                or_pfx = sorted(or_pfx_set)

            if allowed_doc_ids is not None:
                allowed_doc_ids -= trashed
                span.set_attribute("opencraig.allowed_doc_count", len(allowed_doc_ids))
            if or_pfx:
                span.set_attribute("opencraig.or_fallback_prefixes", or_pfx)

            return PathScope(
                path_prefixes=path_prefixes,
                allowed_doc_ids=allowed_doc_ids,
                or_fallback_prefixes=or_pfx,
                trashed_doc_ids=trashed,
            )

    # ── Helpers for downstream components ────────────────────────────

    @staticmethod
    def drop_trashed(hits: list, trashed_doc_ids: set[str], rel=None) -> list:
        """
        Filter hits whose doc_id is in the trashed set. ``hits`` can be
        ``ScoredChunk`` or any object with ``.chunk_id`` / optional ``.doc_id``.
        If ``.doc_id`` isn't populated we look it up from ``rel`` (one
        bulk query capped at 100 ids — cheap for typical top-k sizes).
        """
        if not trashed_doc_ids or not hits:
            return hits
        need_lookup = [
            getattr(h, "chunk_id", "")
            for h in hits
            if getattr(h, "doc_id", None) is None and getattr(h, "chunk_id", None)
        ]
        doc_id_by_chunk: dict[str, str] = {}
        if need_lookup and rel is not None:
            try:
                for row in rel.get_chunks_by_ids(need_lookup[:100]):
                    doc_id_by_chunk[row["chunk_id"]] = row.get("doc_id", "")
            except Exception:
                pass
        out = []
        for h in hits:
            did = getattr(h, "doc_id", None)
            if did is None:
                did = doc_id_by_chunk.get(getattr(h, "chunk_id", ""))
            if did is not None and did in trashed_doc_ids:
                continue
            out.append(h)
        return out
