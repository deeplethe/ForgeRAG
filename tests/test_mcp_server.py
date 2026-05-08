"""
Tests for the MCP server scaffold (Wave 2.2).

Verifies the ``ping`` diagnostic tool is registered + callable,
that the principal ContextVar plumbing roundtrips correctly into
tool handlers, and that ``mount_mcp(app)`` registers the expected
route on a FastAPI app.

HTTP-level integration (auth middleware + actual domain tools +
end-to-end JSON-RPC roundtrip) lands with Wave 2.3 when there's an
auth code path worth exercising. Wave 2.2 sticks to SDK-level
correctness.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")

from fastapi import FastAPI

from api.routes.mcp_server import (
    get_mcp_principal,
    mcp_server,
    mount_mcp,
    reset_mcp_principal,
    set_mcp_principal,
)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_ping_registered_in_tool_list():
    tools = asyncio.run(mcp_server.list_tools())
    names = {t.name for t in tools}
    assert "ping" in names


def test_ping_input_schema_takes_no_args():
    """``ping`` is a zero-arg tool — its inputSchema must accept the
    empty object. If we accidentally add a required field the
    diagnostic stops being useful as a connectivity probe."""
    tools = asyncio.run(mcp_server.list_tools())
    ping = next(t for t in tools if t.name == "ping")
    schema = ping.inputSchema
    # Empty / no-required-fields. FastMCP encodes zero-arg tools with
    # ``properties: {}`` and either no ``required`` or empty list.
    assert schema.get("type") == "object"
    assert schema.get("properties") in (None, {})
    assert schema.get("required") in (None, [])


def test_ping_has_human_readable_description():
    tools = asyncio.run(mcp_server.list_tools())
    ping = next(t for t in tools if t.name == "ping")
    desc = (ping.description or "").lower()
    # The text the agent sees needs to convey what this tool DOES.
    assert "diagnostic" in desc or "reachable" in desc or "ping" in desc


# ---------------------------------------------------------------------------
# Tool execution + ContextVar plumbing
# ---------------------------------------------------------------------------


def test_ping_unauthenticated_returns_no_user():
    """No principal set → tool returns authenticated=False with
    null user fields. This is the Wave 2.2 default; Wave 2.3 wires
    the auth middleware that sets the var."""
    _, structured = asyncio.run(mcp_server.call_tool("ping", {}))
    assert structured["status"] == "ok"
    assert structured["server"] == "opencraig"
    assert structured["authenticated"] is False
    assert structured["user_id"] is None
    assert structured["username"] is None


def test_ping_with_principal_returns_user():
    """Setting the ContextVar in the same async task as the tool
    call propagates into the handler — this is the model Wave 2.3's
    auth middleware will use."""

    async def run():
        principal = SimpleNamespace(
            user_id="u_alice",
            username="alice",
            role="user",
            via="bearer",
        )
        token = set_mcp_principal(principal)
        try:
            return await mcp_server.call_tool("ping", {})
        finally:
            reset_mcp_principal(token)

    _, structured = asyncio.run(run())
    assert structured["authenticated"] is True
    assert structured["user_id"] == "u_alice"
    assert structured["username"] == "alice"


def test_principal_does_not_leak_across_contexts():
    """ContextVar set in one async task must NOT bleed into another.
    This is the safety property that keeps multi-user MCP correct
    once Wave 2.3 turns it on — alice's context shouldn't see bob's
    principal even if their requests interleave."""

    async def alice_calls():
        token = set_mcp_principal(SimpleNamespace(
            user_id="u_alice", username="alice",
            role="user", via="bearer",
        ))
        try:
            return await mcp_server.call_tool("ping", {})
        finally:
            reset_mcp_principal(token)

    async def bob_calls():
        token = set_mcp_principal(SimpleNamespace(
            user_id="u_bob", username="bob",
            role="user", via="bearer",
        ))
        try:
            return await mcp_server.call_tool("ping", {})
        finally:
            reset_mcp_principal(token)

    async def runner():
        # Run them concurrently — each gets its own task, each task
        # gets its own context copy. asyncio.gather guarantees the
        # ContextVars are isolated.
        return await asyncio.gather(alice_calls(), bob_calls())

    (a_content, a_struct), (b_content, b_struct) = asyncio.run(runner())
    assert a_struct["user_id"] == "u_alice"
    assert b_struct["user_id"] == "u_bob"

    # And after both finish, the outer context still has no principal
    assert get_mcp_principal() is None


def test_principal_reset_clears_var():
    async def run():
        token = set_mcp_principal(SimpleNamespace(
            user_id="u_x", username="x", role="user", via="bearer",
        ))
        assert get_mcp_principal() is not None
        reset_mcp_principal(token)
        assert get_mcp_principal() is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Mount on FastAPI
# ---------------------------------------------------------------------------


def test_mount_adds_route_on_fastapi_app():
    app = FastAPI()
    mount_mcp(app)
    # Mounted sub-apps appear in app.routes as Mount instances with a
    # path attribute. We just verify the mount path is there — the
    # inner app's routes are tested by the SDK itself.
    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/api/v1/mcp" in paths


def test_mount_is_idempotent_for_separate_apps():
    """Mounting on two distinct FastAPI app instances doesn't
    interfere — each gets its own mount, the shared ``mcp_server``
    instance handles both. Important for tests that build minimal
    apps in fixtures."""
    app1 = FastAPI()
    app2 = FastAPI()
    mount_mcp(app1)
    mount_mcp(app2)
    assert "/api/v1/mcp" in [getattr(r, "path", "") for r in app1.routes]
    assert "/api/v1/mcp" in [getattr(r, "path", "") for r in app2.routes]
