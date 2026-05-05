"""
Agentic chat — LLM-driven retrieval orchestration.

Replaces the fixed BM25 + vector + KG + tree + RRF + rerank pipeline
with an agent loop that calls retrieval primitives as tools. The
agent decides, per user message, whether to retrieve at all and
which combination of primitives + parameters to use.

Key invariants enforced at the dispatch boundary (NOT inside each
tool — single source of truth):

  * authz: every tool that returns chunks / docs is gated by the
    principal's accessible folder set (path_filters → allowed_doc_ids).
  * trash: trashed-folder docs are excluded from every tool result.
  * KG visibility: entities / relations whose source_doc_ids aren't
    fully covered by the caller's accessible set are dropped (3-tier
    drop, stricter than the API surface — no description redaction
    fallback because LLM context can't render a visibility banner).
  * citation pool: chunks returned by ANY tool land in a per-query
    pool keyed by chunk_id; the agent's final ``done(citations=[id…])``
    picks from this pool. The chunk → bbox citation pipeline
    downstream is unchanged.

Public surface:

    ToolContext            — per-query dispatch state (principal, scope,
                              citation pool, store handles)
    ToolSpec               — tool definition (name, schema, handler)
    TOOL_REGISTRY          — name → ToolSpec
    build_tool_context     — assemble a ToolContext at the agent loop's
                              entry point
    dispatch               — central tool entry; runs authz + invokes
                              the handler + collects citations
    DispatchError          — uniform error shape returned to the LLM
                              when a tool call is invalid / forbidden /
                              raises
"""

from .dispatch import (
    DispatchError,
    ToolContext,
    build_tool_context,
    dispatch,
)
from .llm import LiteLLMClient, LLMClient, LLMResponse, ToolCall
from .loop import AgentConfig, AgentLoop, AgentResult
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_REGISTRY, ToolSpec

__all__ = [
    "SYSTEM_PROMPT",
    "TOOL_REGISTRY",
    "AgentConfig",
    "AgentLoop",
    "AgentResult",
    "DispatchError",
    "LLMClient",
    "LLMResponse",
    "LiteLLMClient",
    "ToolCall",
    "ToolContext",
    "ToolSpec",
    "build_tool_context",
    "dispatch",
]
