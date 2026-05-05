"""
rerank tool — cross-encoder rerank over an agent-supplied chunk
candidate set.

What's pinned:

  * Reranker missing → DispatchError (deployment didn't configure
    one; the agent should pick a different strategy, not silently
    no-op).

  * Empty chunk_ids → empty result (no error). The agent might
    legitimately end up with zero candidates after filtering.

  * Scope filter applied BEFORE handing candidates to the reranker.
    A chunk_id from another user's folder doesn't get rescored —
    silently dropped at the dispatch layer. Defence in depth: even
    if the LLM somehow had a cross-user chunk_id, rerank can't
    re-admit it.

  * Reranker output ORDER is preserved as the primary signal.
    Synthetic 0-1 score derived from rank position so the agent
    has a comparable value (cross-encoder absolute scores aren't
    comparable across providers).

  * Citation pool updated with 'rerank' source — chunk_ids that
    pass through rerank are eligible for citation; their full
    content is in the pool even though only a snippet went back to
    the LLM.

  * Reranker exception → DispatchError (agent reads it and recovers).
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
    ChunkRow,
    Document,
    File,
    Folder,
    ParsedBlock,
)
from persistence.store import Store

# ---------------------------------------------------------------------------
# Stub reranker — duck-types the production Reranker protocol.
# ---------------------------------------------------------------------------


class _ReverseReranker:
    """Reverses the input order — gives a deterministic non-trivial
    rerank so we can assert the tool actually applied the new order."""

    def __init__(self, *, raise_on_call: bool = False):
        self._raise = raise_on_call
        self.calls = 0
        self.last_query: str | None = None

    def rerank(self, query, candidates, *, top_k):
        self.calls += 1
        self.last_query = query
        if self._raise:
            raise RuntimeError("reranker down")
        return list(reversed(candidates))[:top_k]

    def probe(self):
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "agentrr.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict:
    """alice has /research with 3 chunks; bob has /scratch with 1.
    Tests can ask for a mix of chunk_ids to verify scope dropout."""
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

        for fid, who in (
            ("file_research", ids["alice"]),
            ("file_scratch", ids["bob"]),
        ):
            sess.add(
                File(
                    file_id=fid,
                    content_hash=fid,
                    storage_key=f"{fid}.pdf",
                    original_name=f"{fid}.pdf",
                    display_name=f"{fid}.pdf",
                    size_bytes=1,
                    mime_type="application/pdf",
                    user_id=who,
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
        sess.flush()

        # 3 alice chunks, 1 bob chunk.
        for cid, did, content in (
            ("d_research:1:c1", "d_research", "alpha content"),
            ("d_research:1:c2", "d_research", "beta content"),
            ("d_research:1:c3", "d_research", "gamma content"),
            ("d_scratch:1:c1", "d_scratch", "scratch content"),
        ):
            block_id = cid.replace("c", "1:")  # quick unique block id
            sess.add(
                ParsedBlock(
                    block_id=block_id,
                    doc_id=did,
                    parse_version=1,
                    page_no=1,
                    seq=int(cid.rsplit("c", 1)[-1]),
                    bbox_x0=0.0,
                    bbox_y0=0.0,
                    bbox_x1=100.0,
                    bbox_y1=20.0,
                    type="paragraph",
                    text=content,
                    image_storage_key=None,
                )
            )
            sess.add(
                ChunkRow(
                    chunk_id=cid,
                    doc_id=did,
                    parse_version=1,
                    node_id=f"node-{did}",
                    block_ids=[block_id],
                    content=content,
                    content_type="text",
                    page_start=1,
                    page_end=1,
                    token_count=2,
                    path=(
                        "/research/r.pdf" if did == "d_research" else "/scratch/s.pdf"
                    ),
                )
            )
        sess.commit()
    return {"users": ids}


def _state(store: Store, *, with_reranker: bool = True, raise_on_call: bool = False):
    return SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=True)),
        authz=AuthorizationService(store),
        reranker=_ReverseReranker(raise_on_call=raise_on_call) if with_reranker else None,
    )


def _principal(seeded, name, role="user"):
    return AuthenticatedPrincipal(
        user_id=seeded["users"][name],
        username=name,
        role=role,
        via="session",
    )


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


class TestRerankPlumbing:
    def test_reranker_missing(self, store, seeded):
        state = _state(store, with_reranker=False)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch(
            "rerank",
            {"query": "x", "chunk_ids": ["d_research:1:c1"]},
            ctx,
        )
        assert "error" in out
        assert "reranker" in out["error"]

    def test_empty_chunk_ids_empty_result(self, store, seeded):
        state = _state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("rerank", {"query": "x", "chunk_ids": []}, ctx)
        assert out == {"chunks": []}

    def test_missing_required_param(self, store, seeded):
        state = _state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("rerank", {"query": "x"}, ctx)
        assert "error" in out

    def test_reranker_raises(self, store, seeded):
        state = _state(store, raise_on_call=True)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch(
            "rerank",
            {"query": "x", "chunk_ids": ["d_research:1:c1"]},
            ctx,
        )
        assert "error" in out


# ---------------------------------------------------------------------------
# Reranker order + scoring
# ---------------------------------------------------------------------------


class TestRerankOrder:
    def test_reverses_input_order(self, store, seeded):
        """ReverseReranker reverses the input list — proves the tool
        applies the rerank's order, not just passes through."""
        state = _state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch(
            "rerank",
            {
                "query": "any",
                "chunk_ids": [
                    "d_research:1:c1",
                    "d_research:1:c2",
                    "d_research:1:c3",
                ],
            },
            ctx,
        )
        chunk_ids = [c["chunk_id"] for c in out["chunks"]]
        assert chunk_ids == [
            "d_research:1:c3",
            "d_research:1:c2",
            "d_research:1:c1",
        ]

    def test_synthetic_score_descends(self, store, seeded):
        """Position 0 → 1.0; subsequent positions decrease. Strict
        descent so the citation pool sort by score is meaningful."""
        state = _state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch(
            "rerank",
            {
                "query": "any",
                "chunk_ids": [
                    "d_research:1:c1",
                    "d_research:1:c2",
                    "d_research:1:c3",
                ],
            },
            ctx,
        )
        scores = [c["score"] for c in out["chunks"]]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] == 1.0
        # Last score is non-zero (1 - 2/3 ≈ 0.3333).
        assert scores[-1] > 0

    def test_top_k_caps_output(self, store, seeded):
        state = _state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch(
            "rerank",
            {
                "query": "any",
                "chunk_ids": [
                    "d_research:1:c1",
                    "d_research:1:c2",
                    "d_research:1:c3",
                ],
                "top_k": 2,
            },
            ctx,
        )
        assert len(out["chunks"]) == 2


