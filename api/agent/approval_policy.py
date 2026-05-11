"""
Approval policy — classify whether a tool call needs human approval
before execution.

Inc 4 of the long-task / HITL architecture. PreToolUse hook (in the
runtime adapter) calls ``needs_approval(tool_name, tool_input)`` for
every tool the agent picks. If True, the hook emits an
``approval_request`` event, blocks on ``handle.wait_for_approval``, and
honours the user's decision (allow / deny / modified-input).

Design notes:
  * Conservative defaults — only the truly destructive / side-effecting
    tools require approval out of the box. Read-only library / web
    tools auto-allow so the agent stays useful without the user
    babysitting every search.
  * No yaml schema yet. The classifier reads constants below; future
    operators can override via ``cfg.agent.approval`` once we know
    what knobs people actually want.
  * Risk classifier ("destructive / network_write / external_fetch /
    read") attached to each event so the UI's approval card can show a
    sensible reason / icon. The agent's own ask_human escalation
    bypasses approval (asking a human IS the approval).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Risk taxonomy
# ---------------------------------------------------------------------------

RISK_DESTRUCTIVE = "destructive"
RISK_FILE_WRITE = "file_write"
RISK_NETWORK_WRITE = "network_write"
RISK_EXTERNAL_FETCH = "external_fetch"
RISK_SUB_AGENT = "sub_agent"
RISK_READ_ONLY = "read_only"


# ---------------------------------------------------------------------------
# Default policy
# ---------------------------------------------------------------------------
#
# Tools fall into three buckets:
#
#   ALWAYS — every call needs approval. Destructive / file-write /
#            run-arbitrary-code surface lives here.
#
#   NEVER  — auto-allow without surfacing an event. Pure read-only,
#            agent does these constantly, asking would deadlock the UX.
#
#   IF_RISKY — context-sensitive. Pass to the classifier; e.g.
#            ``web_fetch`` of a public URL is fine but POSTing to one
#            isn't.
#
# Tool name match: prefix-match against the bare name AND the
# MCP-prefixed version (``mcp__opencraig__search_vector``).

_ALWAYS_APPROVE = {
    "Bash",
    "BashOutput",
    "KillShell",
    "Edit",
    "Write",
    "NotebookEdit",
    "MultiEdit",
    "Delete",
    # MCP wrappers around the same surface, if/when added
}

_NEVER_APPROVE = {
    "Read",
    "Grep",
    "Glob",
    "WebSearch",  # search ≠ fetch
    # MCP domain tools — all read-only by design
    "search_vector",
    "search_bm25",
    "read_chunk",
    "read_block",
    "read_tree",
    "list_folders",
    "list_docs",
    "graph_explore",
    "rerank",
    "web_search",
    "ask_human",  # escalation — already a human-in-the-loop interaction
}

_IF_RISKY = {
    "WebFetch",
    "web_fetch",
    "import_from_library",
}


@dataclass(frozen=True)
class Decision:
    """Output of the policy. ``allow=True`` means "fire the tool";
    False means "skip without asking" (rare). ``needs_approval`` is the
    "ask the human" branch the runtime hook actually cares about."""

    allow: bool
    needs_approval: bool
    risk: str
    reason: str | None = None


def _bare_tool_name(tool: str) -> str:
    """``mcp__opencraig__search_vector`` → ``search_vector``."""
    if tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return tool


def classify_risk(tool: str, tool_input: dict[str, Any] | None = None) -> str:
    """Risk label for a tool call — used by the UI for icons / colors.

    Pure metadata; doesn't determine whether the call needs approval
    (that's ``needs_approval``). A read-only tool is still tagged
    ``read_only`` even if approval is set to require everything.
    """
    name = _bare_tool_name(tool)
    if name in {"Bash", "BashOutput", "KillShell"}:
        return RISK_DESTRUCTIVE
    if name in {"Edit", "Write", "NotebookEdit", "MultiEdit", "Delete"}:
        return RISK_FILE_WRITE
    if name in {"web_fetch", "WebFetch"}:
        return RISK_EXTERNAL_FETCH
    if name in {"Task", "spawn_subtask"}:
        return RISK_SUB_AGENT
    if name in _NEVER_APPROVE:
        return RISK_READ_ONLY
    return RISK_READ_ONLY


def needs_approval(
    tool: str,
    tool_input: dict[str, Any] | None = None,
    *,
    approval_mode: str = "default",
) -> Decision:
    """Should this tool call wait for human approval?

    ``approval_mode`` lets the deployment override the default policy:

      - 'default'      — standard policy (ALWAYS / NEVER / IF_RISKY)
      - 'paranoid'     — every tool needs approval (except ask_human)
      - 'permissive'   — only ALWAYS list needs approval, everything
                         else flies (treats IF_RISKY as auto-allow)
      - 'bypass'       — never ask (matches the legacy
                         ``permission_mode='bypassPermissions'`` behaviour
                         the runtime used before Inc 4)

    Future: yaml schema under ``cfg.agent.approval`` to add custom
    rules per tool / per user-role. For Inc 4 the constants in this
    file are the source of truth.
    """
    name = _bare_tool_name(tool)
    risk = classify_risk(tool, tool_input)

    if approval_mode == "bypass":
        return Decision(allow=True, needs_approval=False, risk=risk)

    if name == "ask_human":
        # Already a HITL interaction; recursing through approval would
        # create deadlock.
        return Decision(allow=True, needs_approval=False, risk=risk)

    if approval_mode == "paranoid":
        return Decision(
            allow=True,
            needs_approval=True,
            risk=risk,
            reason="paranoid mode: every tool needs approval",
        )

    if name in _ALWAYS_APPROVE:
        return Decision(
            allow=True,
            needs_approval=True,
            risk=risk,
            reason=f"{name} is in the always-approve list ({risk})",
        )

    if name in _NEVER_APPROVE:
        return Decision(allow=True, needs_approval=False, risk=risk)

    if name in _IF_RISKY:
        if approval_mode == "permissive":
            return Decision(allow=True, needs_approval=False, risk=risk)
        # Conservative: ask. Specific input-shape escalations can be
        # added later (e.g. POST methods, private IP fetches).
        return Decision(
            allow=True,
            needs_approval=True,
            risk=risk,
            reason=f"{name} can have side effects ({risk}); asking",
        )

    # Unknown tool — conservative default depends on mode.
    if approval_mode == "permissive":
        return Decision(allow=True, needs_approval=False, risk=risk)
    return Decision(
        allow=True,
        needs_approval=True,
        risk=risk,
        reason=f"unknown tool {name!r}; asking out of caution",
    )
