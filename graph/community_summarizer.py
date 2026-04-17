"""
LLM-based community summary generation.

After Leiden clustering produces communities, this module generates
a concise natural-language summary for each community by feeding
member entities and their relations to an LLM. The summaries are
then embedded for semantic search at query time.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from .base import Community, GraphStore

if TYPE_CHECKING:
    from embedder.base import Embedder

log = logging.getLogger(__name__)

SUMMARY_SYSTEM = """You are an expert at summarizing knowledge graph communities.
Given a list of entities and their relationships within a community cluster,
write a concise 2-3 sentence summary describing what this community represents,
the key themes, and the most important connections.

Output ONLY the summary text, no JSON or formatting."""

SUMMARY_USER = """Community members and relationships:

{context}

Write a concise summary of this community."""


class CommunitySummarizer:
    """Generate and embed community summaries."""

    def __init__(
        self,
        *,
        model: str = "openai/gpt-4o-mini",
        api_key: str | None = None,
        api_base: str | None = None,
        embedder: Embedder,
        timeout: float = 60.0,
        max_workers: int = 5,
    ):
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.embedder = embedder
        self.timeout = timeout
        self.max_workers = max_workers

    def summarize_communities(
        self,
        communities: list[Community],
        graph: GraphStore,
    ) -> list[Community]:
        """Generate summaries and embeddings for each community in parallel."""
        if not communities:
            return []

        def _do(comm: Community) -> Community:
            context = self._build_context(comm, graph)
            if not context.strip():
                comm.summary = comm.title
            else:
                summary = self._call_llm(context)
                comm.summary = summary if summary else comm.title
            return comm

        # Generate summaries in parallel
        results: list[Community] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(_do, c): c for c in communities}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as e:
                    comm = futures[fut]
                    log.warning("Summary generation failed for %s: %s", comm.community_id, e)
                    comm.summary = comm.title
                    results.append(comm)

        # Batch-embed all summaries at once (more efficient than one-by-one)
        to_embed = [c for c in results if c.summary]
        if to_embed:
            try:
                embeddings = self.embedder.embed_texts([c.summary for c in to_embed])
                for c, emb in zip(to_embed, embeddings, strict=True):
                    c.summary_embedding = emb
            except Exception as e:
                log.warning("Community summary embedding failed: %s", e)

        log.info("Summarized %d communities", len(results))
        return results

    def _build_context(self, community: Community, graph: GraphStore) -> str:
        """Build a text context from community members for the LLM.

        Uses ``get_entities_by_ids`` for a single batch fetch instead of
        one ``get_entity`` per member (N→1 round-trips). Relation
        endpoints are resolved from the same batch, eliminating the
        per-relation ``get_entity(src)`` + ``get_entity(tgt)`` pairs.
        """
        capped_ids = community.entity_ids[:30]
        entity_map = graph.get_entities_by_ids(capped_ids) if capped_ids else {}

        lines: list[str] = []

        # Entities
        for eid in capped_ids:
            ent = entity_map.get(eid)
            if ent is None:
                continue
            desc = ent.description[:200] if ent.description else ""
            lines.append(f"- Entity: {ent.name} ({ent.entity_type})" + (f": {desc}" if desc else ""))

        # Relations between community members (get_relations is still
        # per-entity — batching it requires a new GraphStore method).
        # But name resolution now uses the pre-fetched entity_map.
        member_set = set(community.entity_ids)
        rel_count = 0
        for eid in community.entity_ids[:20]:
            for rel in graph.get_relations(eid):
                if rel_count >= 30:
                    break
                other = rel.target_entity if rel.source_entity == eid else rel.source_entity
                if other in member_set:
                    src_ent = entity_map.get(rel.source_entity)
                    tgt_ent = entity_map.get(rel.target_entity)
                    src_name = src_ent.name if src_ent else rel.source_entity
                    tgt_name = tgt_ent.name if tgt_ent else rel.target_entity
                    lines.append(f"- Relation: {src_name} --[{rel.keywords}]--> {tgt_name}")
                    rel_count += 1

        return "\n".join(lines)

    def _call_llm(self, context: str) -> str:
        """Call LLM to generate a community summary."""
        import litellm

        kwargs = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": SUMMARY_USER.replace("{context}", context)},
            ],
            temperature=0.0,
            max_tokens=512,
            timeout=self.timeout,
        )
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base

        try:
            resp = litellm.completion(**kwargs)
            if resp.choices and resp.choices[0].message.content:
                return resp.choices[0].message.content.strip()
            return ""
        except Exception as e:
            log.warning("Community summary LLM call failed: %s", e)
            return ""