# ---------------------------------------------------------------------------
# Scope discipline
# ---------------------------------------------------------------------------


class TestRerankScope:
    def test_cross_user_chunks_dropped_before_rerank(self, store, seeded):
        """alice asks to rerank ALL four chunk_ids including bob's
        d_scratch:1:c1. The bob chunk must never reach the reranker
        — defence in depth, even if the LLM somehow had its id."""
        state = _state(store)
        rr = state.reranker
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch(
            "rerank",
            {
                "query": "any",
                "chunk_ids": [
                    "d_research:1:c1",
                    "d_scratch:1:c1",  # bob's — must drop
                    "d_research:1:c2",
                ],
            },
            ctx,
        )
        chunk_ids = {c["chunk_id"] for c in out["chunks"]}
        assert "d_scratch:1:c1" not in chunk_ids
        # Reranker was called once, with only alice's 2 chunks.
        assert rr.calls == 1


# ---------------------------------------------------------------------------
# Citation pool integration
# ---------------------------------------------------------------------------


class TestRerankCitationPool:
    def test_reranked_chunks_seed_pool_with_full_content(self, store, seeded):
        state = _state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        dispatch(
            "rerank",
            {
                "query": "any",
                "chunk_ids": ["d_research:1:c1", "d_research:1:c2"],
            },
            ctx,
        )
        # Pool should now hold both — full content + 'rerank' source tag.
        for cid in ("d_research:1:c1", "d_research:1:c2"):
            assert cid in ctx.citation_pool
            assert "content" in ctx.citation_pool[cid]
            assert "rerank" in ctx.citation_pool[cid]["sources"]
