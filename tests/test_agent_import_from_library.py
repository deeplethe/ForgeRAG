"""
Unit tests for Phase 2.6 — ``import_from_library`` agent tool.

Covers:
- Tool registration: ``import_from_library`` in TOOL_REGISTRY,
  required param ``doc_id``
- ``tools_for(ctx)`` filtering:
    * unbound chat → import_from_library NOT offered
    * bound chat → offered (no sandbox dependency — copying bytes
      from the Library blob store doesn't require the in-container
      agent runtime to be up)
- Dispatch happy path: ProjectImportService.import_doc called with
  the right (project, doc_id, target_subdir); result dict carries
  artifact_id / target_path / size_bytes / mime / reused
- Idempotency surfacing: second call returns ``reused=True``
- target_subdir override (default 'inputs'; reserved subdirs rejected
  by the underlying service)
- Error envelopes:
    * missing / empty doc_id → schema validator
    * unbound chat → "available only in chats bound to a project"
    * non-owner (viewer trying to import) → "available only to the
      project's owner"
    * library doc not accessible → "not found or not accessible"
    * source doc has no blob → "no associated file blob"
    * generic ImportError → "import failed: <msg>"
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from api.agent.dispatch import ToolContext, dispatch, tools_for
from api.agent.tools import TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Fakes mirroring the shape ``_handle_import_from_library`` consumes
# ---------------------------------------------------------------------------


@dataclass
class _FakeImportResult:
    """Mimics persistence.project_import_service.ImportResult."""

    artifact_id: str = "art_xyz"
    project_id: str = "p_a"
    source_doc_id: str = "d_brief"
    target_path: str = "inputs/brief.txt"
    size_bytes: int = 42
    mime: str = "text/plain"
    sha256: str | None = "abc123"
    reused: bool = False


class _FakeProjectImportService:
    """Stub for the real service — assertable + dictates outcomes."""

    instances: list = []

    def __init__(self, sess, *, file_store, projects_root,
                 max_workdir_bytes=0, max_upload_bytes=0, actor_id="local"):
        self.sess = sess
        self.file_store = file_store
        self.projects_root = projects_root
        self.actor_id = actor_id
        self.calls: list[dict] = []
        # Test hooks set on the class via fixture
        self._raises = self.__class__._next_raises
        self._result = self.__class__._next_result
        # Reset for next instance
        self.__class__._next_raises = None
        self.__class__._next_result = None
        self.__class__.instances.append(self)

    _next_raises: Exception | None = None
    _next_result: _FakeImportResult | None = None

    @classmethod
    def configure(cls, *, raises: Exception | None = None,
                  result: _FakeImportResult | None = None) -> None:
        cls._next_raises = raises
        cls._next_result = result

    def import_doc(self, project, doc_id, *, target_subdir):
        self.calls.append({
            "project_id": project.project_id,
            "doc_id": doc_id,
            "target_subdir": target_subdir,
        })
        if self._raises is not None:
            raise self._raises
        return self._result or _FakeImportResult(
            project_id=project.project_id,
            source_doc_id=doc_id,
            target_path=f"{target_subdir}/{doc_id}.txt",
        )


class _FakeFileStore:
    pass


class _FakeProject:
    def __init__(self, project_id: str, owner_user_id: str):
        self.project_id = project_id
        self.owner_user_id = owner_user_id


class _FakeStore:
    """Just enough surface for the handler's two transactions:
    owner check + service construction. Both look up Project rows."""

    def __init__(self, projects: dict[str, _FakeProject]):
        self.projects = projects

    def transaction(self):
        store = self

        class _Sess:
            def __enter__(self_): return self_
            def __exit__(self_, *args): return False
            def get(self_, model, key):
                return store.projects.get(key)
        return _Sess()

    # Conversations mock — _user_can_read_project (chat route) uses
    # this in some tests; not exercised here but keeps the namespace
    # consistent.
    def get_conversation(self, conv_id):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    *,
    user_id: str = "u_alice",
    project_id: str | None = "p_a",
    project_owner: str | None = None,
    has_file_store: bool = True,
    monkeypatch=None,
) -> ToolContext:
    """Build a ToolContext + monkey-patch the import service when needed.

    ``project_owner`` defaults to ``user_id`` (caller IS the owner);
    pass a different value to test the viewer-rejection path.
    """
    if project_owner is None:
        project_owner = user_id
    projects = (
        {project_id: _FakeProject(project_id, project_owner)}
        if project_id else {}
    )
    state = SimpleNamespace(
        store=_FakeStore(projects),
        file_store=_FakeFileStore() if has_file_store else None,
        cfg=SimpleNamespace(
            agent=SimpleNamespace(
                projects_root="./tmp/projects",
                max_project_workdir_bytes=10 * 1024 * 1024 * 1024,
                max_workdir_upload_bytes=500 * 1024 * 1024,
            ),
            auth=SimpleNamespace(enabled=True),
        ),
        authz=SimpleNamespace(can=lambda *a, **kw: True),
    )
    principal = SimpleNamespace(
        user_id=user_id,
        username=user_id.removeprefix("u_"),
        role="user",
        via="cookie",
    )
    return ToolContext(
        state=state,
        principal=principal,
        accessible=set(),
        path_filters=None,
        allowed_doc_ids=None,
        project_id=project_id,
    )


@pytest.fixture(autouse=True)
def patch_service(monkeypatch):
    """Replace the real ProjectImportService with our fake for every
    test in this module. Tests that need to assert on calls reach
    in via ``_FakeProjectImportService.instances``."""
    _FakeProjectImportService.instances = []
    _FakeProjectImportService._next_raises = None
    _FakeProjectImportService._next_result = None
    import persistence.project_import_service as svc_mod

    monkeypatch.setattr(
        svc_mod, "ProjectImportService", _FakeProjectImportService
    )


@pytest.fixture(autouse=True)
def patch_doc_access(monkeypatch):
    """Default: doc-access check passes. Tests that need the no-access
    path override via the ``deny_doc_access`` fixture-overlay."""
    import api.deps as deps

    monkeypatch.setattr(
        deps,
        "require_doc_access",
        lambda state, principal, doc_id, action="read": {"doc_id": doc_id},
    )


def deny_doc_access(monkeypatch):
    """Helper that individual tests can call to flip the doc-access
    gate to "refused" (handler maps any exception to 404-style
    error)."""
    import api.deps as deps

    def _refuse(*a, **kw):
        from fastapi import HTTPException
        raise HTTPException(404, "doc not found")

    monkeypatch.setattr(deps, "require_doc_access", _refuse)


# ---------------------------------------------------------------------------
# Registration + tools_for filtering
# ---------------------------------------------------------------------------


def test_import_from_library_registered():
    assert "import_from_library" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["import_from_library"]
    assert spec.params_schema["required"] == ["doc_id"]
    assert "doc_id" in spec.params_schema["properties"]
    assert "target_subdir" in spec.params_schema["properties"]


def test_tools_for_offers_import_when_project_bound():
    """import_from_library is available whenever a project binding
    exists — copying bytes from the Library blob store doesn't need
    the in-container agent runtime to be up."""
    ctx = _ctx(project_id="p_a")
    names = {s.name for s in tools_for(ctx)}
    assert "import_from_library" in names


def test_tools_for_drops_import_when_no_project_binding():
    ctx = _ctx(project_id=None)
    names = {s.name for s in tools_for(ctx)}
    assert "import_from_library" not in names


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_dispatch_happy_path():
    ctx = _ctx()
    result = dispatch(
        "import_from_library",
        {"doc_id": "d_brief"},
        ctx,
    )
    assert result["artifact_id"] == "art_xyz"
    assert result["source_doc_id"] == "d_brief"
    assert result["target_path"] == "inputs/d_brief.txt"
    assert result["mime"] == "text/plain"
    assert result["reused"] is False

    # Service was called with the right args
    assert len(_FakeProjectImportService.instances) == 1
    svc = _FakeProjectImportService.instances[0]
    assert svc.calls == [
        {"project_id": "p_a", "doc_id": "d_brief", "target_subdir": "inputs"}
    ]


def test_dispatch_passes_through_target_subdir():
    ctx = _ctx()
    dispatch(
        "import_from_library",
        {"doc_id": "d_brief", "target_subdir": "scratch"},
        ctx,
    )
    svc = _FakeProjectImportService.instances[0]
    assert svc.calls[0]["target_subdir"] == "scratch"


def test_dispatch_idempotent_surfaces_reused_flag():
    _FakeProjectImportService.configure(
        result=_FakeImportResult(
            artifact_id="art_existing",
            target_path="inputs/already_there.txt",
            reused=True,
        ),
    )
    ctx = _ctx()
    result = dispatch(
        "import_from_library",
        {"doc_id": "d_existing"},
        ctx,
    )
    assert result["reused"] is True
    assert result["artifact_id"] == "art_existing"


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------


def test_dispatch_missing_doc_id():
    ctx = _ctx()
    result = dispatch("import_from_library", {}, ctx)
    assert "error" in result
    assert "doc_id" in result["error"]


def test_dispatch_empty_doc_id():
    ctx = _ctx()
    result = dispatch("import_from_library", {"doc_id": "   "}, ctx)
    assert "error" in result
    assert "non-empty" in result["error"]


def test_dispatch_no_project_binding_returns_clean_error():
    ctx = _ctx(project_id=None)
    result = dispatch("import_from_library", {"doc_id": "d_brief"}, ctx)
    assert "error" in result
    assert "bound to a project" in result["error"]


def test_dispatch_viewer_cannot_import():
    """Viewer (read-only share) chats are bound to a project but
    must not write into it. The route's two-gate authz handles this
    for HTTP; the agent tool re-checks so the LLM sees a clean
    refusal not an opaque service error."""
    ctx = _ctx(project_owner="u_alice", user_id="u_bob")
    result = dispatch("import_from_library", {"doc_id": "d_brief"}, ctx)
    assert "error" in result
    assert "owner" in result["error"]


def test_dispatch_library_doc_inaccessible(monkeypatch):
    deny_doc_access(monkeypatch)
    ctx = _ctx()
    result = dispatch("import_from_library", {"doc_id": "d_secret"}, ctx)
    assert "error" in result
    assert "not found" in result["error"] or "not accessible" in result["error"]


def test_dispatch_source_doc_has_no_blob():
    from persistence.project_import_service import SourceDocumentHasNoBlob

    _FakeProjectImportService.configure(
        raises=SourceDocumentHasNoBlob("d_orphan"),
    )
    ctx = _ctx()
    result = dispatch("import_from_library", {"doc_id": "d_orphan"}, ctx)
    assert "error" in result
    assert "no associated file blob" in result["error"]


def test_dispatch_generic_import_error():
    from persistence.project_import_service import ImportError as _IE

    _FakeProjectImportService.configure(raises=_IE("disk full"))
    ctx = _ctx()
    result = dispatch("import_from_library", {"doc_id": "d_brief"}, ctx)
    assert "error" in result
    assert "import failed" in result["error"]


def test_dispatch_no_file_store_returns_clean_error():
    ctx = _ctx(has_file_store=False)
    result = dispatch("import_from_library", {"doc_id": "d_brief"}, ctx)
    assert "error" in result
    assert "file store" in result["error"]
