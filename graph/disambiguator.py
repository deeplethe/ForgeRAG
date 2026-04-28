"""
Entity disambiguation with a fast hashtable path + embedding fallback.

When upserting an entity, the disambiguator first tries an O(1) lookup
against a normalized-name → canonical-id hashtable. If that hits, we
skip the embedding API call entirely (huge win at scale — repeated
high-frequency terms like "soil", "honey bee", "Nature Conservancy"
all match exactly across documents).

Only if the fast path misses do we fall back to the embedding +
FAISS top-k search to catch true semantic equivalents (e.g.
"Apple Inc" vs "AAPL" — different surface forms but same entity).
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from typing import TYPE_CHECKING

from .base import Entity, GraphStore, Relation
from .faiss_index import VectorIndex

if TYPE_CHECKING:
    from embedder.base import Embedder

log = logging.getLogger(__name__)


_WS_RE = re.compile(r"\s+")
# Strip common punctuation that varies between LLM extractions of the
# same entity (commas, parens, dashes, quotes …) — surface noise that
# shouldn't prevent matching.
_PUNCT_RE = re.compile(r"[^\w\s]+", flags=re.UNICODE)


def _normalize_name(name: str) -> str:
    """Lowercase + strip + collapse whitespace + drop punctuation.

    NFKD normalization folds compatibility forms (full-width letters,
    ligatures) into their canonical ASCII-ish equivalents. Combined with
    casefold (locale-independent lower) this catches:

        "Honey Bee", "honey bee", "  Honey  Bee  ", "honey-bee", "Honey, Bee"
            → all map to "honey bee"
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).casefold().strip()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


class EntityDisambiguator:
    """Hashtable-fast-path + FAISS-fallback entity disambiguation.

    Two layers, queried in order:
      1. ``_name_to_id`` — normalized name → canonical entity_id. O(1)
         hashtable lookup; no embedding API call. Catches the long
         tail of cross-document repeats (the dominant case at scale).
      2. ``_index`` — FAISS HNSW index over name embeddings. O(log N)
         top-k search; requires one embed API call per miss. Catches
         true semantic equivalents the hashtable misses.
    """

    def __init__(
        self,
        embedder: Embedder,
        threshold: float = 0.85,
        candidate_top_k: int = 10,
    ):
        self.embedder = embedder
        self.threshold = threshold
        self._candidate_top_k = candidate_top_k
        self._lock = threading.RLock()
        self._index = VectorIndex()
        # original_entity_id → canonical_entity_id
        self._redirects: dict[str, str] = {}
        # normalized_name → canonical_entity_id (fast-path)
        self._name_to_id: dict[str, str] = {}

    def load_existing(self, entities: list[Entity]) -> None:
        """Warm the FAISS index + name hashtable from existing entities."""
        with self._lock:
            keys, vecs = [], []
            for e in entities:
                if e.name_embedding:
                    keys.append(e.entity_id)
                    vecs.append(e.name_embedding)
                # Always populate the name index — embedding is optional
                # for fast-path matching.
                norm = _normalize_name(e.name)
                if norm:
                    self._name_to_id.setdefault(norm, e.entity_id)
            if keys:
                self._index.add_batch(keys, vecs)

    def find_match_by_name(self, name: str) -> str | None:
        """Fast-path: O(1) lookup by normalized name. No embedding call."""
        norm = _normalize_name(name)
        if not norm:
            return None
        with self._lock:
            return self._name_to_id.get(norm)

    def find_match(self, entity: Entity) -> str | None:
        """Embedding + FAISS fallback. Caller has already tried the
        fast path and is paying for an embed API call to be here."""
        with self._lock:
            if self._index.size == 0:
                return None
            if not entity.name_embedding:
                return None

            hits = self._index.search(entity.name_embedding, self._candidate_top_k)
            for eid, score in hits:
                if eid == entity.entity_id:
                    continue
                if score >= self.threshold:
                    return eid
            return None

    def resolve(self, entity_id: str) -> str:
        """Follow redirect chain to canonical ID."""
        with self._lock:
            return self._redirects.get(entity_id, entity_id)

    def register(self, entity: Entity) -> None:
        """Add entity to FAISS index + name hashtable after successful upsert."""
        with self._lock:
            if entity.name_embedding:
                self._index.add(entity.entity_id, entity.name_embedding)
            norm = _normalize_name(entity.name)
            if norm:
                # First-writer-wins: don't overwrite an existing canonical
                # id under a previously registered normalized form.
                self._name_to_id.setdefault(norm, entity.entity_id)

    def add_redirect(self, from_id: str, to_id: str) -> None:
        """Record that from_id should be treated as to_id."""
        with self._lock:
            self._redirects[from_id] = to_id


class DisambiguatingGraphStore:
    """
    Wrapper that adds entity disambiguation to any GraphStore.

    Intercepts upsert_entity / upsert_relation calls to check for
    semantic duplicates. Everything else is delegated to the inner store.
    """

    def __init__(
        self,
        inner: GraphStore,
        disambiguator: EntityDisambiguator,
    ):
        self._inner = inner
        self._dis = disambiguator

    def upsert_entity(self, entity: Entity) -> None:
        # ── Fast path: try normalized-name hashtable first ──
        # This is the common case at scale — high-frequency terms like
        # "soil", "honey bee", "Nature Conservancy" appear in every
        # document. Hitting here skips the embedding API call entirely.
        match_id = self._dis.find_match_by_name(entity.name)
        if match_id and match_id != entity.entity_id:
            log.info(
                "Disambiguated entity %r (id=%s) → existing %s [name-match]",
                entity.name,
                entity.entity_id,
                match_id,
            )
            original_id = entity.entity_id
            entity.entity_id = match_id
            self._dis.add_redirect(original_id, match_id)
            # Skip embedding entirely — we already know the canonical id.
            self._inner.upsert_entity(entity)
            self._dis.register(entity)
            return

        # ── Slow path: embedding + FAISS top-k ──
        # Only reached for first-time-seen surface forms. Catches semantic
        # equivalents the hashtable can't (e.g. "Apple Inc" vs "AAPL").
        if not entity.name_embedding:
            try:
                vecs = self._dis.embedder.embed_texts([entity.name])
                entity.name_embedding = vecs[0]
            except Exception:
                log.warning("Failed to embed entity name %r", entity.name)

        match_id = self._dis.find_match(entity)
        if match_id and match_id != entity.entity_id:
            log.info(
                "Disambiguated entity %r (id=%s) → existing %s [embed-match]",
                entity.name,
                entity.entity_id,
                match_id,
            )
            original_id = entity.entity_id
            entity.entity_id = match_id
            self._dis.add_redirect(original_id, match_id)

        self._inner.upsert_entity(entity)
        self._dis.register(entity)

    def upsert_relation(self, relation: Relation) -> None:
        # Resolve any redirected entity IDs
        new_src = self._dis.resolve(relation.source_entity)
        new_tgt = self._dis.resolve(relation.target_entity)
        if new_src != relation.source_entity or new_tgt != relation.target_entity:
            relation.source_entity = new_src
            relation.target_entity = new_tgt
            relation.relation_id = f"{new_src}->{new_tgt}"
        self._inner.upsert_relation(relation)

    def __getattr__(self, name):
        """Delegate all other methods to the inner store."""
        return getattr(self._inner, name)
