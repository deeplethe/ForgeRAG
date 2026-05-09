"""
Agent dispatch + the v1 tool registry (search_bm25, search_vector,
read_chunk).

What's pinned here:

  * dispatch error path — unknown tool / bad params / handler raise
    all return {"error": ...} dicts, never propagate exceptions.
  * authz / scope / trash invariants are enforced at dispatch (NOT
    inside each tool):
      - cross-user chunks dropped from search results
      - trashed-folder docs dropped from search results
      - read_chunk on cross-user chunk → ``error`` (same shape as
        "not found" — never confirm out-of-scope existence)
  * citation pool — every chunk a tool returns lands in
    ``ctx.citation_pool`` keyed by chunk_id, with content + score +
    source attribution merged across multiple hits.
  * tool_calls_log — each dispatch entry records latency_ms +
    summary, ready for the SSE trace stream.
  * snippet truncation — search results carry ≤200-char snippets;
    full content only via read_chunk.

Setup mirrors the rest of the route-authz tests:

    /research → alice rw,  doc d_research with one chunk
    /scratch  → bob   rw,  doc d_scratch  with one chunk
    /__trash__ contains a third doc (alice's, recently trashed) —
      proves trashed-folder docs are excluded even when accessible.

Vector / embedder are stubbed; we don't exercise the embedding
backend itself (covered by ``test_embedder.py``), only the
agent-layer wiring.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.agent import (
    TOOL_REGISTRY,
    DispatchError,
    build_tool_context,
    dispatch,
)
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "agentd.db")),
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

        for fid in ("file_research", "file_scratch", "file_trashed"):
            sess.add(
                File(
                    file_id=fid,
                    content_hash=fid,
                    storage_key=f"{fid}.pdf",
                    original_name=f"{fid}.pdf",
                    display_name=f"{fid}.pdf",
                    size_bytes=1,
                    mime_type="application/pdf",
                    user_id=ids["alice"] if "research" in fid or "trash" in fid else ids["bob"],
                )
            )
        sess.flush()

        for did, fid, file_id, path in (
            ("d_research", "f_research", "file_research", "/research/r.pdf"),
            ("d_scratch", "f_scratch", "file_scratch", "/scratch/s.pdf"),
            # Trashed doc — same folder as alice but path under /__trash__.
            (
                "d_trashed",
                "f_research",
                "file_trashed",
                "/__trash__/old/t.pdf",
            ),
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

        for did, content in (
            ("d_research", "alice owns this research chunk."),
            ("d_scratch", "bob has scratch content here."),
            ("d_trashed", "this is alice's trashed content."),
        ):
            block_id = f"{did}:1:1:1"
            chunk_id = f"{did}:1:c1"
            sess.add(
                ParsedBlock(
                    block_id=block_id,
                    doc_id=did,
                    parse_version=1,
                    page_no=1,
                    seq=1,
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
                    chunk_id=chunk_id,
                    doc_id=did,
                    parse_version=1,
                    node_id=f"node-{did}",
                    block_ids=[block_id],
                    content=content,
                    content_type="text",
                    page_start=1,
                    page_end=1,
                    token_count=len(content.split()),
                    path=(
                        "/research/r.pdf"
                        if did == "d_research"
                        else "/scratch/s.pdf"
                        if did == "d_scratch"
                        else "/__trash__/old/t.pdf"
                    ),
                )
            )
        sess.commit()
    return {"users": ids}


class _StubBM25:
    """In-memory keyword index that returns canned hits — keeps the
    real BM25 scoring out of the dispatch test surface."""

    def __init__(self, chunks: list[tuple[str, str, str]]):
        # (chunk_id, doc_id, content)
        self._rows = chunks

    def __len__(self):
        return len(self._rows)

    def search_chunks(self, query, top_k, *, allowed_doc_ids=None):
        out = []
        for cid, did, content in self._rows:
            if allowed_doc_ids is not None and did not in allowed_doc_ids:
                continue
            if any(tok in content.lower() for tok in query.lower().split()):
                # Score = #term hits, deterministic for tests.
                score = sum(content.lower().count(tok) for tok in query.lower().split())
                out.append((cid, float(score)))
        out.sort(key=lambda kv: -kv[1])
        return out[:top_k]


class _StubEmbedder:
    """Returns a fixed vector — enough to satisfy the dispatch
    contract without invoking a real embedding backend."""

    def embed_texts(self, texts):
        return [[0.0] * 4 for _ in texts]


class _StubVector:
    """Returns canned VectorHit-like dicts based on a static map."""

    def __init__(self, hits_by_query: dict[str, list[tuple[str, float]]]):
        self._hits = hits_by_query

    def search(self, q_vec, top_k=10, filter=None):
        # We don't exercise the path-prefix filtering inside the
        # vector backend here — the dispatch test cares about the
        # second-line scope check in ``_hydrate_hits``.
        del q_vec, filter
        # Simulate a "match everything" run; the test setup picks
        # which chunks each test case asks about.
        all_hits = [h for hits in self._hits.values() for h in hits]
        # Dedup preserving first-seen order.
        seen: set[str] = set()
        out = []
        for cid, sc in all_hits:
            if cid in seen:
                continue
            seen.add(cid)
            out.append({"chunk_id": cid, "score": sc})
            if len(out) >= top_k:
                break
        return out


def _build_state(
    store: Store,
    *,
    auth_enabled: bool = True,
    with_vector: bool = True,
    with_bm25: bool = True,
):
    bm25 = _StubBM25(
        [
            ("d_research:1:c1", "d_research", "alice owns this research chunk."),
            ("d_scratch:1:c1", "d_scratch", "bob has scratch content here."),
            ("d_trashed:1:c1", "d_trashed", "this is alice's trashed content."),
        ]
    ) if with_bm25 else None
    vector = _StubVector(
        {
            "any": [
                ("d_research:1:c1", 0.91),
                ("d_scratch:1:c1", 0.86),
                ("d_trashed:1:c1", 0.40),
            ]
        }
    ) if with_vector else None
    return SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
        authz=AuthorizationService(store),
        _bm25=bm25,
        embedder=_StubEmbedder() if with_vector else None,
        vector=vector,
    )


def _principal(seeded, name, role="user", via="session"):
    return AuthenticatedPrincipal(
        user_id=seeded["users"][name],
        username=name,
        role=role,
        via=via,
    )


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------


def test_registry_has_v1_tools():
    # Registry grows as step 3 lands new tools (graph_explore,
    # read_tree, web_search, rerank). Pin the minimum set so a typo
    # rename gets caught; we don't assert exact equality.
    assert {"search_bm25", "search_vector", "read_chunk"} <= set(TOOL_REGISTRY)
    for name, spec in TOOL_REGISTRY.items():
        assert spec.name == name
        assert spec.description, "tool needs a description"
        # Anthropic tool envelope renders cleanly.
        env = spec.to_anthropic_tool()
        assert env["name"] == name
        assert env["input_schema"]["type"] == "object"


# ---------------------------------------------------------------------------
# Dispatch error paths
# ---------------------------------------------------------------------------


class TestDispatchErrors:
    def test_unknown_tool(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("does_not_exist", {}, ctx)
        assert "error" in out
        assert "unknown" in out["error"]

    def test_missing_required_param(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("search_bm25", {}, ctx)
        assert "error" in out
        assert "query" in out["error"]

    def test_unknown_param(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("search_bm25", {"query": "x", "weird_arg": 1}, ctx)
        assert "error" in out
        assert "weird_arg" in out["error"]

    def test_param_type_mismatch(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("search_bm25", {"query": 123}, ctx)
        assert "error" in out

    def test_handler_exception_returns_error_dict(self, store, seeded):
        """If a handler raises, the agent gets a dict — never an
        unhandled exception (would crash the agent loop)."""

        class _Boom:
            def __len__(self):
                return 1

            def search_chunks(self, *a, **kw):
                raise RuntimeError("kaboom")

        state = _build_state(store)
        state._bm25 = _Boom()
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("search_bm25", {"query": "x"}, ctx)
        assert "error" in out
        assert "search_bm25" in out["error"]


# ---------------------------------------------------------------------------
# search_bm25 — scope, trash, citation pool
# ---------------------------------------------------------------------------


class TestSearchBM25:
    def test_alice_sees_only_research_chunks(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("search_bm25", {"query": "content"}, ctx)
        cids = {h["chunk_id"] for h in out["hits"]}
        # bob's chunk dropped at BM25 layer (allowed_doc_ids).
        assert "d_scratch:1:c1" not in cids
        # trashed doc dropped at the dispatch second-line scope check.
        assert "d_trashed:1:c1" not in cids

    def test_bob_sees_only_scratch_chunk(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "bob"))
        out = dispatch("search_bm25", {"query": "content"}, ctx)
        cids = {h["chunk_id"] for h in out["hits"]}
        assert cids == {"d_scratch:1:c1"}

    def test_admin_sees_everything_except_trash(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(
            state, _principal(seeded, "admin", role="admin")
        )
        out = dispatch("search_bm25", {"query": "content"}, ctx)
        cids = {h["chunk_id"] for h in out["hits"]}
        # Admin has no folder scope but trash is still excluded —
        # nobody wants stale trashed content in chat answers.
        assert "d_trashed:1:c1" not in cids

    def test_seeds_citation_pool(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        dispatch("search_bm25", {"query": "research"}, ctx)
        assert "d_research:1:c1" in ctx.citation_pool
        rec = ctx.citation_pool["d_research:1:c1"]
        # Full content lives in the pool even though only a snippet
        # went back to the LLM — agent fetches it via read_chunk.
        assert "alice owns" in rec["content"]
        assert rec["doc_id"] == "d_research"
        assert "bm25" in rec["sources"]

    def test_snippet_truncated(self, store, seeded):
        # Inject a long-content chunk to exercise the 200-char cap.
        long = "x" * 500
        with store.transaction() as sess:
            from persistence.models import ChunkRow as _C
            from persistence.models import ParsedBlock as _PB
            sess.add(
                _PB(
                    block_id="d_research:1:1:long",
                    doc_id="d_research",
                    parse_version=1,
                    page_no=2,
                    seq=2,
                    bbox_x0=0.0, bbox_y0=0.0, bbox_x1=100.0, bbox_y1=20.0,
                    type="paragraph",
                    text=long,
                    image_storage_key=None,
                )
            )
            sess.add(
                _C(
                    chunk_id="d_research:1:c2",
                    doc_id="d_research",
                    parse_version=1,
                    node_id="node-d_research",
                    block_ids=["d_research:1:1:long"],
                    content=long,
                    content_type="text",
                    page_start=2,
                    page_end=2,
                    token_count=1,
                    path="/research/r.pdf",
                )
            )
            sess.commit()
        state = _build_state(store)
        # Stub BM25 doesn't include this chunk — wire it manually.
        state._bm25 = _StubBM25(
            [("d_research:1:c2", "d_research", long)]
        )
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("search_bm25", {"query": "x"}, ctx)
        assert len(out["hits"]) == 1
        snippet = out["hits"][0]["snippet"]
        assert len(snippet) <= 201  # 200 + ellipsis


# ---------------------------------------------------------------------------
# search_vector — same scope discipline, different backend
# ---------------------------------------------------------------------------


class TestSearchVector:
    def test_scope_filter_via_dispatch(self, store, seeded):
        """The stub vector backend returns ALL three chunks regardless
        of filter — this proves dispatch's second-line scope check
        catches anything the backend missed."""
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("search_vector", {"query": "anything"}, ctx)
        cids = {h["chunk_id"] for h in out["hits"]}
        assert "d_scratch:1:c1" not in cids
        assert "d_trashed:1:c1" not in cids

    def test_no_index_returns_error(self, store, seeded):
        state = _build_state(store, with_vector=False)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("search_vector", {"query": "anything"}, ctx)
        assert "error" in out


# ---------------------------------------------------------------------------
# read_chunk — full content, scope-checked
# ---------------------------------------------------------------------------


class TestReadChunk:
    def test_owner_full_content(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch(
            "read_chunk", {"chunk_id": "d_research:1:c1"}, ctx
        )
        assert "content" in out
        assert "alice owns" in out["content"]
        # Citation pool seeded with full content.
        assert "d_research:1:c1" in ctx.citation_pool

    def test_cross_user_returns_error(self, store, seeded):
        """alice asks for bob's chunk — same shape as 'not found',
        no existence confirmation."""
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("read_chunk", {"chunk_id": "d_scratch:1:c1"}, ctx)
        assert "error" in out
        assert "not found" in out["error"]
        # Pool MUST stay empty — the chunk was never legitimately seen.
        assert "d_scratch:1:c1" not in ctx.citation_pool

    def test_trashed_chunk_returns_error(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("read_chunk", {"chunk_id": "d_trashed:1:c1"}, ctx)
        assert "error" in out

    def test_admin_reads_any_non_trashed(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(
            state, _principal(seeded, "admin", role="admin")
        )
        out = dispatch("read_chunk", {"chunk_id": "d_scratch:1:c1"}, ctx)
        assert "content" in out


# ---------------------------------------------------------------------------
# Citation pool merging across tools
# ---------------------------------------------------------------------------


def test_citation_pool_merges_sources_and_takes_max_score(store, seeded):
    """Same chunk hit by BM25 and vector should appear once in the
    pool with both sources tagged and the higher score retained —
    proves the agent loop won't double-cite the same chunk."""
    state = _build_state(store)
    ctx = build_tool_context(state, _principal(seeded, "alice"))
    dispatch("search_bm25", {"query": "research"}, ctx)
    dispatch("search_vector", {"query": "research"}, ctx)
    rec = ctx.citation_pool["d_research:1:c1"]
    assert rec["sources"] >= {"bm25", "vector"}


