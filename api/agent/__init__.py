"""
Agent tooling — tool definitions + dispatch + per-request context.

This package no longer contains an agent loop of our own. The
agent runtime is the Claude Agent SDK — see
``api/agent/claude_runtime.py`` for the in-process wrapper and
``api/routes/claude_chat.py`` for the SSE chat route. the SDK
reaches our domain capabilities through the MCP server
(``api/routes/mcp_server.py``) which dispatches into the handlers
defined here.

Key invariants enforced at the dispatch boundary (single source
of truth for both the SSE route and any future direct callers):

  * authz: every tool that returns chunks / docs is gated by the
    principal's accessible folder set (path_filters → allowed_doc_ids).
  * trash: trashed-folder docs are excluded from every tool result.
  * KG visibility: entities / relations whose source_doc_ids aren't
    fully covered by the caller's accessible set are dropped (3-tier
    drop, stricter than the API surface — no description redaction
    fallback because LLM context can't render a visibility banner).
  * citation pool: chunks returned by ANY tool land in a per-query
    pool keyed by chunk_id; downstream code picks from this pool to
    render bbox citations.

Public surface:

    ToolContext            — per-query dispatch state (principal, scope,
                              citation pool, store handles)
    ToolSpec               — tool definition (name, schema, handler)
    TOOL_REGISTRY          — name → ToolSpec
    build_tool_context     — assemble a ToolContext at a route's entry
    dispatch               — central tool entry; runs authz + invokes
                              the handler + collects citations
    DispatchError          — uniform error shape returned to the agent
                              when a tool call is invalid / forbidden /
                              raises
"""

from .dispatch import (
    DispatchError,
    ToolContext,
    build_tool_context,
    dispatch,
)
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_REGISTRY, ToolSpec

__all__ = [
    "SYSTEM_PROMPT",
    "TOOL_REGISTRY",
    "DispatchError",
    "ToolContext",
    "ToolSpec",
    "build_tool_context",
    "dispatch",
]
