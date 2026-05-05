"""
read_tree tool — section-tree navigation, one node per call.

What's pinned:

  * Default (no node_id) returns the root node + its immediate
    children list (titles only).
  * Explicit node_id returns that node's summary + key_entities +
    immediate children.
  * Children list is title + node_id + page-range — the agent
    drills down by calling read_tree again. We deliberately don't
    dump the whole tree (could be 1000+ nodes per doc).
  * children_preview is capped — guards against monster docs with
    a 100-section root.
  * Cross-user → 404 (same shape as missing doc — no existence
    confirmation), enforced via doc_passes_scope.
  * Trashed-doc → 404 even when the underlying tree exists and the
    user has folder access — trashed content is never readable.
  * Tree never built / wrong node_id / unknown doc all surface as
    DispatchError dicts; LLM can read the error and recover.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.agent import build_tool_context, dispatch
from api.auth import AuthenticatedPrincipal, AuthorizationService
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import (
    AuthUser,
    DocTreeRow,
    Document,
    File,
    Folder,
)
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "agentrt.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


def _tree_blob() -> dict:
    """A 3-section doc tree — root with 2 children, one of which has
    a leaf grandchild. Rich enough to test navigation + summaries +
    page ranges."""
    return {
        "doc_id": "d_research",
        "parse_version": 1,
        "root_id": "root",
        "quality_score": 0.9,
        "generation_method": "test",
        "nodes": {
            "root": {
                "node_id": "root",
                "parent_id": None,
                "level": 0,
                "title": "Research Paper",
                "page_start": 1,
                "page_end": 10,
                "children": ["intro", "methods"],
                "summary": "Top-level paper summary.",
                "key_entities": ["Alan Turing"],
                "role": "main",
            },
            "intro": {
                "node_id": "intro",
                "parent_id": "root",
                "level": 1,
                "title": "Introduction",
                "page_start": 1,
                "page_end": 3,
                "children": [],
                "summary": "Intro section about computability.",
                "key_entities": [],
                "role": "main",
            },
            "methods": {
                "node_id": "methods",
                "parent_id": "root",
                "level": 1,
                "title": "Methods",
                "page_start": 4,
                "page_end": 7,
                "children": ["sampling"],
                "summary": "Methods section.",
                "key_entities": ["sampling"],
                "role": "main",
            },
            "sampling": {
                "node_id": "sampling",
                "parent_id": "methods",
                "level": 2,
                "title": "Sampling Procedure",
                "page_start": 5,
                "page_end": 6,
                "children": [],
                "summary": None,  # no summary computed
                "key_entities": [],
                "role": "main",
            },
        },
    }


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
        sess.add(
            Folder(
                folder_id="f_research",
                path="/research",
                path_lower="/research",
                parent_id="__root__",
                name="research",
                shared_with=[{"user_id": ids["alice"], "role": "rw"}],
            )
        )
        sess.flush()
        sess.add(
            File(
                file_id="file_research",
                content_hash="h",
                storage_key="r.pdf",
                original_name="r.pdf",
                display_name="r.pdf",
                size_bytes=1,
                mime_type="application/pdf",
                user_id=ids["alice"],
            )
        )
        sess.flush()
        sess.add(
            Document(
                doc_id="d_research",
                file_id="file_research",
                folder_id="f_research",
                path="/research/r.pdf",
                filename="r.pdf",
                format="pdf",
                active_parse_version=1,
            )
        )
        # An additional doc inside the trash folder — referenced by
        # alice's folder but currently sitting under /__trash__/.
        sess.add(
            Document(
                doc_id="d_trashed",
                file_id="file_research",
                folder_id="f_research",
                path="/__trash__/old/t.pdf",
                filename="t.pdf",
                format="pdf",
                active_parse_version=1,
            )
        )
        sess.flush()
        sess.add(
            DocTreeRow(
                doc_id="d_research",
                parse_version=1,
                root_id="root",
                quality_score=0.9,
                generation_method="test",
                tree_json=_tree_blob(),
            )
        )
        # Trashed doc has its own tree blob too (would be served if
        # we didn't filter — proves the trash gate works).
        trashed_tree = dict(_tree_blob())
        trashed_tree["doc_id"] = "d_trashed"
        sess.add(
            DocTreeRow(
                doc_id="d_trashed",
                parse_version=1,
                root_id="root",
                quality_score=0.9,
                generation_method="test",
                tree_json=trashed_tree,
            )
        )
        sess.commit()
    return {"users": ids}


def _state(store: Store):
    return SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=True)),
        authz=AuthorizationService(store),
    )


def _principal(seeded, name, role="user"):
    return AuthenticatedPrincipal(
        user_id=seeded["users"][name],
        username=name,
        role=role,
        via="session",
    )


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


class TestReadTreeNavigation:
    def test_root_default(self, store, seeded):
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch("read_tree", {"doc_id": "d_research"}, ctx)
        assert out["node_id"] == "root"
        assert out["is_root"] is True
        assert out["title"] == "Research Paper"
        assert out["summary"] == "Top-level paper summary."
        assert out["key_entities"] == ["Alan Turing"]
        # Children list is titles + node_ids only — no recursion.
        child_titles = [c["title"] for c in out["children"]]
        assert child_titles == ["Introduction", "Methods"]

    def test_drill_into_methods(self, store, seeded):
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch(
            "read_tree", {"doc_id": "d_research", "node_id": "methods"}, ctx
        )
        assert out["node_id"] == "methods"
        assert out["is_root"] is False
        assert out["title"] == "Methods"
        assert out["parent_id"] == "root"
        # Sampling sub-node visible as a child stub.
        assert out["children"][0]["node_id"] == "sampling"
        assert out["children"][0]["has_summary"] is False  # null upstream

    def test_leaf_no_children(self, store, seeded):
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch(
            "read_tree", {"doc_id": "d_research", "node_id": "intro"}, ctx
        )
        assert out["children"] == []

    def test_children_preview_capped(self, store, seeded):
        """Synthesise a doc with 50 children — cap kicks in at 20."""
        big_tree = _tree_blob()
        big_tree["nodes"]["root"]["children"] = [f"c{i}" for i in range(50)]
        for i in range(50):
            big_tree["nodes"][f"c{i}"] = {
                "node_id": f"c{i}",
                "parent_id": "root",
                "level": 1,
                "title": f"section {i}",
                "page_start": i,
                "page_end": i,
                "children": [],
                "summary": None,
                "key_entities": [],
                "role": "main",
            }
        with store.transaction() as sess:
            existing = sess.get(DocTreeRow, ("d_research", 1))
            existing.tree_json = big_tree
            sess.commit()
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch("read_tree", {"doc_id": "d_research"}, ctx)
        assert len(out["children"]) == 20


# ---------------------------------------------------------------------------
# Authz / scope
# ---------------------------------------------------------------------------


class TestReadTreeAuthz:
    def test_cross_user_404(self, store, seeded):
        """bob has no /research access — read_tree must return error,
        same shape as a missing doc. No existence confirmation."""
        ctx = build_tool_context(_state(store), _principal(seeded, "bob"))
        out = dispatch("read_tree", {"doc_id": "d_research"}, ctx)
        assert "error" in out
        assert "not found" in out["error"]

    def test_trashed_doc_blocked(self, store, seeded):
        """alice technically has access to the folder, but the doc
        is currently sitting inside /__trash__/. Tree is built and
        the row exists, but the trash gate blocks the read."""
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch("read_tree", {"doc_id": "d_trashed"}, ctx)
        assert "error" in out

    def test_admin_bypass(self, store, seeded):
        ctx = build_tool_context(
            _state(store), _principal(seeded, "admin", role="admin")
        )
        out = dispatch("read_tree", {"doc_id": "d_research"}, ctx)
        assert "error" not in out
        assert out["title"] == "Research Paper"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestReadTreeErrors:
    def test_missing_doc_param(self, store, seeded):
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch("read_tree", {}, ctx)
        assert "error" in out

    def test_unknown_doc(self, store, seeded):
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch("read_tree", {"doc_id": "d_unknown"}, ctx)
        assert "error" in out

    def test_unknown_node(self, store, seeded):
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch(
            "read_tree",
            {"doc_id": "d_research", "node_id": "ghost_node"},
            ctx,
        )
        assert "error" in out
        assert "node not found" in out["error"]

    def test_no_tree_built(self, store, seeded):
        """Doc exists but tree blob isn't built yet (e.g. ingestion
        still running). Surfaces as a clear error so the agent can
        try a different tool instead of looping."""
        # Wipe the tree row.
        with store.transaction() as sess:
            row = sess.get(DocTreeRow, ("d_research", 1))
            sess.delete(row)
            sess.commit()
        ctx = build_tool_context(_state(store), _principal(seeded, "alice"))
        out = dispatch("read_tree", {"doc_id": "d_research"}, ctx)
        assert "error" in out
        assert "tree" in out["error"]