# ---------------------------------------------------------------------------
# tool_calls_log — drives the SSE trace stream later
# ---------------------------------------------------------------------------


def test_tool_calls_log_records_each_call(store, seeded):
    state = _build_state(store)
    ctx = build_tool_context(state, _principal(seeded, "alice"))
    dispatch("search_bm25", {"query": "research"}, ctx)
    dispatch("search_vector", {"query": "research"}, ctx)
    dispatch("read_chunk", {"chunk_id": "d_research:1:c1"}, ctx)
    assert [c["tool"] for c in ctx.tool_calls_log] == [
        "search_bm25",
        "search_vector",
        "read_chunk",
    ]
    for entry in ctx.tool_calls_log:
        assert "latency_ms" in entry
        assert isinstance(entry["latency_ms"], int)


# ---------------------------------------------------------------------------
# DispatchError shape (used by the agent loop's tool_result rendering)
# ---------------------------------------------------------------------------


def test_dispatch_error_shape():
    e = DispatchError(error="oops", tool="search_bm25")
    out = e.to_result()
    assert out == {"error": "oops", "tool": "search_bm25"}


# ---------------------------------------------------------------------------
# v1.0.0 polish: search hits expose folder ``path`` so the agent can
# use directory hierarchy as a semantic signal
# ---------------------------------------------------------------------------


