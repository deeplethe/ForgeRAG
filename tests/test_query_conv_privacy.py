"""
``/api/v1/query`` conversation-privacy guard.

The answering pipeline auto-creates a conversation row when the
client supplies a ``conversation_id`` that doesn't yet exist. Pre
S5.2 it did so without setting ``user_id``, which meant a malicious
client could:

  * post a query with a guessed ``conversation_id`` belonging to
    another user → write into their chat history;
  * post with a fresh id → end up with a NULL-owner row that
    every user could then read.

The guard ``_ensure_conversation_owned`` runs at the route boundary
BEFORE the answering pipeline kicks off:

  * conversation_id missing → no-op (standalone query);
  * id refers to a row owned by the caller → pass through;
  * id refers to a row owned by someone else → 404 (same code as
    "doesn't exist", to never confirm strangers' ids exist);
  * id refers to a missing row → pre-create with caller's user_id
    so the answering pipeline finds an owned row.

These tests exercise the guard in isolation against a tiny fake
state — the actual answering pipeline isn't invoked (we'd need
LLM credentials), and that's fine because the guard runs before
``state.answering.ask``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

from api.auth import AuthenticatedPrincipal
from api.routes.query import _ensure_conversation_owned
from api.schemas import QueryRequest
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import AuthUser, Conversation
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "qcp.db")),
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
        for username in ("alice", "bob"):
            uid = f"u_{username}"
            ids[username] = uid
            sess.add(
                AuthUser(
                    user_id=uid,
                    username=username,
                    email=f"{username}@example.com",
                    password_hash="x",
                    role="user",
                    status="active",
                    is_active=True,
                )
            )
        sess.add(
            Conversation(
                conversation_id="c_alice", title="Alice's", user_id=ids["alice"]
            )
        )
        sess.add(
            Conversation(
                conversation_id="c_legacy", title="Pre-multi", user_id=None
            )
        )
        sess.commit()
    return ids


def _state(store: Store, *, auth_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
    )


def _principal(user_id: str, *, role: str = "user", via: str = "session"):
    return AuthenticatedPrincipal(
        user_id=user_id, username=user_id, role=role, via=via
    )


# ---------------------------------------------------------------------------
# No conversation_id → no-op
# ---------------------------------------------------------------------------


def test_no_conversation_id_is_passthrough(store, seeded):
    state = _state(store)
    req = QueryRequest(query="hello")
    _ensure_conversation_owned(state, _principal(seeded["alice"]), req)


def test_auth_disabled_is_passthrough(store, seeded):
    """Even with a cross-user id, auth-disabled mode skips the check.
    The synthetic local admin is trusted with everything."""
    state = _state(store, auth_enabled=False)
    req = QueryRequest(query="hello", conversation_id="c_alice")
    _ensure_conversation_owned(
        state,
        _principal("local", role="admin", via="auth_disabled"),
        req,
    )


# ---------------------------------------------------------------------------
# Existing conversation: ownership enforcement
# ---------------------------------------------------------------------------


def test_owner_can_continue_their_conversation(store, seeded):
    state = _state(store)
    req = QueryRequest(query="follow up", conversation_id="c_alice")
    _ensure_conversation_owned(state, _principal(seeded["alice"]), req)


def test_other_user_gets_404(store, seeded):
    state = _state(store)
    req = QueryRequest(query="sneaky", conversation_id="c_alice")
    with pytest.raises(HTTPException) as e:
        _ensure_conversation_owned(state, _principal(seeded["bob"]), req)
    assert e.value.status_code == 404


def test_legacy_null_owner_blocks_authenticated_user(store, seeded):
    """A pre-multiuser conversation (user_id=NULL) belongs to the
    legacy single-user world. Authenticated users can't claim it
    by guessing the id."""
    state = _state(store)
    req = QueryRequest(query="legacy claim", conversation_id="c_legacy")
    with pytest.raises(HTTPException) as e:
        _ensure_conversation_owned(state, _principal(seeded["alice"]), req)
    assert e.value.status_code == 404


def test_local_principal_can_claim_legacy_null_owner(store, seeded):
    """In auth-disabled mode the local principal IS the legacy
    single-user; they can continue any user_id=NULL conversation."""
    state = _state(store, auth_enabled=False)
    req = QueryRequest(query="continue legacy", conversation_id="c_legacy")
    _ensure_conversation_owned(
        state,
        _principal("local", role="admin", via="auth_disabled"),
        req,
    )


# ---------------------------------------------------------------------------
# Auto-create with caller's user_id when conversation_id is fresh
# ---------------------------------------------------------------------------


def test_unknown_conversation_id_pre_created_with_owner(store, seeded):
    """Client passes a brand-new id; the guard pre-creates the row
    with the caller's user_id so the answering pipeline finds an
    owned row and skips its own (user_id-less) create branch."""
    state = _state(store)
    req = QueryRequest(query="new chat please", conversation_id="c_fresh")
    _ensure_conversation_owned(state, _principal(seeded["alice"]), req)
    with store.transaction() as sess:
        row = sess.get(Conversation, "c_fresh")
        assert row is not None
        assert row.user_id == seeded["alice"]
        # Title falls back to query[:100], same as the answering
        # pipeline's auto-create would have used.
        assert row.title == "new chat please"


def test_unknown_conversation_id_truncates_long_title(store, seeded):
    """Same title-from-query convention as the answering pipeline:
    truncated at 100 chars."""
    state = _state(store)
    long_q = "x" * 250
    req = QueryRequest(query=long_q, conversation_id="c_long")
    _ensure_conversation_owned(state, _principal(seeded["alice"]), req)
    with store.transaction() as sess:
        row = sess.get(Conversation, "c_long")
        assert row is not None
        assert len(row.title) == 100
