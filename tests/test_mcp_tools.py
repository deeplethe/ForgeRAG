"""
Tests for the MCP-typed wrappers around the existing TOOL_REGISTRY
handlers (Wave 2.3).

Verifies:
    * each domain tool is registered with FastMCP under the
      expected name and schema shape
    * the shared ``_dispatch_via_mcp`` helper:
        - reads the per-request principal from the ContextVar and
          refuses unauthenticated calls cleanly (no raise)
        - refuses when AppState isn't bound yet (server startup
          racing the first MCP request)
        - calls ``api.agent.dispatch.dispatch`` with the right
          (tool_name, params, ctx) on the happy path
        - generates a forward-compat call_id for lineage and emits
          the log line that Wave 3.5 will swap for a DB write
        - strips leading-underscore keys before returning

The handlers themselves are unit-tested elsewhere (test_agent_*,
test_artifact_*, etc.); here we only exercise the MCP wrap.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")

from api.routes import mcp_tools as _mcp_tools_module
from api.routes.mcp_server import (
    mcp_server,
    reset_mcp_principal,
    set_mcp_principal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _principal(user_id: str = "u_alice"):
    return SimpleNamespace(
        user_id=user_id,
        username=user_id.removeprefix("u_"),
        role="user",
        via="bearer",
    )


@pytest.fixture
def fake_dispatch(monkeypatch):
    """Replace the dispatch import in mcp_tools with a recorder.
    Each test sets ``recorder.return_value`` (or callable) to control
    what dispatch returns; ``recorder.calls`` accumulates every call."""

    class _Recorder:
        def __init__(self):
            self.calls: list[dict] = []
            self.return_value: dict = {"ok": True}

        def __call__(self, tool_name, params, ctx):
            self.calls.append(
                {"tool_name": tool_name, "params": params, "ctx": ctx}
            )
            if callable(self.return_value):
                return self.return_value(tool_name, params, ctx)
            return self.return_value

    rec = _Recorder()
    monkeypatch.setattr(_mcp_tools_module, "_dispatch", rec)
    return rec


@pytest.fixture
def fake_state(monkeypatch):
    """Install a fake AppState getter so ``_dispatch_via_mcp`` finds
    one. Also stubs ``build_tool_context`` to a no-op that returns a
    placeholder ctx, since constructing the real one needs all of
    AppState wired up."""

    state = SimpleNamespace(name="fake-app-state")
    _mcp_tools_module._set_app_state_getter(lambda: state)

    def _fake_build(state_arg, principal_arg, *, project_id=None):
        return SimpleNamespace(
            state=state_arg,
            principal=principal_arg,
            project_id=project_id,
            tool_calls_log=[],
        )

    monkeypatch.setattr(_mcp_tools_module, "build_tool_context", _fake_build)

    yield state

    # Reset the getter so other tests don't see a stale one
    _mcp_tools_module._set_app_state_getter(lambda: None)


@pytest.fixture
def with_principal():
    """Set + tear down the MCP principal ContextVar around each test."""
    tokens: list = []

    def _set(p):
        tokens.append(set_mcp_principal(p))

    yield _set

    for tok in reversed(tokens):
        reset_mcp_principal(tok)


# ---------------------------------------------------------------------------
# Tool registration shape
# ---------------------------------------------------------------------------


def test_mcp_tool_catalog_is_exact():
    """Make sure importing ``mcp_tools`` results in exactly the
    expected catalog being published — locks in the v0.6.0 surface
    (no web_search; list_folders + list_docs added for progressive
    corpus browsing)."""
    tools = asyncio.run(mcp_server.list_tools())
    names = {t.name for t in tools}
    expected = {
        "ping",  # diagnostic from mcp_server.py
        "search_vector",
        "read_chunk",
        "read_tree",
        "list_folders",
        "list_docs",
        "graph_explore",
        "rerank",
        "import_from_library",
    }
    assert names == expected, f"unexpected MCP tool catalog: {names ^ expected}"


def test_web_search_is_NOT_exposed_via_mcp():
    """the SDK ships its own WebFetch / WebSearch built-in tools; we
    deliberately don't double-expose ours via MCP. Locking this in
    so a future re-add doesn't sneak through."""
    tools = asyncio.run(mcp_server.list_tools())
    names = {t.name for t in tools}
    assert "web_search" not in names


def test_search_vector_input_schema():
    tools = asyncio.run(mcp_server.list_tools())
    spec = next(t for t in tools if t.name == "search_vector")
    schema = spec.inputSchema
    # Required only the query string; top_k has a default
    assert "query" in schema["properties"]
    assert schema["required"] == ["query"]


def test_rerank_takes_chunk_ids_array():
    tools = asyncio.run(mcp_server.list_tools())
    spec = next(t for t in tools if t.name == "rerank")
    schema = spec.inputSchema
    assert "chunk_ids" in schema["properties"]
    assert schema["properties"]["chunk_ids"]["type"] == "array"