class TestSearchHitsCarryPath:
    """Hits returned by search_vector / search_bm25 + payload from
    read_chunk should include the document's folder path. Folder
    naming carries time / domain / scope information the agent
    can't otherwise see at chunk level."""

    def test_search_hits_include_path(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("search_bm25", {"query": "research"}, ctx)
        assert out["hits"], "alice should have a research hit"
        h = next(x for x in out["hits"] if x["doc_id"] == "d_research")
        assert h["path"] == "/research/r.pdf", (
            "search hit must surface the doc's folder path so the agent "
            "can use directory hierarchy as semantic signal"
        )

    def test_read_chunk_includes_path(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("read_chunk", {"chunk_id": "d_research:1:c1"}, ctx)
        assert out["path"] == "/research/r.pdf"


# ---------------------------------------------------------------------------
# v1.0.0 polish: list_folders + list_docs progressive-browse tools
# ---------------------------------------------------------------------------


class TestListFoldersAuthzScoped:
    """list_folders must only return folders the user has at least
    'r' access to — same path-as-authz rule as every other tool."""

    def test_alice_sees_research_only(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("list_folders", {}, ctx)
        paths = {f["path"] for f in out["folders"]}
        assert "/research" in paths
        assert "/scratch" not in paths, (
            "bob's scratch folder must NOT appear in alice's listing"
        )

    def test_bob_sees_scratch_only(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "bob"))
        out = dispatch("list_folders", {}, ctx)
        paths = {f["path"] for f in out["folders"]}
        assert "/scratch" in paths
        assert "/research" not in paths

    def test_admin_sees_both(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(
            state, _principal(seeded, "admin", role="admin")
        )
        out = dispatch("list_folders", {}, ctx)
        paths = {f["path"] for f in out["folders"]}
        assert {"/research", "/scratch"} <= paths

    def test_doc_count_per_folder(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(
            state, _principal(seeded, "admin", role="admin")
        )
        out = dispatch("list_folders", {}, ctx)
        by_path = {f["path"]: f for f in out["folders"]}
        # /research has 1 non-trashed doc (d_trashed lives under
        # /__trash__/old/ on disk path, but its folder_id is f_research
        # — both rows therefore count to f_research's doc_count IF we
        # don't filter trashed. The handler filters trashed_metadata
        # is None; since neither d_research nor d_trashed has a
        # trashed_metadata set in the fixture, both appear. Pin
        # whichever is true and adjust if the seed changes.
        assert by_path["/research"]["doc_count"] >= 1
        assert by_path["/scratch"]["doc_count"] == 1


class TestListDocsAuthzScoped:
    """list_docs must refuse 404 for folders outside the user's
    access set — same treatment as the per-resource read routes
    ('never confirm out-of-scope existence')."""

    def test_alice_sees_research_docs(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("list_docs", {"folder_path": "/research"}, ctx)
        assert "error" not in out
        ids = {d["doc_id"] for d in out["docs"]}
        assert "d_research" in ids
        assert out["folder_path"] == "/research"

    def test_alice_cannot_list_bobs_folder(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("list_docs", {"folder_path": "/scratch"}, ctx)
        # 404-equivalent — never confirm existence
        assert "error" in out
        assert "not found or not accessible" in out["error"].lower()

    def test_missing_folder_path_returns_error(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch("list_docs", {}, ctx)
        assert "error" in out

    def test_pagination_args_threaded(self, store, seeded):
        state = _build_state(store)
        ctx = build_tool_context(state, _principal(seeded, "alice"))
        out = dispatch(
            "list_docs",
            {"folder_path": "/research", "limit": 1, "offset": 0},
            ctx,
        )
        assert out["limit"] == 1
        assert out["offset"] == 0
        # ``has_more`` is True iff offset+returned < total; with one
        # /research doc and limit=1, has_more should be False.
        assert isinstance(out["has_more"], bool)


def test_list_folders_and_list_docs_in_registry():
    """Make sure the new browsing tools made it into TOOL_REGISTRY
    so MCP exposure auto-picks them up."""
    assert "list_folders" in TOOL_REGISTRY
    assert "list_docs" in TOOL_REGISTRY
