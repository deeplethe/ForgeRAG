"""
Route-level authz tests — the ``resolve_path_filters`` dependency
that ``/search`` and ``/query`` both call before retrieval.

This is the integration point between the request layer and the
``AuthorizationService``. Two failure modes matter:

  1. Non-admin asks for a path they have no grant on: 403 with the
     offending path in the body.
  2. Auth is disabled: the helper is a passthrough so single-user
     deploys keep behaving exactly like they did before.

The HTTP wiring itself is one function call per route; covering
this helper in isolation gives us the actual logic without the
cost of spinning up a full ``AppState`` (which boots vector store,
ingest queue, etc.).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.auth import AuthenticatedPrincipal
from api.auth.authz import AuthorizationService
from api.deps import resolve_path_filters
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import AuthUser, Folder
from persistence.store import Store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "ra.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict[str, str]:
    """alice owns /research; bob is admin; carol has 'r' on /legal."""
    ids: dict[str, str] = {}
    with store.transaction() as sess:
        for username, role in (
            ("admin", "admin"),
            ("alice", "user"),
            ("carol", "user"),
        ):
            uid = f"u_{username}"
            ids[username] = uid
            sess.add(
                AuthUser(
                    user_id=uid,
                    username=username,
                    password_hash="x",
                    role=role,
                    status="active",
                    is_active=True,
                )
            )
        sess.flush()

        # __root__ has no shared_with — admin reaches it via role bypass.
        sess.add(
            Folder(
                folder_id="f_research",
                path="/research",
                path_lower="/research",
                parent_id="__root__",
                name="research",
                shared_with=[{"user_id": ids["alice"], "role": "rw"}],
            )
        )
        sess.add(
            Folder(
                folder_id="f_legal",
                path="/legal",
                path_lower="/legal",
                parent_id="__root__",
                name="legal",
                shared_with=[{"user_id": ids["carol"], "role": "r"}],
            )
        )
        sess.commit()
    return ids


def _state(store: Store, *, auth_enabled: bool):
    """Minimal AppState stand-in covering the two attributes
    ``resolve_path_filters`` reads."""
    return SimpleNamespace(
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
        authz=AuthorizationService(store),
    )


def _principal(user_id: str, role: str = "user") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=user_id,
        username=user_id,
        role=role,
        via="session",
    )


# ---------------------------------------------------------------------------
# auth disabled → passthrough
# ---------------------------------------------------------------------------


def test_auth_disabled_passes_filters_through(store):
    state = _state(store, auth_enabled=False)
    principal = _principal("anyone", role="admin")  # role is ignored too
    # No DB validation runs — the requested list is returned as-is.
    assert resolve_path_filters(state, principal, ["/anywhere"]) == ["/anywhere"]
    assert resolve_path_filters(state, principal, None) is None


# ---------------------------------------------------------------------------
# auth enabled — happy paths
# ---------------------------------------------------------------------------


def test_admin_passes_arbitrary_paths(store, seeded):
    state = _state(store, auth_enabled=True)
    p = _principal(seeded["admin"], role="admin")
    assert resolve_path_filters(state, p, ["/legal", "/scratch/anywhere"]) == [
        "/legal", "/scratch/anywhere",
    ]


def test_admin_default_returns_their_accessible_set(store, seeded):
    """Admin owns /; with no explicit filter, the default collapses
    to ['/']."""
    state = _state(store, auth_enabled=True)
    p = _principal(seeded["admin"], role="admin")
    assert resolve_path_filters(state, p, None) == ["/"]


def test_owner_can_scope_to_own_folder(store, seeded):
    state = _state(store, auth_enabled=True)
    p = _principal(seeded["alice"])
    assert resolve_path_filters(state, p, ["/research"]) == ["/research"]


def test_member_can_scope_to_shared_folder(store, seeded):
    state = _state(store, auth_enabled=True)
    p = _principal(seeded["carol"])
    assert resolve_path_filters(state, p, ["/legal"]) == ["/legal"]


def test_default_returns_minimal_accessible_set(store, seeded):
    state = _state(store, auth_enabled=True)
    p = _principal(seeded["alice"])
    # Alice owns /research and has no shared grants.
    assert resolve_path_filters(state, p, None) == ["/research"]


# ---------------------------------------------------------------------------
# auth enabled — 403 cases
# ---------------------------------------------------------------------------


def test_unauthorised_path_raises_403(store, seeded):
    state = _state(store, auth_enabled=True)
    p = _principal(seeded["alice"])
    with pytest.raises(HTTPException) as e:
        resolve_path_filters(state, p, ["/legal"])
    assert e.value.status_code == 403
    assert e.value.detail["error"] == "unauthorized_path"
    assert e.value.detail["path"] == "/legal"


def test_partial_authorisation_still_403s(store, seeded):
    """One bad path in a list is enough; the helper stops at first."""
    state = _state(store, auth_enabled=True)
    p = _principal(seeded["alice"])
    with pytest.raises(HTTPException) as e:
        # /research is fine but /legal is not.
        resolve_path_filters(state, p, ["/research", "/legal"])
    assert e.value.status_code == 403
    assert e.value.detail["path"] == "/legal"


def test_unknown_user_raises_403(store, seeded):
    state = _state(store, auth_enabled=True)
    p = _principal("ghost")
    with pytest.raises(HTTPException) as e:
        resolve_path_filters(state, p, None)
    assert e.value.status_code == 403


def test_outsider_default_scope_yields_empty_list(store, seeded):
    """If a user has no grants at all, the default-scope path yields
    an empty list. Down the line that means retrieval returns no
    results — the request still succeeds, it just sees nothing."""
    state = _state(store, auth_enabled=True)
    # Bob isn't seeded; create a row for him with no grants.
    with store.transaction() as sess:
        sess.add(
            AuthUser(
                user_id="u_bob",
                username="bob",
                password_hash="x",
                role="user",
                status="active",
                is_active=True,
            )
        )
        sess.commit()
    p = _principal("u_bob")
    assert resolve_path_filters(state, p, None) == []
