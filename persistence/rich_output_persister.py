"""
Rich-output persister — Phase 2.5.

When a ``python_exec`` call produces inline kernel outputs
(matplotlib figures, DataFrame HTML, plotly charts, etc.) we save
them to disk in the project workdir rather than streaming base64
through SSE. Reasoning:

1. **Survives reload**: refresh the chat tab → the trace re-renders
   from disk, no data lost. With inline base64 the image was gone
   the moment the SSE connection closed.
2. **Frontend simplicity**: the existing project file-download API
   already serves bytes from the workdir gated by project read
   access. Frontend just renders ``<img src=download_url>``.
3. **DB stays clean**: no LOB columns, no JSON fields with megabytes
   of base64 in them. The relational model cares about
   ``Artifact`` rows, not figure bytes.
4. **Agent re-use**: the same paths are reachable from a future
   ``read_file`` tool — agent can refer back to "the chart I just
   made" in a follow-up call.

Layout under each project workdir:

    scratch/_rich_outputs/
        README.md                      auto-generated, explains the dir
        <batch>-01.png                 first display_data of the call
        <batch>-02.html                second display_data of the call
        ...

The ``_rich_outputs`` subdir's leading underscore signals
"system-managed; safe to delete; will be regenerated" while still
showing up in the UI file browser. Keeps the convention parallel to
``.trash/`` and ``.agent-state/`` (those are HIDDEN; this one is
visible because the user might want to check what's there).
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Subdir under the project workdir where we drop inline kernel outputs.
RICH_OUTPUT_SUBDIR = "scratch/_rich_outputs"

# Auto-generated README so a user inspecting the file tree can tell
# what these files are. Only written once (idempotent on first hit;
# never overwrites a hand-edited one).
_README_TEXT = """\
# Rich outputs from python_exec

OpenCraig writes inline kernel outputs (matplotlib charts,
DataFrame HTML, plotly figures, etc.) here — one file per
display_data / execute_result frame produced by a `python_exec`
tool call.

Files in this directory are SAFE to delete — they're already shown
in the chat trace where they were produced. If you want to keep
something around, copy it to `outputs/` (which is meant for
"things to keep").

This directory is managed by OpenCraig. Manual edits to existing
files may be overwritten on the next call, but adding new files
here is harmless.
"""

# MIME → (file extension, base64-encoded?). Order matters — when an
# output bundle carries multiple representations (Jupyter typically
# bundles ``image/png`` + ``text/plain`` for matplotlib), we save the
# FIRST hit in this list so the richest format wins. ``text/plain``
# is deliberately omitted: it's already in stdout (KernelManager
# folds execute_result text/plain into stdout).
_MIME_PRIORITY: tuple[tuple[str, str, bool], ...] = (
    # Raster images — always base64 in Jupyter
    ("image/png", ".png", True),
    ("image/jpeg", ".jpg", True),
    ("image/gif", ".gif", True),
    # Vector + structured viz — sent as raw text JSON
    ("image/svg+xml", ".svg", False),
    ("application/vnd.plotly.v1+json", ".plotly.json", False),
    ("application/vnd.vegalite.v5+json", ".vegalite.json", False),
    ("application/vnd.vega.v5+json", ".vega.json", False),
    # HTML — DataFrame.to_html, ipywidgets repr_html, etc.
    ("text/html", ".html", False),
    # JSON / markdown fallbacks
    ("application/json", ".json", False),
    ("text/markdown", ".md", False),
)


@dataclass
class RichOutputRef:
    """Reference to one persisted rich-output file.

    ``path`` is workdir-relative posix (e.g. ``scratch/_rich_outputs/
    abc12345-01.png``) so it round-trips through the existing project
    file-download API without further translation.
    """

    kind: str           # 'display_data' | 'execute_result'
    mime: str           # the MIME type we saved
    path: str           # workdir-relative posix path
    size_bytes: int

    def to_summary_dict(self) -> dict[str, Any]:
        """Shape for ``tool.call_end`` event payloads (the trace UI).
        Same as ``asdict`` but stable across dataclass refactors."""
        return asdict(self)


def persist_rich_outputs(
    rich_outputs: list[dict[str, Any]],
    project_workdir: Path | str,
) -> list[RichOutputRef]:
    """Save each rich output's richest MIME representation to disk.

    Picks the first MIME from ``_MIME_PRIORITY`` that the output
    carries; outputs that ONLY carry ``text/plain`` (or only
    unrecognised MIMEs) are skipped — text/plain already lives in
    stdout, and unknown MIMEs would render as opaque blobs anyway.

    All files for one call share a per-call ``batch`` token so a
    user inspecting the directory can see them grouped.

    Empty / missing rich_outputs returns ``[]`` without touching the
    filesystem at all (no empty dir created).
    """
    if not rich_outputs:
        return []

    target_dir = Path(project_workdir) / RICH_OUTPUT_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)

    readme_path = target_dir / "README.md"
    if not readme_path.exists():
        try:
            readme_path.write_text(_README_TEXT, encoding="utf-8")
        except OSError:
            # Non-fatal — readme is convenience only
            log.warning("rich_outputs: readme write failed at %s", readme_path)

    # Per-call token so the file names sort together and don't collide
    # across rapid back-to-back calls. 8 hex chars = 32 bits = enough
    # for tens of thousands of calls before any birthday-collision
    # concern (and even then collisions are harmless — different seq
    # numbers).
    batch = secrets.token_hex(4)

    refs: list[RichOutputRef] = []
    for idx, output in enumerate(rich_outputs, start=1):
        if not isinstance(output, dict):
            continue
        data = output.get("data") or {}
        if not isinstance(data, dict):
            continue
        # Pick the richest MIME present
        chosen = None
        for mime, ext, is_b64 in _MIME_PRIORITY:
            if mime in data:
                chosen = (mime, ext, is_b64)
                break
        if chosen is None:
            # text/plain only or all-unknown — skip; stdout already
            # has the text representation.
            continue
        mime, ext, is_b64 = chosen
        payload = data[mime]
        try:
            content_bytes = _payload_to_bytes(payload, is_b64=is_b64)
        except Exception as e:
            log.warning(
                "rich_outputs: failed to materialise %s (%s); skipping",
                mime, type(e).__name__,
            )
            continue

        filename = f"{batch}-{idx:02d}{ext}"
        target = target_dir / filename
        try:
            target.write_bytes(content_bytes)
        except OSError as e:
            log.warning(
                "rich_outputs: write failed for %s: %s; skipping",
                target, e,
            )
            continue

        # Workdir-relative posix path — what the frontend will pass
        # to GET /api/v1/projects/{id}/files/download?path=...
        rel_path = f"{RICH_OUTPUT_SUBDIR}/{filename}"
        refs.append(
            RichOutputRef(
                kind=str(output.get("kind", "display_data")),
                mime=mime,
                path=rel_path,
                size_bytes=len(content_bytes),
            )
        )
    return refs


def _payload_to_bytes(payload: Any, *, is_b64: bool) -> bytes:
    """Coerce a rich-output payload to bytes for writing to disk.

    ipykernel encodes binary MIMEs (image/png etc.) as base64 in the
    iopub message. Text MIMEs (image/svg+xml / text/html / *.json)
    arrive as plain strings or sometimes as already-parsed JSON
    structures (plotly's bundle does this) — handle both.
    """
    if is_b64:
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, str):
            return base64.b64decode(payload)
        raise TypeError(
            f"binary MIME payload must be str or bytes, got {type(payload).__name__}"
        )
    # Text MIMEs
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, bytes):
        return payload
    # Plotly / Vega often arrive as already-parsed JSON dicts/lists
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")