def test_import_from_library_required_doc_id():
    tools = asyncio.run(mcp_server.list_tools())
    spec = next(t for t in tools if t.name == "import_from_library")
    assert "doc_id" in spec.inputSchema["properties"]
    assert "doc_id" in spec.inputSchema["required"]


# ---------------------------------------------------------------------------
# _dispatch_via_mcp guards
# ---------------------------------------------------------------------------


def test_dispatch_unauthenticated_returns_error_dict():
    """No principal in ContextVar → wrapper returns ``error`` dict
    instead of raising. The agent should be able to read this."""
    # Ensure context is clean
    asyncio.run(asyncio.sleep(0))  # yields to clear any stale state
    result = _mcp_tools_module._dispatch_via_mcp("search_vector", {"query": "x"})
    assert "error" in result
    assert "no authenticated principal" in result["error"].lower()
    assert result["tool"] == "search_vector"


def test_dispatch_no_app_state_returns_error_dict(with_principal):
    with_principal(_principal())
    # Default getter returns None
    _mcp_tools_module._set_app_state_getter(lambda: None)
    result = _mcp_tools_module._dispatch_via_mcp("search_vector", {"query": "x"})
    assert "error" in result
    assert "not initialised" in result["error"]


def test_dispatch_happy_path_routes_to_real_dispatch(
    with_principal, fake_state, fake_dispatch
):
    fake_dispatch.return_value = {"hits": [{"chunk_id": "c1"}], "count": 1}
    with_principal(_principal())

    result = _mcp_tools_module._dispatch_via_mcp(
        "search_vector", {"query": "blue", "top_k": 5}
    )
    assert result == {"hits": [{"chunk_id": "c1"}], "count": 1}

    assert len(fake_dispatch.calls) == 1
    call = fake_dispatch.calls[0]
    assert call["tool_name"] == "search_vector"
    assert call["params"] == {"query": "blue", "top_k": 5}
    # ctx was built with the right principal + state
    assert call["ctx"].state is fake_state
    assert call["ctx"].principal.user_id == "u_alice"


def test_dispatch_strips_underscore_keys(with_principal, fake_state, fake_dispatch):
    """Result keys starting with ``_`` are SSE-trace channels, not for
    the agent. The wrap drops them before returning."""
    fake_dispatch.return_value = {
        "hits": ["x"],
        "_internal_telemetry": {"latency_ms": 7},
        "_rich_outputs": [{"mime": "image/png"}],
    }
    with_principal(_principal())

    result = _mcp_tools_module._dispatch_via_mcp("search_vector", {"query": "x"})
    assert result == {"hits": ["x"]}
    assert "_internal_telemetry" not in result
    assert "_rich_outputs" not in result


def test_dispatch_logs_call_id_for_lineage(
    with_principal, fake_state, fake_dispatch, caplog
):
    """Forward-compat hook for Phase C: every dispatch logs a call_id
    + the params keys + latency. Wave 3.5 swaps the log line for a
    DB write into ``tool_call_log``."""
    with_principal(_principal())
    fake_dispatch.return_value = {"ok": True}

    with caplog.at_level(logging.INFO, logger="api.routes.mcp_tools"):
        _mcp_tools_module._dispatch_via_mcp(
            "search_vector", {"query": "blue", "top_k": 5}
        )

    log_msgs = [r.getMessage() for r in caplog.records]
    matched = [m for m in log_msgs if "mcp_tool_call" in m]
    assert len(matched) == 1, log_msgs
    msg = matched[0]
    assert "user=u_alice" in msg
    assert "tool=search_vector" in msg
    assert "call_id=" in msg


# ---------------------------------------------------------------------------
# Per-tool wrappers route through with the right shapes
# ---------------------------------------------------------------------------


def test_search_vector_wrapper_routes_correctly(
    with_principal, fake_state, fake_dispatch
):
    fake_dispatch.return_value = {"hits": []}
    with_principal(_principal())
    out = _mcp_tools_module.search_vector("hello", top_k=12)
    assert out == {"hits": []}
    assert fake_dispatch.calls[-1]["tool_name"] == "search_vector"
    assert fake_dispatch.calls[-1]["params"] == {"query": "hello", "top_k": 12}


def test_read_chunk_wrapper_routes_correctly(
    with_principal, fake_state, fake_dispatch
):
    fake_dispatch.return_value = {"chunk_id": "c1", "content": "..."}
    with_principal(_principal())
    out = _mcp_tools_module.read_chunk("c1")
    assert out["chunk_id"] == "c1"
    assert fake_dispatch.calls[-1]["params"] == {"chunk_id": "c1"}


