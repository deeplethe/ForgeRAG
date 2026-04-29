"""
Knowledge Graph entity/relation extraction via LLM.

Inspired by LightRAG: uses structured prompts to extract
(entity_name, entity_type, description) and
(source, target, keywords, description, weight) tuples
from document chunks.

Supports two modes:
  - **Multi-chunk batch** (default): packs several chunks into one LLM
    call to minimise API round-trips.  Typically 3-5× faster.
  - **Single-chunk**: one LLM call per chunk (fallback if batch parse fails).
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

from graph.base import Entity, Relation, entity_id_from_name

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM = """You are an expert at extracting structured knowledge from text.
Given a passage, extract all meaningful entities and relationships.

Output ONLY valid JSON with this exact structure:
{
  "entities": [
    {"name": "EntityName", "type": "TYPE", "description": "Brief description"}
  ],
  "relations": [
    {"source": "Entity1", "target": "Entity2", "keywords": "relationship keywords", "description": "How they relate", "weight": 1.0}
  ]
}

Entity types: PERSON, ORGANIZATION, LOCATION, CONCEPT, TECHNOLOGY, EVENT, DOCUMENT, PRODUCT, METHOD, OTHER.
Only extract clearly stated entities and relationships. Be precise and concise.
Weight: 1.0 for explicit statements, 0.5 for implied relationships."""

EXTRACTION_USER = """Extract entities and relationships from this text:

---
{text}
---

Return ONLY the JSON object."""


# Multi-chunk batch prompt — packs N chunks into one call
BATCH_EXTRACTION_SYSTEM = """You are an expert at extracting structured knowledge from text.
You will receive MULTIPLE text passages, each marked with a [CHUNK:id] header.
For EACH passage, extract all meaningful entities and relationships.

Output ONLY valid JSON with this exact structure — a JSON ARRAY, one object per chunk:
[
  {
    "chunk_id": "the_chunk_id",
    "entities": [
      {"name": "EntityName", "type": "TYPE", "description": "Brief description"}
    ],
    "relations": [
      {"source": "Entity1", "target": "Entity2", "keywords": "keywords", "description": "How they relate", "weight": 1.0}
    ]
  }
]

Entity types: PERSON, ORGANIZATION, LOCATION, CONCEPT, TECHNOLOGY, EVENT, DOCUMENT, PRODUCT, METHOD, OTHER.
Only extract clearly stated entities and relationships. Be precise and concise.
Weight: 1.0 for explicit statements, 0.5 for implied relationships."""

BATCH_EXTRACTION_USER = """Extract entities and relationships from each passage below:

{passages}

Return ONLY the JSON array."""


# Entity extraction from query (for retrieval)
QUERY_ENTITY_SYSTEM = """You are an expert at identifying key entities in search queries.
Given a query, extract the main entities the user is asking about.

Output ONLY valid JSON:
{
  "entities": ["entity1", "entity2"],
  "keywords": ["keyword1", "keyword2"]
}"""

QUERY_ENTITY_USER = """Extract key entities and keywords from this query:

{query}

