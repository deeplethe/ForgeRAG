"""
Conversation privacy — multi-user.

Conversations are user-private. Even ``role=admin`` does NOT bypass:
the admin role is for managing the shared corpus (folders, tokens,
users), not for reading other users' chat history. Cross-user
access consistently 404s — same code as "doesn't exist" so the
endpoint never confirms a stranger's conversation_id is real.

Setup: alice + bob (active users) and an admin. Each has a couple
of conversations. Tests assert the privacy boundary holds across
list / get / patch / delete / messages / message-add, in
``auth.enabled=true`` mode. A passthrough test confirms the
auth-disabled path keeps single-user behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal
from api.deps import get_state
from api.routes.conversations import router as conversations_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import AuthUser, Conversation, Message
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "conv.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict[str, str]:
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
        # Two conversations per user; one legacy row with user_id=NULL.
        sess.add(Conversation(conversation_id="c_alice_1", title="Alice 1", user_id=ids["alice"]))
        sess.add(Conversation(conversation_id="c_alice_2", title="Alice 2", user_id=ids["alice"]))
        sess.add(Conversation(conversation_id="c_bob_1", title="Bob 1", user_id=ids["bob"]))
        sess.add(Conversation(conversation_id="c_admin_1", title="Admin 1", user_id=ids["admin"]))
        sess.add(Conversation(conversation_id="c_legacy", title="Pre-multiuser", user_id=None))
        sess.flush()
        # A message in alice's conversation we can use later.
        sess.add(
            Message(
                message_id="m_alice_1_1",
                conversation_id="c_alice_1",
                role="user",
                content="hello from alice",
            )
        )
        sess.commit()
    return ids


def _build_app(
    store: Store,
    *,
    auth_enabled: bool = True,
    principal: AuthenticatedPrincipal,
) -> FastAPI:
    fake_state = SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
    )
    app = FastAPI()
    app.include_router(conversations_router)
    app.dependency_overrides[get_state] = lambda: fake_state

    @app.middleware("http")
    async def _set_principal(request: Request, call_next):
        request.state.principal = principal
        return await call_next(request)

    return app


def _alice_principal(seeded: dict[str, str]) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=seeded["alice"], username="alice", role="user", via="session"
    )


def _bob_principal(seeded: dict[str, str]) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=seeded["bob"], username="bob", role="user", via="session"
    )


def _admin_principal(seeded: dict[str, str]) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=seeded["admin"], username="admin", role="admin", via="session"
    )


# ---------------------------------------------------------------------------
# list — every user only sees their own
# ---------------------------------------------------------------------------


def test_list_returns_only_callers_conversations(store, seeded):
    app = _build_app(store, principal=_alice_principal(seeded))
    with TestClient(app) as c:
        body = c.get("/api/v1/conversations").json()
    ids = sorted(it["conversation_id"] for it in body["items"])
    assert ids == ["c_alice_1", "c_alice_2"]
    assert body["total"] == 2


def test_list_admin_does_not_bypass(store, seeded):
    """Admin role does NOT extend to reading other users' chat history.
    Admin only sees their own conversations."""
    app = _build_app(store, principal=_admin_principal(seeded))
    with TestClient(app) as c:
        body = c.get("/api/v1/conversations").json()
    ids = sorted(it["conversation_id"] for it in body["items"])
    assert ids == ["c_admin_1"]


def test_list_passthrough_when_auth_disabled(store, seeded):
    """In auth-off dev mode the synthetic ``local`` admin sees
    everything the legacy single-user dev would have seen."""
    principal = AuthenticatedPrincipal(
        user_id="local", username="local", role="admin", via="auth_disabled"
    )
    app = _build_app(store, auth_enabled=False, principal=principal)
    with TestClient(app) as c:
        body = c.get("/api/v1/conversations").json()
    assert body["total"] == 5  # all rows in the seeded fixture


# ---------------------------------------------------------------------------
# get / patch / delete — privacy boundary
# ---------------------------------------------------------------------------


def test_get_own_conversation_works(store, seeded):
    app = _build_app(store, principal=_alice_principal(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/conversations/c_alice_1")
    assert r.status_code == 200
    assert r.json()["conversation_id"] == "c_alice_1"


def test_get_other_users_conversation_404(store, seeded):
    """Same status code as a missing conversation — never confirm
    that someone else's id is valid."""
    app = _build_app(store, principal=_alice_principal(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/conversations/c_bob_1")
    assert r.status_code == 404


def test_admin_cannot_get_other_users_conversation(store, seeded):
    app = _build_app(store, principal=_admin_principal(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/conversations/c_alice_1")
    assert r.status_code == 404


def test_patch_other_users_conversation_404(store, seeded):
    app = _build_app(store, principal=_alice_principal(seeded))
    with TestClient(app) as c:
        r = c.patch(
            "/api/v1/conversations/c_bob_1",
            json={"title": "I steal!"},
        )
    assert r.status_code == 404
    # Bob's title unchanged.
    with store.transaction() as sess:
        assert sess.get(Conversation, "c_bob_1").title == "Bob 1"


def test_delete_other_users_conversation_404(store, seeded):
    app = _build_app(store, principal=_alice_principal(seeded))
    with TestClient(app) as c:
        r = c.delete("/api/v1/conversations/c_bob_1")
    assert r.status_code == 404
    # Bob's conversation still there.
    with store.transaction() as sess:
        assert sess.get(Conversation, "c_bob_1") is not None


def test_delete_own_conversation_204(store, seeded):
    app = _build_app(store, principal=_alice_principal(seeded))
    with TestClient(app) as c:
        r = c.delete("/api/v1/conversations/c_alice_2")
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# messages — same privacy boundary
# ---------------------------------------------------------------------------


def test_list_messages_other_users_conversation_404(store, seeded):
    app = _build_app(store, principal=_bob_principal(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/conversations/c_alice_1/messages")
    assert r.status_code == 404


def test_list_own_messages_works(store, seeded):
    app = _build_app(store, principal=_alice_principal(seeded))
    with TestClient(app) as c:
        body = c.get("/api/v1/conversations/c_alice_1/messages").json()
    assert len(body) == 1
    assert body[0]["message_id"] == "m_alice_1_1"


def test_add_message_to_other_users_conversation_404(store, seeded):
    app = _build_app(store, principal=_bob_principal(seeded))
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/conversations/c_alice_1/messages",
            json={"role": "user", "content": "wedge"},
        )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# create — auto-owned by caller
# ---------------------------------------------------------------------------


def test_create_records_caller_user_id(store, seeded):
    app = _build_app(store, principal=_alice_principal(seeded))
    with TestClient(app) as c:
        body = c.post("/api/v1/conversations", json={"title": "fresh"}).json()
    cid = body["conversation_id"]
    with store.transaction() as sess:
        row = sess.get(Conversation, cid)
        assert row.user_id == seeded["alice"]


def test_create_in_auth_disabled_records_null_user(store, seeded):
    principal = AuthenticatedPrincipal(
        user_id="local", username="local", role="admin", via="auth_disabled"
    )
    app = _build_app(store, auth_enabled=False, principal=principal)
    with TestClient(app) as c:
        body = c.post("/api/v1/conversations", json={"title": "dev"}).json()
    cid = body["conversation_id"]
    with store.transaction() as sess:
        row = sess.get(Conversation, cid)
        # auth disabled → effective owner is None, conversation lands
        # with user_id=NULL (matches legacy single-user rows).
        assert row.user_id is None


# ---------------------------------------------------------------------------
# Legacy NULL-user_id rows behave correctly
# ---------------------------------------------------------------------------


def test_legacy_null_owner_row_invisible_to_normal_users(store, seeded):
    """A pre-multiuser conversation has user_id=NULL. Authenticated
    users (including admin) shouldn't see it — only the synthetic
    'local' principal in auth-disabled mode does."""
    app = _build_app(store, principal=_alice_principal(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/conversations/c_legacy")
    assert r.status_code == 404


def test_legacy_null_owner_row_visible_in_auth_disabled(store, seeded):
    principal = AuthenticatedPrincipal(
        user_id="local", username="local", role="admin", via="auth_disabled"
    )
    app = _build_app(store, auth_enabled=False, principal=principal)
    with TestClient(app) as c:
        r = c.get("/api/v1/conversations/c_legacy")
    assert r.status_code == 200
