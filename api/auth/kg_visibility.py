"""
KG visibility filter for multi-user.

Knowledge-graph entities and relations are extracted from chunks
across the whole corpus. An entity's ``description`` is LLM-
synthesized from ALL its source chunks during extraction, and a
relation's ``description`` similarly. That means returning the raw
record to a user lets them see facts derived from sources they
have no access to — a real privacy hole in multi-user.

This module produces three visibility tiers per entity / relation,
based on how much of the record's source set the caller can read:

    full     — every source chunk's parent doc is in the user's
               accessible set. Return the record as-is.

    partial  — at least one but not all source chunks are
               accessible. Return name / type / id and a
               ``visibility`` block describing the redaction;
               redact ``description`` (set to None); filter
               ``source_doc_ids`` and ``source_chunk_ids`` down to
               the accessible subset; suppress relations whose
               source chunk is not in the accessible set.

    hidden   — no source is accessible. The record is invisible:
               omit from list endpoints; 404 on direct fetch.

Admin role bypasses the filter — admins always see ``full``.

Why redact ``description`` rather than truncate / regenerate per-
user: regenerating costs an LLM call per request; truncating yields
incoherent prose that may still leak claims derived from
inaccessible sources. Showing nothing keeps the contract honest.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select

from persistence.models import Document

from .authz import AuthorizationService

VisibilityLevel = str  # "full" | "partial" | "hidden"


@dataclass
class Visibility:
    """Per-record visibility decision returned alongside the
    redacted entity / relation. The frontend renders a banner /
    tooltip from these counts."""

    level: VisibilityLevel  # "full" | "partial"
    accessible_sources: int
    total_sources: int
    hidden_relations: int = 0

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "accessible_sources": self.accessible_sources,
            "total_sources": self.total_sources,
            "hidden_relations": self.hidden_relations,
        }


@dataclass
class AccessibleSet:
    """Pre-computed access scope for the caller, reused across every
    entity / relation in a single request. Built once at the route
    boundary by ``build_accessible_set``."""

    is_admin: bool
    # When admin: empty (filter is a passthrough). When not: the
    # full set of doc_ids the user can read.
    doc_ids: set[str] = field(default_factory=set)

    def is_doc_accessible(self, doc_id: str | None) -> bool:
        if self.is_admin:
            return True
        if not doc_id:
            return False
        return doc_id in self.doc_ids

    def is_chunk_accessible(self, chunk_id: str | None) -> bool:
        if self.is_admin:
            return True
        if not chunk_id:
            return False
        return _doc_id_of_chunk(chunk_id) in self.doc_ids


def _doc_id_of_chunk(chunk_id: str) -> str:
    """Recover doc_id from a ``{doc_id}:{parse_version}:c{seq}`` id.
    Mirrors ``retrieval.file_search.UnifiedSearcher._doc_id_of_chunk``;
    duplicated here to keep the auth layer free of retrieval-package
    imports."""
    parts = chunk_id.rsplit(":", 2)
    return parts[0] if len(parts) == 3 else chunk_id


def build_accessible_set(
    state, user_id: str, *, is_admin: bool, auth_enabled: bool
) -> AccessibleSet:
    """Compute the user's accessible doc_id set once per request.

    The KG filter calls ``is_doc_accessible`` per entity / relation
    source — pulling the set up front turns N folder-membership
    checks into one bulk query.

    Auth-disabled deployments and admin role return an "all-access"
    flag that short-circuits the per-record check.
    """
    if not auth_enabled or is_admin:
        return AccessibleSet(is_admin=True)

    # Walk the user's accessible folders, then collect doc_ids
    # under any of those paths.
    authz: AuthorizationService = state.authz
    paths = authz.resolve_paths(user_id, None)  # user's spanning set
    if not paths:
        return AccessibleSet(is_admin=False, doc_ids=set())

    with state.store.transaction() as sess:
        # Normalise prefixes so root-y paths don't trip us up.
        # ``resolve_paths`` already minimises (e.g. drops /a/b when
        # /a is in the set), so the OR is small.
        clauses = []
        for p in paths:
            p = p.rstrip("/")
            if not p or p == "/":
                # No filter — but ``resolve_paths`` would have
                # collapsed the list to ``[]`` already if the user
                # had a "/" grant. Defensive default: treat as no
                # restriction at all.
                rows = sess.execute(select(Document.doc_id)).scalars().all()
                return AccessibleSet(is_admin=False, doc_ids=set(rows))
            clauses.append((Document.path == p) | (Document.path.like(p + "/%")))
        # Build OR of clauses. SQLAlchemy supports any() via reduce.
        from sqlalchemy import or_

        rows = sess.execute(
            select(Document.doc_id).where(or_(*clauses))
        ).scalars().all()
    return AccessibleSet(is_admin=False, doc_ids=set(rows))


# ---------------------------------------------------------------------------
# Per-record filtering
# ---------------------------------------------------------------------------


def filter_entity(
    entity: dict,
    *,
    accessible: AccessibleSet,
    relation_chunk_ids: Iterable[str] | None = None,
) -> tuple[dict | None, Visibility | None]:
    """Apply the visibility tier to a single entity record.

    Returns ``(filtered_dict, visibility)``:
      * ``(None, None)`` when the entity is hidden.
      * ``(entity_with_redactions, visibility)`` when partial.
      * ``(entity_unchanged, None)`` when full (no banner needed).

    ``relation_chunk_ids`` is the optional set of source_chunk_ids
    the caller already collected from the entity's relations; if
    provided, ``hidden_relations`` is computed by counting those
    not in the accessible set.
    """
    src_doc_ids = list(entity.get("source_doc_ids") or [])
    if not src_doc_ids:
        # An entity with no sources is malformed; hide it for non-
        # admin callers. Admins still see it (might want to clean up).
        if accessible.is_admin:
            return entity, None
        return None, None

    accessible_docs = [
        d for d in src_doc_ids if accessible.is_doc_accessible(d)
    ]
    if not accessible_docs:
        return None, None

    src_chunk_ids = list(entity.get("source_chunk_ids") or [])
    accessible_chunks = [
        c for c in src_chunk_ids if accessible.is_chunk_accessible(c)
    ]

    if len(accessible_docs) == len(src_doc_ids):
        return entity, None  # full visibility

    redacted = dict(entity)
    redacted["description"] = None
    redacted["source_doc_ids"] = accessible_docs
    redacted["source_chunk_ids"] = accessible_chunks
    hidden_relations = 0
    if relation_chunk_ids is not None:
        hidden_relations = sum(
            1 for c in relation_chunk_ids
            if not accessible.is_chunk_accessible(c)
        )
    vis = Visibility(
        level="partial",
        accessible_sources=len(accessible_docs),
        total_sources=len(src_doc_ids),
        hidden_relations=hidden_relations,
    )
    return redacted, vis


def filter_relation(
    relation: dict, *, accessible: AccessibleSet
) -> dict | None:
    """Apply visibility to a relation. Relations are simpler than
    entities — their description is also LLM-synthesized so the
    same redaction rule applies. Returns ``None`` when the relation
    has zero accessible source chunks (caller treats as hidden);
    otherwise returns the relation, possibly with redacted
    description + filtered source lists.
    """
    src_chunk_ids = list(relation.get("source_chunk_ids") or [])
    if not src_chunk_ids:
        # Relation with no source chunks is malformed; hide for
        # non-admin.
        if accessible.is_admin:
            return relation
        return None

    accessible_chunks = [
        c for c in src_chunk_ids if accessible.is_chunk_accessible(c)
    ]
    if not accessible_chunks:
        return None

    src_doc_ids = list(relation.get("source_doc_ids") or [])
    accessible_docs = [
        d for d in src_doc_ids if accessible.is_doc_accessible(d)
    ]

    if (
        len(accessible_chunks) == len(src_chunk_ids)
        and len(accessible_docs) == len(src_doc_ids)
    ):
        return relation  # full

    out = dict(relation)
    out["description"] = None
    out["source_doc_ids"] = accessible_docs
    out["source_chunk_ids"] = accessible_chunks
    return out
