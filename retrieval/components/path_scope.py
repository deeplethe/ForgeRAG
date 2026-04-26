"""
PathScopeResolver — resolves a user-facing ``path_filter`` string into
the two representations the retrievers need:

1. ``path_prefix``        — passed to SQL / metadata-indexed backends
                            (pgvector, Chroma, Neo4j) for native prefix
                            filtering.

2. ``allowed_doc_ids``    — snapshot whitelist for Python-side backends
                            (BM25 in-memory index, TreeNav) that don't
                            store path. Resolved once per request from
                            ``Document.path`` so all paths see a
                            consistent scope across a concurrent rename.

3. ``or_fallback_prefixes`` — during a pending folder-rename window the
                              downstream stores may still carry the old
                              path; these are extra prefixes to OR-match
                              so retrieval stays complete.

Additionally records ``trashed_doc_ids`` so the downstream post-filter
can drop any chunks that slipped into a non-scoped query.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..telemetry import get_tracer

_tracer = get_tracer()


@dataclass
class PathScope:
    path_prefix: str | None = None
    allowed_doc_ids: set[str] | None = None
    or_fallback_prefixes: list[str] = field(default_factory=list)
    trashed_doc_ids: set[str] = field(default_factory=set)


class PathScopeResolver:
    """
    Stateless (aside from the injected relational store). Safe to share
    across requests.
    """

    def __init__(self, rel):
        self.rel = rel

    def run(self, filter: dict | None = None) -> PathScope:
        with _tracer.start_as_current_span("forgerag.path_scope") as span:
            from sqlalchemy import select

            from persistence.folder_service import TRASH_PATH
            from persistence.models import Document
            from persistence.pending_ops import or_fallback_prefixes

            raw = (filter or {}).get("_path_filter") if filter else None
            path_prefix: str | None = None
            if raw and raw != "/":
                path_prefix = raw.rstrip("/")
            span.set_attribute("forgerag.path_filter", path_prefix or "")

            with self.rel.transaction() as sess:
                allowed_doc_ids: set[str] | None = None
                if path_prefix is not None:
                    allowed_doc_ids = set(
                        sess.execute(
                            select(Document.doc_id).where(
                                (Document.path == path_prefix) | (Document.path.like(path_prefix + "/%"))
                            )
                        ).scalars()
                    )

                trashed = set(
                    sess.execute(select(Document.doc_id).where(Document.path.like(TRASH_PATH + "/%"))).scalars()
                )
                or_pfx = or_fallback_prefixes(sess, path_prefix) or []

            if allowed_doc_ids is not None:
                allowed_doc_ids -= trashed
                span.set_attribute("forgerag.allowed_doc_count", len(allowed_doc_ids))
            if or_pfx:
                span.set_attribute("forgerag.or_fallback_prefixes", or_pfx)

            return PathScope(
                path_prefix=path_prefix,
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
