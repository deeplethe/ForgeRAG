"""
Multi-user visibility for the knowledge graph (S5.3).

The leak we're closing: KG entity / relation descriptions are
LLM-synthesised across **every** source chunk during extraction.
Pre S5.3 the route returned the synthesised description as soon as
any one source was accessible — the user could read facts derived
from chunks they had no access to.

S5.3 introduces three tiers per record (``api/auth/kg_visibility.py``):

    full     — every source chunk's parent doc is in the user's
               accessible set. Return the record untouched.
    partial  — at least one but not all sources accessible. Strip
               ``description`` to ``None``, filter source ID lists
               to the accessible subset, attach a ``visibility``
               block with counts.
    hidden   — no source accessible. Drop from lists; 404 on direct
               fetch.

Admin role bypasses (always sees ``full``). Auth-disabled deploys
behave like single-user (admin everywhere).

This file covers two layers:

  * Unit tests against ``filter_entity`` / ``filter_relation`` /
    ``AccessibleSet`` — pure data, no FastAPI involved.
  * Integration tests against ``api/routes/graph.py`` with a fake
    in-memory graph store — verify each route applies the filter
    and returns the right shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.auth import (
    AccessibleSet,
    AuthenticatedPrincipal,
    AuthorizationService,
    Visibility,
    build_accessible_set,
    filter_entity,
    filter_relation,
)
from api.deps import get_state
from api.routes.graph import router as graph_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import AuthUser, Document, File, Folder
from persistence.store import Store

# ---------------------------------------------------------------------------
# Unit tests — AccessibleSet + filter_entity + filter_relation
# ---------------------------------------------------------------------------


class TestAccessibleSet:
    def test_admin_allows_everything(self):
        s = AccessibleSet(is_admin=True)
        assert s.is_doc_accessible("anything")
        # Even None / empty — admin is unconditional.
        assert s.is_doc_accessible(None) is True
        assert s.is_chunk_accessible("d1:1:c5")
        assert s.is_chunk_accessible(None) is True

    def test_user_doc_membership(self):
        s = AccessibleSet(is_admin=False, doc_ids={"a", "b"})
        assert s.is_doc_accessible("a")
        assert not s.is_doc_accessible("c")
        assert not s.is_doc_accessible(None)

    def test_user_chunk_resolves_via_doc_id(self):
        s = AccessibleSet(is_admin=False, doc_ids={"d_research"})
        # Chunk id parses out to ``d_research`` regardless of
        # parse_version / seq.
        assert s.is_chunk_accessible("d_research:1:c1")
        assert s.is_chunk_accessible("d_research:7:c99")
        # Different doc — denied.
        assert not s.is_chunk_accessible("d_scratch:1:c1")
        assert not s.is_chunk_accessible(None)


# Sample records used across filter_entity / filter_relation tests.
def _ent(**overrides) -> dict:
    base = {
        "entity_id": "e1",
        "name": "Alan Turing",
        "entity_type": "person",
        "description": "synthesized from A and B",
        "source_doc_ids": ["d_a", "d_b"],
        "source_chunk_ids": ["d_a:1:c1", "d_b:1:c1"],
    }
    base.update(overrides)
    return base


def _rel(**overrides) -> dict:
    base = {
        "relation_id": "e1->e2",
        "source_entity": "e1",
        "target_entity": "e2",
        "keywords": "knew",
        "description": "they collaborated",
        "weight": 1.0,
        "source_doc_ids": ["d_a", "d_b"],
        "source_chunk_ids": ["d_a:1:c1", "d_b:1:c1"],
    }
    base.update(overrides)
    return base


class TestFilterEntity:
    def test_admin_sees_full(self):
        accessible = AccessibleSet(is_admin=True)
        out, vis = filter_entity(_ent(), accessible=accessible)
        assert vis is None
        assert out is not None
        assert out["description"] == "synthesized from A and B"
        assert out["source_doc_ids"] == ["d_a", "d_b"]

    def test_user_with_all_sources_full(self):
        accessible = AccessibleSet(is_admin=False, doc_ids={"d_a", "d_b"})
        out, vis = filter_entity(_ent(), accessible=accessible)
        assert vis is None
        assert out is not None
        assert out["description"] == "synthesized from A and B"

    def test_user_with_partial_sources_redacts_description(self):
        """The headline contract: with partial source access the LLM-
        synthesised description is set to None — not truncated, not
        regenerated. Source ID lists are filtered to the accessible
        subset and a visibility block is attached."""
        accessible = AccessibleSet(is_admin=False, doc_ids={"d_a"})
        out, vis = filter_entity(_ent(), accessible=accessible)
        assert out is not None
        assert vis is not None
        assert vis.level == "partial"
        assert vis.accessible_sources == 1
        assert vis.total_sources == 2
        assert out["description"] is None
        assert out["source_doc_ids"] == ["d_a"]
        assert out["source_chunk_ids"] == ["d_a:1:c1"]
        # Identity / type metadata still flows — the user knows this
        # entity *exists* and what kind it is.
        assert out["entity_id"] == "e1"
        assert out["name"] == "Alan Turing"
        assert out["entity_type"] == "person"

    def test_user_with_no_sources_hidden(self):
        accessible = AccessibleSet(is_admin=False, doc_ids={"d_other"})
        out, vis = filter_entity(_ent(), accessible=accessible)
        assert out is None
        assert vis is None

    def test_no_sources_admin_keeps_user_hides(self):
        """Malformed entity (zero sources) — admin still sees it for
        cleanup; non-admin gets a flat hide."""
        ent = _ent(source_doc_ids=[], source_chunk_ids=[])
        out_a, _ = filter_entity(ent, accessible=AccessibleSet(is_admin=True))
        assert out_a is not None
        out_u, _ = filter_entity(
            ent, accessible=AccessibleSet(is_admin=False, doc_ids={"d_a"})
        )
        assert out_u is None

    def test_relation_chunk_ids_drives_hidden_relation_count(self):
        accessible = AccessibleSet(is_admin=False, doc_ids={"d_a"})
        rel_chunks = [
            "d_a:1:c1",  # accessible
            "d_b:1:c2",  # not accessible
            "d_c:1:c3",  # not accessible
        ]
        out, vis = filter_entity(
            _ent(),
            accessible=accessible,
            relation_chunk_ids=rel_chunks,
        )
        assert out is not None
        assert vis is not None
        assert vis.hidden_relations == 2

    def test_visibility_to_dict_shape(self):
        v = Visibility(
            level="partial",
            accessible_sources=1,
            total_sources=3,
            hidden_relations=2,
        )
        d = v.to_dict()
        assert d == {
            "level": "partial",
            "accessible_sources": 1,
            "total_sources": 3,
            "hidden_relations": 2,
        }


class TestFilterRelation:
    def test_admin_sees_full(self):
        out = filter_relation(_rel(), accessible=AccessibleSet(is_admin=True))
        assert out is not None
        assert out["description"] == "they collaborated"

    def test_user_with_all_sources_full(self):
        accessible = AccessibleSet(is_admin=False, doc_ids={"d_a", "d_b"})
        out = filter_relation(_rel(), accessible=accessible)
        assert out is not None
        assert out["description"] == "they collaborated"

    def test_user_with_partial_redacts_description(self):
        accessible = AccessibleSet(is_admin=False, doc_ids={"d_a"})
        out = filter_relation(_rel(), accessible=accessible)
        assert out is not None
        assert out["description"] is None
        assert out["source_chunk_ids"] == ["d_a:1:c1"]
        assert out["source_doc_ids"] == ["d_a"]
        # Endpoint ids are not redacted — the relation graph topology
        # is still useful even when its description is hidden.
        assert out["source_entity"] == "e1"
        assert out["target_entity"] == "e2"

    def test_user_with_no_sources_hidden(self):
        accessible = AccessibleSet(is_admin=False, doc_ids={"d_other"})
        out = filter_relation(_rel(), accessible=accessible)
        assert out is None

    def test_no_chunks_admin_keeps_user_hides(self):
        rel = _rel(source_chunk_ids=[], source_doc_ids=[])
        assert filter_relation(rel, accessible=AccessibleSet(is_admin=True)) is not None
        assert (
            filter_relation(
                rel, accessible=AccessibleSet(is_admin=False, doc_ids={"d_a"})
            )
            is None
        )


# ---------------------------------------------------------------------------
# build_accessible_set — needs a real Store + AuthorizationService
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "kgv.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict:
    """Two users with non-overlapping folders, two docs each.

    /research  → alice (rw)   docs: d_research_1, d_research_2
    /scratch   → bob   (rw)   docs: d_scratch_1
    """
    ids: dict[str, str] = {}
    with store.transaction() as sess:
        for username, role in (
            ("admin", "admin"),
            ("alice", "user"),
            ("bob", "user"),
            # carol has no folder grants — used to exercise the
            # "user with no accessible scope" path in
            # build_accessible_set.
            ("carol", "user"),
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

        for fid in ("file_r1", "file_r2", "file_s1"):
            sess.add(
                File(
                    file_id=fid,
                    content_hash=fid,
                    storage_key=f"{fid}.pdf",
                    original_name=f"{fid}.pdf",
                    display_name=f"{fid}.pdf",
                    size_bytes=1,
                    mime_type="application/pdf",
                    user_id=ids["alice"] if fid.startswith("file_r") else ids["bob"],
                )
            )
        sess.flush()

        for did, fid, file_id, path in (
            ("d_research_1", "f_research", "file_r1", "/research/r1.pdf"),
            ("d_research_2", "f_research", "file_r2", "/research/r2.pdf"),
            ("d_scratch_1", "f_scratch", "file_s1", "/scratch/s1.pdf"),
        ):
            sess.add(
                Document(
                    doc_id=did,
                    file_id=file_id,
                    folder_id=fid,
                    path=path,
                    filename=path.split("/")[-1],
                    format="pdf",
                    active_parse_version=1,
                )
            )
        sess.commit()
    return {"users": ids}


def _fake_state(store: Store, *, auth_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
        authz=AuthorizationService(store),
    )


class TestBuildAccessibleSet:
    def test_auth_disabled_returns_admin_set(self, store, seeded):
        st = _fake_state(store, auth_enabled=False)
        s = build_accessible_set(
            st, seeded["users"]["alice"], is_admin=False, auth_enabled=False
        )
        assert s.is_admin is True
        # is_doc_accessible short-circuits on admin
        assert s.is_doc_accessible("anything")

    def test_admin_role_returns_admin_set(self, store, seeded):
        st = _fake_state(store)
        s = build_accessible_set(
            st, seeded["users"]["admin"], is_admin=True, auth_enabled=True
        )
        assert s.is_admin is True

    def test_user_collects_docs_under_granted_folders(self, store, seeded):
        st = _fake_state(store)
        s = build_accessible_set(
            st, seeded["users"]["alice"], is_admin=False, auth_enabled=True
        )
        assert s.is_admin is False
        assert s.doc_ids == {"d_research_1", "d_research_2"}
        assert s.is_doc_accessible("d_research_1")
        assert not s.is_doc_accessible("d_scratch_1")

    def test_user_with_no_grants_empty_set(self, store, seeded):
        # carol exists but has no shared_with grant on any folder.
        st = _fake_state(store)
        s = build_accessible_set(
            st, seeded["users"]["carol"], is_admin=False, auth_enabled=True
        )
        assert s.is_admin is False
        assert s.doc_ids == set()
        assert not s.is_doc_accessible("d_research_1")
        assert not s.is_doc_accessible("d_scratch_1")


# ---------------------------------------------------------------------------
# Route integration — fake graph store with controlled visibility tiers
# ---------------------------------------------------------------------------


@dataclass
class _Ent:
    """Mimics ``graph.base.Entity`` enough for the route helpers."""

    entity_id: str
    name: str
    entity_type: str
    description: str
    source_doc_ids: set[str] = field(default_factory=set)
    source_chunk_ids: set[str] = field(default_factory=set)


@dataclass
class _Rel:
    relation_id: str
    source_entity: str
    target_entity: str
    keywords: str = ""
    description: str = ""
    weight: float = 1.0
    source_doc_ids: set[str] = field(default_factory=set)
    source_chunk_ids: set[str] = field(default_factory=set)


class _FakeGraphStore:
    """Three-entity graph spanning both folders.

    e1  — sources only in /research (visible to alice / admin only)
    e2  — sources only in /scratch  (visible to bob / admin only)
    e3  — sources in BOTH folders   (alice partial, bob partial,
                                     admin full)

    Plus relations:
      r_e1_e2 (cross-folder, source chunk in /scratch)
      r_e3_e1 (sources in BOTH)
      r_e2_e3 (source only in /scratch)
    """

    def __init__(self):
        self.entities: dict[str, _Ent] = {
            "e1": _Ent(
                entity_id="e1",
                name="OnlyResearch",
                entity_type="concept",
                description="synthesised from research only",
                source_doc_ids={"d_research_1"},
                source_chunk_ids={"d_research_1:1:c1"},
            ),
            "e2": _Ent(
                entity_id="e2",
                name="OnlyScratch",
                entity_type="concept",
                description="synthesised from scratch only",
                source_doc_ids={"d_scratch_1"},
                source_chunk_ids={"d_scratch_1:1:c1"},
            ),
            "e3": _Ent(
                entity_id="e3",
                name="Crosscut",
                entity_type="concept",
                description="synthesised from BOTH research and scratch",
                source_doc_ids={"d_research_1", "d_scratch_1"},
                source_chunk_ids={
                    "d_research_1:1:c2",
                    "d_scratch_1:1:c2",
                },
            ),
        }
        self.relations: dict[str, _Rel] = {
            "r_e1_e2": _Rel(
                relation_id="r_e1_e2",
                source_entity="e1",
                target_entity="e2",
                description="bridge",
                source_doc_ids={"d_scratch_1"},
                source_chunk_ids={"d_scratch_1:1:c5"},
            ),
            "r_e3_e1": _Rel(
                relation_id="r_e3_e1",
                source_entity="e3",
                target_entity="e1",
                description="syn from both",
                source_doc_ids={"d_research_1", "d_scratch_1"},
                source_chunk_ids={
                    "d_research_1:1:c3",
                    "d_scratch_1:1:c3",
                },
            ),
            "r_e2_e3": _Rel(
                relation_id="r_e2_e3",
                source_entity="e2",
                target_entity="e3",
                description="scratch-only edge",
                source_doc_ids={"d_scratch_1"},
                source_chunk_ids={"d_scratch_1:1:c4"},
            ),
        }

    def stats(self) -> dict:
        return {"entities": len(self.entities), "relations": len(self.relations)}

    def search_entities(self, query: str, top_k: int = 10) -> list[_Ent]:
        # Trivial: return all in deterministic order; route over-fetches
        # 3*top_k anyway, and the visibility filter does the trimming.
        return sorted(self.entities.values(), key=lambda e: e.entity_id)[:top_k]

    def get_entity(self, entity_id: str) -> _Ent | None:
        return self.entities.get(entity_id)

    def get_relations(self, entity_id: str) -> list[_Rel]:
        return [
            r
            for r in self.relations.values()
            if r.source_entity == entity_id or r.target_entity == entity_id
        ]

    def _node(self, e: _Ent) -> dict:
        # Mimic networkx_store node shape (uses ``id``, not ``entity_id``).
        return {
            "id": e.entity_id,
            "name": e.name,
            "type": e.entity_type,
            "description": e.description,
            "source_doc_ids": sorted(e.source_doc_ids),
            "source_chunk_ids": sorted(e.source_chunk_ids),
        }

    def _edge(self, r: _Rel) -> dict:
        return {
            "source": r.source_entity,
            "target": r.target_entity,
            "keywords": r.keywords,
            "weight": r.weight,
            "source_doc_ids": sorted(r.source_doc_ids),
            "source_chunk_ids": sorted(r.source_chunk_ids),
        }

    def get_subgraph(self, entity_ids: list[str]) -> dict:
        ids = set(entity_ids)
        # Include neighbours, mirroring real backends.
        for r in self.relations.values():
            if r.source_entity in ids:
                ids.add(r.target_entity)
            if r.target_entity in ids:
                ids.add(r.source_entity)
        nodes = [self._node(self.entities[i]) for i in ids if i in self.entities]
        edges = [
            self._edge(r)
            for r in self.relations.values()
            if r.source_entity in ids and r.target_entity in ids
        ]
        return {"nodes": nodes, "edges": edges}

    def get_full(self, limit: int = 500) -> dict:
        return self.get_subgraph(list(self.entities.keys())[:limit])

    def get_by_doc(self, doc_id: str) -> dict:
        matching = [
            e.entity_id
            for e in self.entities.values()
            if doc_id in e.source_doc_ids
        ]
        return self.get_subgraph(matching)

    def explore(self, *, anchors=200, halo_cap=600, doc_id=None, entity_type=None):
        ids = list(self.entities.keys())
        if doc_id is not None:
            ids = [
                i for i in ids if doc_id in self.entities[i].source_doc_ids
            ]
        return self.get_subgraph(ids[:anchors])

    def cleanup_orphans(self, valid_ids):
        return {"removed_entities": 0, "removed_relations": 0}


def _build_app(
    store: Store,
    principal: AuthenticatedPrincipal,
    *,
    auth_enabled: bool = True,
) -> tuple[FastAPI, _FakeGraphStore]:
    gs = _FakeGraphStore()
    fake_state = SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(
            auth=AuthConfig(enabled=auth_enabled),
            graph=SimpleNamespace(backend="fake"),
        ),
        authz=AuthorizationService(store),
        graph_store=gs,
    )
    app = FastAPI()
    app.include_router(graph_router)
    app.dependency_overrides[get_state] = lambda: fake_state

    @app.middleware("http")
    async def _set_principal(request: Request, call_next):
        request.state.principal = principal
        return await call_next(request)

    return app, gs


def _principal(seeded, name, role="user", via="session") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=seeded["users"][name],
        username=name,
        role=role,
        via=via,
    )


# ---------------------------------------------------------------------------
# /api/v1/graph/entities  (search)
# ---------------------------------------------------------------------------


class TestEntitiesSearch:
    def test_admin_sees_all_full(self, store, seeded):
        app, _ = _build_app(store, _principal(seeded, "admin", role="admin"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/entities?query=foo&top_k=10")
        assert r.status_code == 200
        items = r.json()["items"]
        ids = {it["entity_id"] for it in items}
        assert ids == {"e1", "e2", "e3"}
        for it in items:
            assert it["visibility"] is None
            assert it["description"]  # non-null

    def test_alice_hides_scratch_only_entities(self, store, seeded):
        app, _ = _build_app(store, _principal(seeded, "alice"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/entities?query=foo&top_k=10")
        assert r.status_code == 200
        items = {it["entity_id"]: it for it in r.json()["items"]}
        # e2 (scratch only) — fully hidden
        assert "e2" not in items
        # e1 (research only) — full
        assert items["e1"]["description"] == "synthesised from research only"
        assert items["e1"]["visibility"] is None
        # e3 (cross-cut) — partial: description redacted, sources
        # filtered, visibility populated
        e3 = items["e3"]
        assert e3["description"] is None
        assert e3["visibility"] == {
            "level": "partial",
            "accessible_sources": 1,
            "total_sources": 2,
            "hidden_relations": 0,
        }
        assert e3["source_doc_ids"] == ["d_research_1"]
        assert e3["source_chunk_ids"] == ["d_research_1:1:c2"]


# ---------------------------------------------------------------------------
# /api/v1/graph/entities/{id}  (detail)
# ---------------------------------------------------------------------------


class TestEntityDetail:
    def test_admin_full_with_all_relations(self, store, seeded):
        app, _ = _build_app(store, _principal(seeded, "admin", role="admin"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/entities/e3")
        assert r.status_code == 200
        body = r.json()
        assert body["entity"]["visibility"] is None
        assert body["entity"]["description"]
        # All three relations involving e3 plus e2's edges visible.
        rel_ids = {rel["relation_id"] for rel in body["relations"]}
        # e3 participates in r_e3_e1 and r_e2_e3.
        assert rel_ids == {"r_e3_e1", "r_e2_e3"}

    def test_alice_partial_entity_with_redacted_description(self, store, seeded):
        app, _ = _build_app(store, _principal(seeded, "alice"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/entities/e3")
        assert r.status_code == 200
        body = r.json()
        ent = body["entity"]
        assert ent["description"] is None
        assert ent["visibility"]["level"] == "partial"
        assert ent["visibility"]["accessible_sources"] == 1
        assert ent["visibility"]["total_sources"] == 2
        # r_e3_e1 has both sources; alice has only research → partial.
        # r_e2_e3 has only scratch source → hidden for alice.
        rels = {rel["relation_id"]: rel for rel in body["relations"]}
        assert "r_e2_e3" not in rels
        assert "r_e3_e1" in rels
        assert rels["r_e3_e1"]["description"] is None  # redacted on partial
        # hidden_relations should reflect r_e2_e3 dropped.
        assert ent["visibility"]["hidden_relations"] == 1

    def test_bob_404_when_entity_hidden(self, store, seeded):
        """e1 has no scratch source — bob must see 404, not 403,
        not a stub. Don't confirm e1 exists."""
        app, _ = _build_app(store, _principal(seeded, "bob"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/entities/e1")
        assert r.status_code == 404

    def test_alice_full_when_all_sources_in_research(self, store, seeded):
        app, _ = _build_app(store, _principal(seeded, "alice"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/entities/e1")
        assert r.status_code == 200
        body = r.json()
        assert body["entity"]["visibility"] is None
        assert body["entity"]["description"] == "synthesised from research only"


# ---------------------------------------------------------------------------
# /api/v1/graph/subgraph + /full + /by-doc + /explore
# ---------------------------------------------------------------------------


class TestSubgraphFiltering:
    def test_alice_subgraph_drops_scratch_node_and_incident_edges(
        self, store, seeded
    ):
        app, _ = _build_app(store, _principal(seeded, "alice"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/subgraph?entity_ids=e1,e3")
        assert r.status_code == 200
        body = r.json()
        node_ids = {n.get("id") or n.get("entity_id") for n in body["nodes"]}
        # e2 fully hidden for alice → it AND every incident edge gone.
        assert "e2" not in node_ids
        assert "e1" in node_ids
        assert "e3" in node_ids
        # e3 should be partial with description=null
        e3 = next(n for n in body["nodes"] if (n.get("id") or n.get("entity_id")) == "e3")
        assert e3["description"] is None
        assert e3["visibility"]["level"] == "partial"
        # Edges: r_e1_e2 dropped (e2 hidden), r_e2_e3 dropped (e2 hidden).
        # r_e3_e1 stays (both endpoints visible) but description redacted.
        # ``source``/``target`` shape from networkx_store-style edges.
        edge_pairs = {(e.get("source"), e.get("target")) for e in body["edges"]}
        assert ("e3", "e1") in edge_pairs
        assert ("e1", "e2") not in edge_pairs
        assert ("e2", "e3") not in edge_pairs

    def test_full_graph_admin_sees_all(self, store, seeded):
        app, _ = _build_app(store, _principal(seeded, "admin", role="admin"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/full?limit=100")
        assert r.status_code == 200
        ids = {n.get("id") or n.get("entity_id") for n in r.json()["nodes"]}
        assert ids == {"e1", "e2", "e3"}

    def test_full_graph_bob_only_sees_scratch_and_partial_crosscut(
        self, store, seeded
    ):
        app, _ = _build_app(store, _principal(seeded, "bob"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/full?limit=100")
        assert r.status_code == 200
        body = r.json()
        ids = {n.get("id") or n.get("entity_id") for n in body["nodes"]}
        assert "e1" not in ids  # research-only — hidden
        assert "e2" in ids
        assert "e3" in ids
        # r_e1_e2 dropped (e1 hidden). r_e2_e3 stays — both endpoints
        # visible to bob. r_e3_e1 dropped (e1 hidden).
        edge_pairs = {(e.get("source"), e.get("target")) for e in body["edges"]}
        assert ("e2", "e3") in edge_pairs
        assert ("e1", "e2") not in edge_pairs
        assert ("e3", "e1") not in edge_pairs


class TestByDocAccess:
    def test_bob_blocked_at_doc_gate(self, store, seeded):
        """``/by-doc`` checks doc-level access first → 404 before any
        KG work runs. (If we leaked here it would also leak which docs
        exist by responding 404 only for unknown ids.)"""
        app, _ = _build_app(store, _principal(seeded, "bob"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/by-doc/d_research_1")
        assert r.status_code == 404

    def test_alice_by_doc_returns_research_subgraph(self, store, seeded):
        app, _ = _build_app(store, _principal(seeded, "alice"))
        with TestClient(app) as c:
            r = c.get("/api/v1/graph/by-doc/d_research_1")
        assert r.status_code == 200
        ids = {n.get("id") or n.get("entity_id") for n in r.json()["nodes"]}
        # e1 + e3 are sourced from d_research_1 (e3 partially).
        assert "e1" in ids
        assert "e3" in ids
        # e2 has no /research source → still hidden in this view.
        assert "e2" not in ids


# ---------------------------------------------------------------------------
# /api/v1/graph/cleanup — admin only
# ---------------------------------------------------------------------------


class TestCleanupAdminOnly:
    def test_user_403(self, store, seeded):
        app, _ = _build_app(store, _principal(seeded, "alice"))
        with TestClient(app) as c:
            r = c.post("/api/v1/graph/cleanup")
        assert r.status_code == 403

    def test_admin_ok(self, store, seeded):
        app, _ = _build_app(store, _principal(seeded, "admin", role="admin"))
        with TestClient(app) as c:
            r = c.post("/api/v1/graph/cleanup")
        assert r.status_code == 200
        body = r.json()
        assert "removed_entities" in body


# ---------------------------------------------------------------------------
# Auth-disabled passthrough
# ---------------------------------------------------------------------------


class TestAuthDisabledPassthrough:
    def test_synthetic_local_admin_sees_full(self, store, seeded):
        principal = AuthenticatedPrincipal(
            user_id="local",
            username="local",
            role="admin",
            via="auth_disabled",
        )
        app, _ = _build_app(store, principal, auth_enabled=False)
        with TestClient(app) as c:
            # Search — full description on every entity
            r = c.get("/api/v1/graph/entities?query=foo&top_k=10")
            assert r.status_code == 200
            for it in r.json()["items"]:
                assert it["visibility"] is None
                assert it["description"]
            # Subgraph — every node + edge present
            r = c.get("/api/v1/graph/subgraph?entity_ids=e1,e2,e3")
            assert r.status_code == 200
            ids = {n.get("id") or n.get("entity_id") for n in r.json()["nodes"]}
            assert ids == {"e1", "e2", "e3"}