Return ONLY the JSON object."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Max characters per batch LLM call (keeps within context window limits)
_BATCH_CHAR_LIMIT = 12000
# Max chunks per batch call
_BATCH_CHUNK_LIMIT = 8


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class KGExtractor:
    """LLM-based entity/relation extractor for knowledge graph construction."""

    def __init__(
        self,
        *,
        model: str = "openai/gpt-4o-mini",
        api_key: str | None = None,
        api_key_env: str | None = None,
        api_base: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        self.model = model
        self.api_key = api_key or _resolve_env(api_key_env)
        self.api_base = api_base
        self.timeout = timeout
        self.max_retries = max_retries
        self._fatal_error: str | None = None

    # ----- single chunk extraction -----

    def extract(
        self,
        text: str,
        doc_id: str,
        chunk_id: str,
        path: str | None = None,
    ) -> tuple[list[Entity], list[Relation]]:
        """Extract entities and relations from a single chunk of text."""
        if not text or len(text.strip()) < 20:
            return [], []
        if self._fatal_error:
            return [], []
        if len(text) > 8000:
            text = text[:8000]

        try:
            raw = self._call_llm(
                system=EXTRACTION_SYSTEM,
                user=EXTRACTION_USER.replace("{text}", text),
            )
            return self._parse_response(raw, doc_id, chunk_id, path=path)
        except Exception as e:
            log.warning("KG extraction failed for chunk %s: %s", chunk_id, e)
            return [], []

    # ----- multi-chunk batch extraction -----

    def _extract_multi(
        self,
        chunk_group: list[dict],
        doc_id: str,
    ) -> tuple[list[Entity], list[Relation]]:
        """
        Extract from multiple chunks in a SINGLE LLM call.

        Falls back to per-chunk extraction if batch parse fails.
        """
        if self._fatal_error:
            return [], []

        # Build multi-passage prompt
        passages: list[str] = []
        id_map: dict[str, str] = {}  # chunk_id → content (for fallback)
        path_map: dict[str, str | None] = {}  # chunk_id → path
        for c in chunk_group:
            cid = c["chunk_id"]
            content = c.get("content", "")
            if not content or len(content.strip()) < 20:
                continue
            if len(content) > 6000:
                content = content[:6000]
            passages.append(f"[CHUNK:{cid}]\n{content}")
            id_map[cid] = content
            path_map[cid] = c.get("path")

        if not passages:
            return [], []

        user_text = BATCH_EXTRACTION_USER.replace("{passages}", "\n\n".join(passages))

        try:
            raw = self._call_llm(
                system=BATCH_EXTRACTION_SYSTEM,
                user=user_text,
            )
            return self._parse_batch_response(raw, doc_id, set(id_map.keys()), path_map=path_map)
        except Exception as e:
            # Batch call failed — fall back to per-chunk
            log.warning("Batch KG extraction failed, falling back to per-chunk: %s", e)
            all_ents: list[Entity] = []
            all_rels: list[Relation] = []
            for cid, content in id_map.items():
                ents, rels = self.extract(content, doc_id, cid, path=path_map.get(cid))
                all_ents.extend(ents)
                all_rels.extend(rels)
            return all_ents, all_rels

    # ----- top-level batch API -----

    def extract_batch(
        self,
        chunks: list[dict],
        doc_id: str,
        *,
        max_workers: int = 5,
    ) -> tuple[list[Entity], list[Relation]]:
        """
        Extract entities/relations from all chunks, using multi-chunk
        batch prompts with parallel execution.

        Chunks are grouped into batches of up to _BATCH_CHUNK_LIMIT
        (or _BATCH_CHAR_LIMIT total characters), then each batch is
        processed as a single LLM call.  Batches run in parallel.
        """
        all_entities: dict[str, Entity] = {}
        all_relations: dict[str, Relation] = {}

        # Group chunks into batches
        groups = _make_groups(chunks)
        log.info(
            "KG extraction: %d chunks → %d batch groups (workers=%d)",
            len(chunks),
            len(groups),
            max_workers,
        )

        import time as _time

        t_start = _time.monotonic()
        done_count = 0
        total_groups = len(groups)

        def _do(group):
            return self._extract_multi(group, doc_id)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_do, g): i for i, g in enumerate(groups)}
            for fut in as_completed(futures):
                futures[fut]
                done_count += 1
                try:
                    entities, relations = fut.result()
                    for e in entities:
                        _merge_entity(all_entities, e)
                    for r in relations:
                        _merge_relation(all_relations, r)
                    log.info(
                        "KG group %d/%d done: %d entities, %d relations (%.1fs elapsed)",
                        done_count,
                        total_groups,
                        len(entities),
                        len(relations),
                        _time.monotonic() - t_start,
                    )
                except Exception as exc:
                    log.warning(
                        "KG batch group %d/%d failed (%.1fs elapsed): %s",
                        done_count,
                        total_groups,
                        _time.monotonic() - t_start,
                        exc,
                    )

        return list(all_entities.values()), list(all_relations.values())

    # ----- query entity extraction (for retrieval) -----

    def extract_query_entities(self, query: str) -> tuple[list[str], list[str]]:
        """
        Extract entity names and keywords from a search query.
        Returns (entity_names, keywords).
        """
        try:
            raw = self._call_llm(
                system=QUERY_ENTITY_SYSTEM,
                user=QUERY_ENTITY_USER.replace("{query}", query),
            )
            data = _parse_json(raw)
            entities = data.get("entities", [])
            keywords = data.get("keywords", [])
            return (
                [str(e) for e in entities if e],
                [str(k) for k in keywords if k],
            )
        except Exception as e:
            log.warning("query entity extraction failed: %s", e)
            return [], []

    # ----- LLM call -----

    def _call_llm(self, system: str, user: str) -> str:
        """Call LLM via litellm with retry on transient errors.

        Uses a hard Python-level timeout via ThreadPoolExecutor to guard
        against thinking-model APIs that keep the connection alive but
        don't respond within the configured timeout.
        """
        import time
        from concurrent.futures import ThreadPoolExecutor as _TP
        from concurrent.futures import TimeoutError as _TE

        # ``litellm`` for exception classes; ``cached_completion`` for the call.
        import litellm

        from forgerag.llm_cache import cached_completion

        # Hard timeout = 2× the litellm timeout to account for thinking models
        hard_timeout = self.timeout * 2

        kwargs = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=4096,
            timeout=self.timeout,
        )
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base

        last_err: Exception | None = None
        for attempt in range(1 + self.max_retries):
            t0 = time.monotonic()
            try:
                # Hard timeout wrapper — litellm.completion may hang on
                # thinking models even with timeout= set.
                with _TP(1) as _pool:
                    _fut = _pool.submit(cached_completion, **kwargs)
                    resp = _fut.result(timeout=hard_timeout)
                elapsed = time.monotonic() - t0
                content = resp.choices[0].message.content or ""
                # Diagnostic: log a tiny preview of the raw response
                # alongside the OK timing. Critical for debugging the
                # "LLM returned OK but parser produced 0 entities"
                # case (e.g. missing chunk_id fields, malformed JSON,
                # unexpected schema). Truncated at 200 chars + cleaned
                # of newlines so the log line stays readable.
                preview = content[:200].replace("\n", " ").replace("\r", " ")
                log.info(
                    "KG LLM call OK (%.1fs) attempt=%d chars=%d preview=%r",
                    elapsed,
                    attempt + 1,
                    len(content),
                    preview,
                )
                return content
            except _TE:
                elapsed = time.monotonic() - t0
                log.warning(
                    "KG LLM hard timeout (%.1fs > %.1fs) attempt %d/%d",
                    elapsed,
                    hard_timeout,
                    attempt + 1,
                    1 + self.max_retries,
                )
                last_err = litellm.Timeout(
                    message=f"Hard timeout after {elapsed:.0f}s",
                    model=self.model,
                    llm_provider="openai",
                )
                if attempt < self.max_retries:
                    wait = 2**attempt
                    time.sleep(wait)
            except (litellm.Timeout, litellm.APIConnectionError) as e:
                elapsed = time.monotonic() - t0
                last_err = e
                log.info(
                    "KG LLM call timeout (%.1fs) attempt %d/%d, retrying...", elapsed, attempt + 1, 1 + self.max_retries
                )
                if attempt < self.max_retries:
                    wait = 2**attempt
                    time.sleep(wait)
            except (litellm.AuthenticationError, litellm.NotFoundError) as e:
                self._fatal_error = str(e)
                log.error("KG LLM fatal error (skipping remaining chunks): %s", e)
                raise
            except litellm.RateLimitError as e:
                last_err = e
                if attempt < self.max_retries:
                    wait = 2 ** (attempt + 1)
                    log.info(
                        "KG LLM rate limited (attempt %d/%d), retrying in %ds...",
                        attempt + 1,
                        1 + self.max_retries,
                        wait,
                    )
                    time.sleep(wait)
            except Exception as e:
                elapsed = time.monotonic() - t0
                last_err = e
                log.warning(
                    "KG LLM call error (%.1fs) attempt %d/%d: %s", elapsed, attempt + 1, 1 + self.max_retries, e
                )
                if attempt < self.max_retries:
                    wait = 2**attempt
                    time.sleep(wait)
        raise last_err  # type: ignore[misc]

    # ----- parse responses -----

    def _parse_response(
        self,
        raw: str,
        doc_id: str,
        chunk_id: str,
        path: str | None = None,
    ) -> tuple[list[Entity], list[Relation]]:
        """Parse single-chunk LLM JSON response."""
        data = _parse_json(raw)
        entities = _build_entities(data, doc_id, chunk_id, path=path)
        relations = _build_relations(data, doc_id, chunk_id, path=path)
        return entities, relations

    def _parse_batch_response(
        self,
        raw: str,
        doc_id: str,
        expected_ids: set[str],
        path_map: dict[str, str | None] | None = None,
    ) -> tuple[list[Entity], list[Relation]]:
        """
        Parse multi-chunk batch LLM JSON response.

        Expects a JSON array of objects, each with a "chunk_id" field.
        Falls back to treating the entire response as a single-chunk
        response if array parsing fails.
        """
        parsed = _parse_json_array(raw)
        pmap = path_map or {}

        all_ents: list[Entity] = []
        all_rels: list[Relation] = []

        if isinstance(parsed, list):
            # Diagnostic counters: how many items did the LLM return,
            # and how many did we silently drop because the item
            # lacked a usable ``chunk_id``? When the post-extraction
            # entity count is unexpectedly 0, this telemetry tells us
            # whether the LLM omitted the field entirely or returned
            # an empty array — without it the two failure modes look
            # identical from the outside.
            skipped_no_cid = 0
            unknown_cid = 0
            for item in parsed:
                cid = str(item.get("chunk_id", "")) if isinstance(item, dict) else ""
                if not cid:
                    skipped_no_cid += 1
                    continue
                if cid not in expected_ids:
                    # cid is non-empty but doesn't match any chunk we
                    # actually sent — the LLM likely hallucinated /
                    # mangled the id. Counting these separately keeps
                    # them from getting silently merged into the wrong
                    # provenance later if we decide to be lenient.
                    unknown_cid += 1
                ents = _build_entities(item, doc_id, cid, path=pmap.get(cid))
                rels = _build_relations(item, doc_id, cid, path=pmap.get(cid))
                all_ents.extend(ents)
                all_rels.extend(rels)
            if skipped_no_cid or unknown_cid:
                # Capture the keys of the first item so we can spot
                # schema drift (e.g. ``id`` vs ``chunk_id`` vs
                # ``chunkId``) without dumping the whole payload.
                first_keys = list(parsed[0].keys()) if isinstance(parsed[0], dict) else "<not-a-dict>"
                log.warning(
                    "KG batch parse: items=%d dropped_missing_cid=%d unknown_cid=%d expected=%d first_item_keys=%s",
                    len(parsed),
                    skipped_no_cid,
                    unknown_cid,
                    len(expected_ids),
                    first_keys,
                )
        elif isinstance(parsed, dict):
            # LLM returned a single object instead of array — treat as one chunk
            cid = str(parsed.get("chunk_id", "")) or (next(iter(expected_ids)) if len(expected_ids) == 1 else "unknown")
            all_ents = _build_entities(parsed, doc_id, cid, path=pmap.get(cid))
            all_rels = _build_relations(parsed, doc_id, cid, path=pmap.get(cid))

        return all_ents, all_rels


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_groups(chunks: list[dict]) -> list[list[dict]]:
    """Split chunks into groups respecting size and count limits."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0

    for c in chunks:
        content = c.get("content", "")
        clen = len(content)
        if not content or len(content.strip()) < 20:
            continue
        # Skip figure/image chunks — their content is just a placeholder
        # like "[figure:doc_xxx:1:1:107]" with no extractable knowledge.
        if c.get("content_type") == "figure":
            continue
        if content.strip().startswith("[figure:"):
            continue

        # Would this chunk overflow the current batch?
        if current and (len(current) >= _BATCH_CHUNK_LIMIT or current_chars + clen > _BATCH_CHAR_LIMIT):
            groups.append(current)
            current = []
            current_chars = 0

        current.append(c)
        current_chars += clen

    if current:
        groups.append(current)

    return groups


def _build_entities(
    data: dict,
    doc_id: str,
    chunk_id: str,
    path: str | None = None,
) -> list[Entity]:
    """Build Entity objects from parsed JSON dict."""
    entities = []
    src_paths = {path} if path else set()
    for e in data.get("entities", []):
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        eid = entity_id_from_name(name)
        entities.append(
            Entity(
                entity_id=eid,
                name=name,
                entity_type=str(e.get("type", "OTHER")).upper(),
                description=str(e.get("description", "")),
                source_doc_ids={doc_id},
                source_chunk_ids={chunk_id},
                source_paths=set(src_paths),
            )
        )
    return entities


def _build_relations(
    data: dict,
    doc_id: str,
    chunk_id: str,
    path: str | None = None,
) -> list[Relation]:
    """Build Relation objects from parsed JSON dict."""
    relations = []
    src_paths = {path} if path else set()
    for r in data.get("relations", []):
        src = str(r.get("source", "")).strip()
        tgt = str(r.get("target", "")).strip()
        if not src or not tgt:
            continue
        src_id = entity_id_from_name(src)
        tgt_id = entity_id_from_name(tgt)
        try:
            weight = float(r.get("weight", 1.0))
        except (ValueError, TypeError):
            weight = 1.0
        relations.append(
            Relation(
                source_entity=src_id,
                target_entity=tgt_id,
                keywords=str(r.get("keywords", "")),
                description=str(r.get("description", "")),
                weight=weight,
                source_doc_ids={doc_id},
                source_chunk_ids={chunk_id},
                source_paths=set(src_paths),
            )
        )
    return relations


def _parse_json(raw: str) -> dict:
    """Extract JSON object from LLM response (handles markdown code blocks)."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        result = json.loads(text)
        if isinstance(result, list):
            # If we got an array, wrap in dict for single-chunk compat
            return {"entities": [], "relations": [], "_items": result}
        return result
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                return {"entities": [], "relations": []}
        return {"entities": [], "relations": []}