def test_read_tree_omits_empty_node_id(
    with_principal, fake_state, fake_dispatch
):
    """Empty ``node_id`` (default) shouldn't be sent — handler treats
    omission as 'use root'. Sending '' could bypass that."""
    fake_dispatch.return_value = {"node": "root"}
    with_principal(_principal())
    _mcp_tools_module.read_tree("d_abc")
    assert fake_dispatch.calls[-1]["params"] == {"doc_id": "d_abc"}

    # When given, it gets passed
    _mcp_tools_module.read_tree("d_abc", node_id="n1")
    assert fake_dispatch.calls[-1]["params"] == {"doc_id": "d_abc", "node_id": "n1"}


def test_list_folders_omits_empty_parent_path(
    with_principal, fake_state, fake_dispatch
):
    """Default parent_path '' (top-level) is not sent — handler
    treats omission as 'top level'. Sending an empty string would
    bypass that intent."""
    fake_dispatch.return_value = {"folders": []}
    with_principal(_principal())
    _mcp_tools_module.list_folders()
    assert fake_dispatch.calls[-1]["params"] == {}

    _mcp_tools_module.list_folders(parent_path="/data")
    assert fake_dispatch.calls[-1]["params"] == {"parent_path": "/data"}


def test_list_docs_threads_pagination_args(
    with_principal, fake_state, fake_dispatch
):
    fake_dispatch.return_value = {"docs": []}
    with_principal(_principal())
    _mcp_tools_module.list_docs("/data/sales/2025")
    assert fake_dispatch.calls[-1]["params"] == {
        "folder_path": "/data/sales/2025",
        "limit": 50,
        "offset": 0,
    }

    _mcp_tools_module.list_docs("/foo", limit=10, offset=20)
    assert fake_dispatch.calls[-1]["params"] == {
        "folder_path": "/foo",
        "limit": 10,
        "offset": 20,
    }


def test_rerank_passes_chunk_ids_as_list(
    with_principal, fake_state, fake_dispatch
):
    fake_dispatch.return_value = {"hits": []}
    with_principal(_principal())
    _mcp_tools_module.rerank("query", chunk_ids=("c1", "c2", "c3"))
    params = fake_dispatch.calls[-1]["params"]
    assert params["chunk_ids"] == ["c1", "c2", "c3"]


def test_import_from_library_threads_project_id(
    with_principal, fake_state, fake_dispatch
):
    """Legacy project mode: project_id arrives on the ToolContext, and
    explicit target_subdir threads through verbatim. The default-args
    call no longer auto-injects target_subdir — v1.0 shipped the
    cwd-relative ``target_subpath`` path and now defaults to "no
    target", so the handler picks the mode based on which target the
    agent supplied."""
    fake_dispatch.return_value = {"artifact_id": "a1"}
    with_principal(_principal())

    _mcp_tools_module.import_from_library(
        "d_abc", target_subdir="inputs", project_id="p_proj"
    )
    last = fake_dispatch.calls[-1]
    assert last["params"] == {"doc_id": "d_abc", "target_subdir": "inputs"}
    assert last["ctx"].project_id == "p_proj"

    # Empty project_id collapses to None on the ctx
    _mcp_tools_module.import_from_library("d_abc")
    last = fake_dispatch.calls[-1]
    assert last["ctx"].project_id is None
    # Default call carries just doc_id — handler picks the mode.
    assert last["params"] == {"doc_id": "d_abc"}


def test_import_from_library_target_subpath_threads_through(
    with_principal, fake_state, fake_dispatch
):
    """v1.0 folder-as-cwd mode: ``target_subpath`` reaches the handler
    so it can resolve against ``user_workdirs_root`` instead of going
    through ProjectImportService."""
    fake_dispatch.return_value = {"target_path": "sales/2025/inputs/x.txt"}
    with_principal(_principal())

    _mcp_tools_module.import_from_library(
        "d_abc", target_subpath="sales/2025/inputs"
    )
    last = fake_dispatch.calls[-1]
    assert last["params"] == {
        "doc_id": "d_abc",
        "target_subpath": "sales/2025/inputs",
    }


# ---------------------------------------------------------------------------
# Concurrent isolation — same as ping test, but for a real wrapper
# ---------------------------------------------------------------------------


def test_concurrent_calls_isolate_principals(
    fake_state, fake_dispatch
):
    """Alice's call must see u_alice in the dispatched ctx, bob's
    must see u_bob — even running concurrently."""
    fake_dispatch.return_value = lambda *a: {"ok": True}

    async def alice():
        token = set_mcp_principal(_principal("u_alice"))
        try:
            return _mcp_tools_module.search_vector("a", top_k=1)
        finally:
            reset_mcp_principal(token)

    async def bob():
        token = set_mcp_principal(_principal("u_bob"))
        try:
            return _mcp_tools_module.search_vector("b", top_k=1)
        finally:
            reset_mcp_principal(token)

    async def runner():
        return await asyncio.gather(alice(), bob())

    asyncio.run(runner())
    users = sorted(c["ctx"].principal.user_id for c in fake_dispatch.calls)
    assert users == ["u_alice", "u_bob"]
