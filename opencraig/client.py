"""
Thin Python client for a running ForgeRAG server.

For embedding ForgeRAG inside your own app, prefer ``opencraig.components``
(direct, no HTTP). Use this ``Client`` when you have a deployed server
and want to call it over HTTP.

    >>> from opencraig.client import Client
    >>> c = Client("http://localhost:8000")
    >>> answer = c.ask("What are the Q3 revenue trends?")
    >>> for cite in answer.citations_used:
    ...     print(cite.doc_id, cite.page_no, cite.snippet[:80])

Streaming:

    >>> for event, data in c.ask_stream("..."):
    ...     if event == "delta": print(data["text"], end="")

Per-request retrieval overrides:

    >>> answer = c.ask(
    ...     "what's in /legal/2024?",
    ...     path_filter="/legal/2024",
    ...     overrides={"kg_path": False, "rerank_top_k": 20},
    ... )
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
except ImportError as _e:  # pragma: no cover — httpx is a core dep via LiteLLM
    raise ImportError(
        "opencraig.client requires httpx. It's included by default; reinstall the project's requirements.txt to get it."
    ) from _e


# ---------------------------------------------------------------------------
# Lightweight DTOs (duck-typed against the server's pydantic schemas —
# we keep them intentionally loose so SDK users don't pin to specific
# pydantic versions).
# ---------------------------------------------------------------------------


@dataclass
class Citation:
    citation_id: str = ""
    doc_id: str = ""
    file_id: str | None = None
    page_no: int | None = None
    snippet: str = ""
    score: float = 0.0
    highlights: list[dict] = field(default_factory=list)
    open_url: str | None = None


@dataclass
class Answer:
    query: str
    text: str
    model: str | None
    finish_reason: str | None
    citations_used: list[Citation]
    citations_all: list[Citation]
    stats: dict
    trace: dict | None = None  # raw OTel spans payload — None if disabled

    @classmethod
    def from_dict(cls, d: dict) -> Answer:
        return cls(
            query=d.get("query", ""),
            text=d.get("text", ""),
            model=d.get("model"),
            finish_reason=d.get("finish_reason"),
            citations_used=[Citation(**c) for c in (d.get("citations_used") or [])],
            citations_all=[Citation(**c) for c in (d.get("citations_all") or [])],
            stats=d.get("stats") or {},
            trace=d.get("trace"),
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Client:
    """
    Args:
        base_url: Deployed server base, e.g. ``"http://localhost:8000"``.
        token:   API token (Forge_...) — sent as ``Authorization: Bearer``
                 on every request. If unset, falls back to ``$OPENCRAIG_API_TOKEN``
                 (legacy ``$FORGERAG_API_TOKEN`` also accepted).
        timeout: Per-request timeout (seconds) for non-streaming calls.
                 Streaming uses its own idle-timeout.
        headers: Extra HTTP headers.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
    ):
        import os as _os

        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers = dict(headers or {})

        resolved_token = (
            token
            if token is not None
            else (_os.environ.get("OPENCRAIG_API_TOKEN") or _os.environ.get("FORGERAG_API_TOKEN"))
        )
        if resolved_token:
            self._headers.setdefault("Authorization", f"Bearer {resolved_token}")

        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers=self._headers,
        )

    # Context manager + close for resource hygiene in scripts
    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── Health + readiness ──────────────────────────────────────────────

    def health(self) -> dict:
        r = self._client.get("/api/v1/health")
        r.raise_for_status()
        return r.json()

    # ── Query ────────────────────────────────────────────────────────────

    def ask(
        self,
        query: str,
        *,
        conversation_id: str | None = None,
        path_filter: str | None = None,
        overrides: dict | None = None,
        filter: dict | None = None,
    ) -> Answer:
        """Non-streaming query. Returns a full ``Answer`` with trace spans
        (under ``answer.trace``) when the server's observability is on."""
        payload: dict[str, Any] = {"query": query, "stream": False}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if path_filter:
            payload["path_filter"] = path_filter
        if overrides:
            payload["overrides"] = overrides
        if filter:
            payload["filter"] = filter

        r = self._client.post("/api/v1/query", json=payload)
        r.raise_for_status()
        return Answer.from_dict(r.json())

    def ask_stream(
        self,
        query: str,
        *,
        conversation_id: str | None = None,
        path_filter: str | None = None,
        overrides: dict | None = None,
        filter: dict | None = None,
    ) -> Iterator[tuple[str, dict]]:
        """SSE stream. Yields ``(event, data)`` tuples:

        progress  {phase, status, detail?}
        retrieval {vector_hits, bm25_hits, tree_hits, ...}
        delta     {text}
        done      {text, citations_used, citations_all, stats}
        trace     {spans: [...]}
        error     {error, path?, message}
        """
        payload: dict[str, Any] = {"query": query, "stream": True}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if path_filter:
            payload["path_filter"] = path_filter
        if overrides:
            payload["overrides"] = overrides
        if filter:
            payload["filter"] = filter

        with self._client.stream(
            "POST",
            "/api/v1/query",
            json=payload,
            headers={**self._headers, "Accept": "text/event-stream"},
            timeout=None,
        ) as resp:
            resp.raise_for_status()
            event = "message"
            data_buf: list[str] = []
            for line in resp.iter_lines():
                if line is None:
                    continue
                if line == "":
                    if data_buf:
                        raw = "\n".join(data_buf)
                        try:
                            data = json.loads(raw)
                        except Exception:
                            data = {"raw": raw}
                        yield event, data
                    event, data_buf = "message", []
                    continue
                if line.startswith(":"):
                    continue  # SSE comment
                if line.startswith("event:"):
                    event = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_buf.append(line[len("data:") :].lstrip())

    # ── Documents ────────────────────────────────────────────────────────

    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        path_filter: str | None = None,
        recursive: bool = True,
        search: str | None = None,
        status: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset, "recursive": recursive}
        if path_filter:
            params["path_filter"] = path_filter
        if search:
            params["search"] = search
        if status:
            params["status"] = status
        r = self._client.get("/api/v1/documents", params=params)
        r.raise_for_status()
        return r.json()

    def upload(
        self,
        path_or_bytes,
        *,
        filename: str | None = None,
        folder_path: str = "/",
        doc_id: str | None = None,
    ) -> dict:
        """Upload a local file or bytes buffer and queue for ingestion.

        Returns ``{file_id, doc_id, status, message}`` (202 accepted)."""
        import os

        if isinstance(path_or_bytes, (bytes, bytearray)):
            data = bytes(path_or_bytes)
            name = filename or "upload.bin"
        else:
            with open(path_or_bytes, "rb") as f:
                data = f.read()
            name = filename or os.path.basename(path_or_bytes)

        form: dict[str, Any] = {"folder_path": folder_path}
        if doc_id:
            form["doc_id"] = doc_id
        files = {"file": (name, data)}
        r = self._client.post("/api/v1/documents/upload-and-ingest", data=form, files=files)
        r.raise_for_status()
        return r.json()