def _parse_json_array(raw: str) -> list | dict:
    """Extract JSON array or object from LLM response."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find array
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        # Try to find object
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        return {"entities": [], "relations": []}


def _merge_entity(store: dict[str, Entity], entity: Entity) -> None:
    """Merge entity into store: append description, union sources."""
    if entity.entity_id in store:
        existing = store[entity.entity_id]
        if entity.description and entity.description not in existing.description:
            sep = "; " if existing.description else ""
            existing.description = existing.description + sep + entity.description
        existing.source_doc_ids |= entity.source_doc_ids
        existing.source_chunk_ids |= entity.source_chunk_ids
        existing.source_paths |= entity.source_paths
    else:
        store[entity.entity_id] = entity


def _merge_relation(store: dict[str, Relation], relation: Relation) -> None:
    """Merge relation into store: add weight (clamped), union sources."""
    if relation.relation_id in store:
        existing = store[relation.relation_id]
        existing.weight = min(existing.weight + relation.weight, 10.0)
        if relation.description and relation.description not in existing.description:
            sep = "; " if existing.description else ""
            existing.description = existing.description + sep + relation.description
        existing.source_doc_ids |= relation.source_doc_ids
        existing.source_chunk_ids |= relation.source_chunk_ids
        existing.source_paths |= relation.source_paths
    else:
        store[relation.relation_id] = relation


# ---------------------------------------------------------------------------
# Description consolidation (LLM-based merge)
# ---------------------------------------------------------------------------

MERGE_DESCRIPTION_SYSTEM = """You are an expert at synthesizing information.
Given multiple description fragments about the same entity (or relationship),
produce a SINGLE comprehensive, concise description that:

