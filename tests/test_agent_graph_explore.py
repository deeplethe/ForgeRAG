"""
graph_explore tool — knowledge graph access with the S5.3
visibility filter applied at retrieval-strictness (drop partial
AND hidden, full only).

The four invariants pinned:

  1. Full-visibility entities (every source doc accessible) come
     through with their LLM-synthesised description.
  2. Hidden entities (no accessible source) silently dropped.
  3. Partial entities (some-but-not-all sources accessible) ALSO
     dropped — strictly stricter than the API surface, because
     LLM context can't render the "1/3 sources visible" banner
     and a name-only entry would risk leaking entity existence.
  4. Relations between accepted entities surface only when their
     own source coverage is full; partial relations get dropped.

Setup:

    /research → alice rw
    /scratch  → bob rw

    Three entities in the graph:
      e_research_only — sources only in /research      (full to alice)
      e_scratch_only  — sources only in /scratch       (hidden to alice)
      e_crosscut      — sources in BOTH               (partial to alice)

    Relations:
      r_full      : e_research_only ↔ e_crosscut       (sources only /research → full)
      r_partial   : e_research_only ↔ e_crosscut       (sources both → partial for alice)
      r_hidden    : e_scratch_only ↔ e_crosscut        (sources only /scratch → hidden)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from api.agent import build_tool_context, dispatch
from api.auth import AuthenticatedPrincipal, AuthorizationService
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import (
    AuthUser,
    Document,
    File,
    Folder,
)
from persistence.store import Store

# ---------------------------------------------------------------------------
# Fake graph store (mimics graph.base.GraphStore for the methods
# graph_explore actually calls).
# ---------------------------------------------------------------------------


@dataclass
class _Ent:
    entity_id: str
    name: str
    entity_type: str = "concept"
    description: str = ""
    source_doc_ids: set[str] = field(default_factory=set)
    source_chunk_ids: set[str] = field(default_factory=set)


@dataclass
class _Rel:
    relation_id: str
    source_entity: str
    target_entity: str
    keywords: str = ""
    description: str = ""
    source_doc_ids: set[str] = field(default_factory=set)
    source_chunk_ids: set[str] = field(default_factory=set)


class _FakeGraphStore:
    def __init__(self, entities: list[_Ent], relations: list[_Rel]):
        self.entities = {e.entity_id: e for e in entities}
        self.relations = {r.relation_id: r for r in relations}

    def search_entities(self, query: str, top_k: int = 10):
        # Trivial: return all entities with name containing the query
        # (case-insensitive). Tests pass "" or specific names.
        q = query.lower()
        out = [e for e in self.entities.values() if q in e.name.lower()]
        return out[:top_k]

    def get_entity(self, entity_id: str):
        return self.entities.get(entity_id)

    def get_relations(self, entity_id: str):
        return [
            r
            for r in self.relations.values()
            if r.source_entity == entity_id or r.target_entity == entity_id
        ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "agentge.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict:
    ids: dict[str, str] = {}
    with store.transaction() as sess:
        for username, role in (
            ("admin", "admin"),
            ("alice", "user"),
            ("bob", "user"),
        ):
            uid = f"u_{username}"
            ids[username] = uid
            sess.add(
                AuthUser(
                    user_id=uid,
                    username=username,
                    email=f"{username}@example.com",
                    password_hash="x",
                    role=role,
                    status="active",
                    is_active=True,
                )
            )
        sess.flush()

        for fid, path, sw in (
            ("f_research", "/research", [{"user_id": ids["alice"], "role": "rw"}]),
            ("f_scratch", "/scratch", [{"user_id": ids["bob"], "role": "rw"}]),
        ):
            sess.add(
                Folder(
                    folder_id=fid,
                    path=path,
                    path_lower=path,
                    parent_id="__root__",
                    name=path.lstrip("/"),
                    shared_with=sw,
                )
            )
        sess.flush()

        for fid in ("file_research", "file_scratch"):
            sess.add(
                File(
                    file_id=fid,
                    content_hash=fid,
                    storage_key=f"{fid}.pdf",
                    original_name=f"{fid}.pdf",
                    display_name=f"{fid}.pdf",
                    size_bytes=1,
                    mime_type="application/pdf",
                    user_id=ids["alice"]
                    if "research" in fid
                    else ids["bob"],
                )
            )
        sess.flush()

        for did, fid, file_id, path in (
            ("d_research", "f_research", "file_research", "/research/r.pdf"),
            ("d_scratch", "f_scratch", "file_scratch", "/scratch/s.pdf"),
        ):
            sess.add(
                Document(
                    doc_id=did,
                    file_id=file_id,
                    folder_id=fid,
                    path=path,
                    filename=path.rsplit("/", 1)[-1],
                    format="pdf",
                    active_parse_version=1,
                )
            )
        sess.commit()
    return {"users": ids}


def _graph() -> _FakeGraphStore:
    """Three entities, three relations spanning the visibility tiers."""
    entities = [
        _Ent(
            entity_id="e_research_only",
            name="ResearchOnly",
            description="Synthesised from research only.",
            source_doc_ids={"d_research"},
            source_chunk_ids={"d_research:1:c1"},
        ),
        _Ent(
            entity_id="e_scratch_only",
            name="ScratchOnly",
            description="Synthesised from scratch only.",
            source_doc_ids={"d_scratch"},
            source_chunk_ids={"d_scratch:1:c1"},
        ),
        _Ent(
            entity_id="e_crosscut",
            name="Crosscut",
            description="Synthesised from BOTH folders.",
            source_doc_ids={"d_research", "d_scratch"},
            source_chunk_ids={"d_research:1:c2", "d_scratch:1:c2"},
        ),
    ]
    relations = [
        _Rel(
            relation_id="r_full",
            source_entity="e_research_only",
            target_entity="e_crosscut",
            description="research-only relation",
            keywords="contains",
            source_doc_ids={"d_research"},
            source_chunk_ids={"d_research:1:c3"},
        ),
        _Rel(
            relation_id="r_partial",
            source_entity="e_research_only",
            target_entity="e_crosscut",
            description="cross-folder relation",
            keywords="related",
            source_doc_ids={"d_research", "d_scratch"},
            source_chunk_ids={"d_research:1:c4", "d_scratch:1:c4"},
        ),
        _Rel(
            relation_id="r_hidden",
            source_entity="e_scratch_only",
            target_entity="e_crosscut",
            description="scratch-only relation",
            keywords="other",
            source_doc_ids={"d_scratch"},
            source_chunk_ids={"d_scratch:1:c5"},
        ),
    ]
    return _FakeGraphStore(entities, relations)


def _state(store: Store, *, with_graph: bool = True):
    return SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=True)),
        authz=AuthorizationService(store),
        graph_store=_graph() if with_graph else None,
    )


def _principal(seeded, name, role="user"):
    return AuthenticatedPrincipal(
        user_id=seeded["users"][name],
        username=name,
        role=role,
        via="session",
    )


# ---------------------------------------------------------------------------
# Visibility tiers
# ---------------------------------------------------------------------------


class TestGraphExploreVisibility:
    def test_admin_sees_everything(self, store, seeded):
        ctx = build_tool_context(_state(store), _principal(seeded, "admin", role="admin"))
        out = dispatch("graph_explore", {"query": ""}, ctx)
        ent_names = {e["name"] for e in out["entities"]}
        # Admin's accessible set is universal → all three entities full.
        assert ent_names == {"ResearchOnly", "ScratchOnly", "Crosscut"}
        # All three relations should also surface.
        assert len(out["relations"]) == 3

    def test_alice_full_only_no_partial_no_hidden(self, store, seeded):
        """alice has /research but not /scratch:
          * e_research_only — full → keep
          * e_crosscut      — partial → DROP (stricter than API)
          * e_scratch_only  — hidden → DROP
        """
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch("graph_explore", {"query": ""}, ctx)
        ent_names = {e["name"] for e in out["entities"]}
        assert ent_names == {"ResearchOnly"}
        # Description is the full LLM synthesis since the entity has
        # 100% accessible sources.
        assert out["entities"][0]["description"] == "Synthesised from research only."

    def test_bob_full_only_inverse(self, store, seeded):
        ctx = build_tool_context(_state(store), _principal(seeded, "bob"))
        out = dispatch("graph_explore", {"query": ""}, ctx)
        ent_names = {e["name"] for e in out["entities"]}
        assert ent_names == {"ScratchOnly"}

    def test_alice_relations_full_only(self, store, seeded):
        """For alice:
          * r_full     — both endpoints visible? r_full's source is
                         /research only → relation FULL → keep.
                         BUT only e_research_only is in alice's
                         accepted set; e_crosscut got dropped as
                         partial. graph_explore doesn't gate
                         relations on endpoint-acceptance — it
                         resolves names via gs.get_entity for
                         endpoints not in the cache. So r_full
                         should still surface.
          * r_partial  — sources span both folders → relation
                         description=None on partial → DROP.
          * r_hidden   — source only /scratch → DROP.
        """
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch("graph_explore", {"query": ""}, ctx)
        rel_descs = {r["description"] for r in out["relations"]}
        assert rel_descs == {"research-only relation"}

    def test_bob_no_research_relations(self, store, seeded):
        """bob: r_hidden's source is /scratch only → full for bob,
        keep. r_full's source is /research → hidden for bob → drop.
        r_partial is partial → drop."""
        ctx = build_tool_context(_state(store), _principal(seeded, "bob"))
        out = dispatch("graph_explore", {"query": ""}, ctx)
        rel_descs = {r["description"] for r in out["relations"]}
        assert rel_descs == {"scratch-only relation"}


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class TestGraphExploreShape:
    def test_endpoint_names_resolved_for_dropped_entities(self, store, seeded):
        """Even when one endpoint of a relation belongs to an entity
        we DIDN'T put in the entities list (e.g. e_crosscut for alice),
        the relation's source/target must surface a human-readable
        name — not the raw entity_id — so the LLM can read it."""
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch("graph_explore", {"query": ""}, ctx)
        assert len(out["relations"]) == 1
        rel = out["relations"][0]
        assert {rel["source"], rel["target"]} == {"ResearchOnly", "Crosscut"}

    def test_source_chunk_ids_capped(self, store, seeded):
        """Hub entities can have hundreds of source chunk_ids; agent
        only needs a few to ground citations. Result truncates to
        at most 3 per record."""
        # Synthesise an entity with 10 chunks.
        gs = _graph()
        gs.entities["e_research_only"].source_chunk_ids = {
            f"d_research:1:c{i}" for i in range(10)
        }
        state = _state(store)
        state.graph_store = gs
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("graph_explore", {"query": ""}, ctx)
        ent = next(e for e in out["entities"] if e["name"] == "ResearchOnly")
        assert len(ent["source_chunk_ids"]) == 3


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestGraphExploreErrors:
    def test_no_graph_configured(self, store, seeded):
        state = _state(store, with_graph=False)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("graph_explore", {"query": "x"}, ctx)
        assert "error" in out
        assert "knowledge graph" in out["error"]

    def test_missing_query_param(self, store, seeded):
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch("graph_explore", {}, ctx)
        assert "error" in out

    def test_graph_search_raises(self, store, seeded):
        class _Boom:
            def search_entities(self, *a, **kw):
                raise RuntimeError("graph down")

        state = _state(store)
        state.graph_store = _Boom()
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("graph_explore", {"query": "x"}, ctx)
        assert "error" in out

    def test_top_k_capped(self, store, seeded):
        """Hard ceiling on top_k regardless of what the LLM asks
        for — mirrors search_bm25 / search_vector behaviour."""
        ctx = build_tool_context(_state(store), _principal(seeded, "admin", role="admin"))
        out = dispatch("graph_explore", {"query": "", "top_k": 10000}, ctx)
        # We seeded 3 entities, so the cap doesn't actually matter
        # for output count — but the dispatch must not error out.
        assert "error" not in out