1. Integrates ALL key information from every fragment
2. Removes redundancy and repetition
3. Maintains an objective, third-person perspective
4. Resolves contradictions by presenting the most specific/recent information
5. Keeps the result under 200 words

Output ONLY the consolidated description text, no JSON, no markers."""

MERGE_DESCRIPTION_USER = """Consolidate these description fragments about "{name}":

{fragments}

Write a single comprehensive description:"""


def _count_fragments(desc: str) -> int:
    """Count description fragments (heuristic: split by '; ' or newlines)."""
    if not desc:
        return 0
    parts = [p.strip() for p in desc.replace("\n", "; ").split("; ") if p.strip()]
    return len(parts)


def consolidate_descriptions(
    entities: list[Entity],
    relations: list[Relation],
    *,
    model: str = "openai/gpt-4o-mini",
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: float = 60.0,
    fragment_threshold: int = 6,
    char_threshold: int = 2000,
    max_workers: int = 5,
) -> tuple[int, int]:
    """
    LLM-consolidate fragmented entity/relation descriptions.

    Scans entities and relations for descriptions that have accumulated
    too many fragments (>= fragment_threshold) or are too long
    (>= char_threshold chars). For each, calls an LLM to synthesize
    a single concise description.

    Returns (entities_merged, relations_merged) count.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from forgerag.llm_cache import cached_completion

    # Collect items that need merging
    ent_targets = [
        e
        for e in entities
        if _count_fragments(e.description) >= fragment_threshold or len(e.description) >= char_threshold
    ]
    rel_targets = [
        r
        for r in relations
        if _count_fragments(r.description) >= fragment_threshold or len(r.description) >= char_threshold
    ]

    if not ent_targets and not rel_targets:
        return 0, 0

    log.info(
        "description consolidation: %d entities + %d relations to merge",
        len(ent_targets),
        len(rel_targets),
    )

    def _merge_one(name: str, description: str) -> str | None:
        """Call LLM to merge description fragments."""
        try:
            kwargs: dict = dict(
                model=model,
                messages=[
                    {"role": "system", "content": MERGE_DESCRIPTION_SYSTEM},
                    {
                        "role": "user",
                        "content": MERGE_DESCRIPTION_USER.format(
                            name=name,
                            fragments=description,
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=1024,
                timeout=timeout,
            )
            if api_key:
                kwargs["api_key"] = api_key
            if api_base:
                kwargs["api_base"] = api_base
            resp = cached_completion(**kwargs)
            result = (resp.choices[0].message.content or "").strip()
            return result if result else None
        except Exception as e:
            log.warning("description merge failed for %s: %s", name, e)
            return None

    # Build tasks
    tasks: list[tuple] = []  # (obj, name, description)
    for e in ent_targets:
        tasks.append((e, e.name, e.description))
    for r in rel_targets:
        # Use keywords as a human-readable label since source/target are
        # entity_ids (SHA256 hashes), not readable names.
        label = r.keywords or f"{r.source_entity[:8]}→{r.target_entity[:8]}"
        tasks.append((r, label, r.description))

    ent_merged = 0
    rel_merged = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_merge_one, name, desc): obj for obj, name, desc in tasks}
        for fut in as_completed(futures):
            obj = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                log.warning("description consolidation future failed: %s", e)
                continue
            if result:
                obj.description = result
                if isinstance(obj, Entity):
                    ent_merged += 1
                else:
                    rel_merged += 1

    log.info(
        "description consolidation done: %d/%d entities, %d/%d relations merged",
        ent_merged,
        len(ent_targets),
        rel_merged,
        len(rel_targets),
    )
    return ent_merged, rel_merged


def _resolve_env(env_var: str | None) -> str | None:
    """Resolve environment variable."""
    if not env_var:
        return None
    import os

    return os.environ.get(env_var)
