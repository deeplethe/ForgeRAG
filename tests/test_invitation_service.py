"""
FolderInvitationService — token mint / preview / redemption
lifecycle tests.

The contract:

  * Tokens are random 32-byte url-safe strings, returned by
    ``create()`` exactly once. Server stores only the sha256 hash.
  * ``preview()`` and ``consume()`` look up the invitation by raw
    token; expiry / already-consumed / missing-folder all raise
    typed errors so the route can map them to HTTP status codes.
  * ``consume()`` applies the grant via FolderShareService so the
    cascade + audit logic both run; the invitation is marked
    consumed atomically with the grant.
  * Revoke is a hard delete of the row — token immediately stops
    working.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from config import RelationalConfig, SQLiteConfig
from persistence.folder_share_service import _grant_for
from persistence.invitation_service import (
    FolderInvitationService,
    InvitationAlreadyConsumed,
    InvitationError,
    InvitationExpired,
    InvitationFolderMissing,
    InvitationNotFound,
)
from persistence.models import AuditLogRow, AuthUser, Folder, FolderInvitation
from persistence.store import Store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "inv.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict[str, str]:
    """alice owns /research; bob is a stranger who will redeem."""
    ids: dict[str, str] = {}
    with store.transaction() as sess:
        for username, email in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
            uid = f"u_{username}"
            ids[username] = uid
            sess.add(
                AuthUser(
                    user_id=uid,
                    username=username,
                    email=email,
                    password_hash="x",
                    role="user",
                    status="active",
                    is_active=True,
                )
            )
        sess.flush()

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
        sess.commit()
    return ids


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_returns_raw_token_once(store, seeded):
    with store.transaction() as sess:
        issued = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="newcomer@example.com",
            role="rw",
            inviter_user_id=seeded["alice"],
        )
        sess.commit()

    assert issued.token  # raw token returned
    assert issued.folder_path == "/research"
    assert issued.target_email == "newcomer@example.com"
    assert issued.role == "rw"

    # Server stored only the hash.
    with store.transaction() as sess:
        row = sess.execute(
            select(FolderInvitation).where(
                FolderInvitation.invitation_id == issued.invitation_id
            )
        ).scalar_one()
        assert row.token_hash != issued.token  # hash, not the raw value
        assert len(row.token_hash) == 64  # sha256 hex


def test_create_rejects_invalid_role(store, seeded):
    with store.transaction() as sess:
        svc = FolderInvitationService(sess)
        with pytest.raises(InvitationError):
            svc.create(
                folder_id="f_research",
                target_email="x@example.com",
                role="admin",  # type: ignore[arg-type]
                inviter_user_id=seeded["alice"],
            )


def test_create_rejects_missing_folder(store, seeded):
    with store.transaction() as sess:
        svc = FolderInvitationService(sess)
        with pytest.raises(InvitationFolderMissing):
            svc.create(
                folder_id="ghost",
                target_email="x@example.com",
                role="r",
                inviter_user_id=seeded["alice"],
            )


def test_create_lowercases_and_trims_email(store, seeded):
    with store.transaction() as sess:
        issued = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="  Mixed@Example.com  ",
            role="r",
            inviter_user_id=seeded["alice"],
        )
        sess.commit()
    assert issued.target_email == "mixed@example.com"


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


def test_preview_returns_folder_and_inviter_info(store, seeded):
    with store.transaction() as sess:
        issued = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="bob@example.com",
            role="rw",
            inviter_user_id=seeded["alice"],
        )
        sess.commit()

    with store.transaction() as sess:
        preview = FolderInvitationService(sess).preview(issued.token)

    assert preview.folder_path == "/research"
    assert preview.role == "rw"
    assert preview.inviter_username == "alice"
    assert preview.inviter_email == "alice@example.com"


def test_preview_unknown_token_raises(store):
    with store.transaction() as sess:
        with pytest.raises(InvitationNotFound):
            FolderInvitationService(sess).preview("not-a-real-token")


def test_preview_expired_raises(store, seeded):
    with store.transaction() as sess:
        issued = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="bob@example.com",
            role="r",
            inviter_user_id=seeded["alice"],
        )
        sess.commit()

    # Forcibly age the row past expiry rather than sleeping.
    with store.transaction() as sess:
        row = sess.get(FolderInvitation, issued.invitation_id)
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        sess.commit()

    with store.transaction() as sess:
        with pytest.raises(InvitationExpired):
            FolderInvitationService(sess).preview(issued.token)


def test_preview_already_consumed_raises(store, seeded):
    with store.transaction() as sess:
        issued = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="bob@example.com",
            role="r",
            inviter_user_id=seeded["alice"],
        )
        sess.commit()

    with store.transaction() as sess:
        FolderInvitationService(sess).consume(
            token=issued.token, redeemer_user_id=seeded["bob"]
        )
        sess.commit()

    with store.transaction() as sess:
        with pytest.raises(InvitationAlreadyConsumed):
            FolderInvitationService(sess).preview(issued.token)


# ---------------------------------------------------------------------------
# consume
# ---------------------------------------------------------------------------


def test_consume_grants_access_via_share_service(store, seeded):
    """After redemption the recipient appears in folder.shared_with
    with the role the invitation specified."""
    with store.transaction() as sess:
        issued = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="bob@example.com",
            role="rw",
            inviter_user_id=seeded["alice"],
        )
        sess.commit()

    with store.transaction() as sess:
        FolderInvitationService(sess).consume(
            token=issued.token, redeemer_user_id=seeded["bob"]
        )
        sess.commit()

    with store.transaction() as sess:
        f = sess.get(Folder, "f_research")
        assert _grant_for(f.shared_with, seeded["bob"]) == "rw"
        # Invitation row marked consumed.
        inv = sess.get(FolderInvitation, issued.invitation_id)
        assert inv.consumed_at is not None
        assert inv.consumed_by_user_id == seeded["bob"]


def test_consume_double_redemption_rejected(store, seeded):
    with store.transaction() as sess:
        issued = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="bob@example.com",
            role="r",
            inviter_user_id=seeded["alice"],
        )
        sess.commit()
    with store.transaction() as sess:
        FolderInvitationService(sess).consume(
            token=issued.token, redeemer_user_id=seeded["bob"]
        )
        sess.commit()
    with store.transaction() as sess:
        with pytest.raises(InvitationAlreadyConsumed):
            FolderInvitationService(sess).consume(
                token=issued.token, redeemer_user_id=seeded["bob"]
            )


def test_consume_writes_audit_rows(store, seeded):
    with store.transaction() as sess:
        issued = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="bob@example.com",
            role="rw",
            inviter_user_id=seeded["alice"],
        )
        sess.commit()
    with store.transaction() as sess:
        FolderInvitationService(sess).consume(
            token=issued.token, redeemer_user_id=seeded["bob"]
        )
        sess.commit()
    with store.transaction() as sess:
        actions = [
            r.action
            for r in sess.execute(select(AuditLogRow).order_by(AuditLogRow.audit_id)).scalars()
        ]
        assert "folder.invitation.create" in actions
        assert "folder.invitation.consume" in actions
        # The grant write also audits via FolderShareService.
        assert "folder.member.set" in actions


# ---------------------------------------------------------------------------
# list / revoke
# ---------------------------------------------------------------------------


def test_list_filters_consumed_by_default(store, seeded):
    with store.transaction() as sess:
        a = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="x@example.com",
            role="r",
            inviter_user_id=seeded["alice"],
        )
        b = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="y@example.com",
            role="rw",
            inviter_user_id=seeded["alice"],
        )
        sess.commit()
    # Consume one of them.
    with store.transaction() as sess:
        FolderInvitationService(sess).consume(
            token=a.token, redeemer_user_id=seeded["bob"]
        )
        sess.commit()

    with store.transaction() as sess:
        rows = FolderInvitationService(sess).list(folder_id="f_research")
        assert [r.target_email for r in rows] == ["y@example.com"]
        # Include the consumed one when asked.
        rows = FolderInvitationService(sess).list(
            folder_id="f_research", include_consumed=True
        )
        assert {r.target_email for r in rows} == {"x@example.com", "y@example.com"}
        assert {r.invitation_id for r in rows} == {a.invitation_id, b.invitation_id}


def test_revoke_makes_token_unredeemable(store, seeded):
    with store.transaction() as sess:
        issued = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="x@example.com",
            role="r",
            inviter_user_id=seeded["alice"],
        )
        sess.commit()

    with store.transaction() as sess:
        FolderInvitationService(sess).revoke(
            invitation_id=issued.invitation_id, actor_user_id=seeded["alice"]
        )
        sess.commit()

    with store.transaction() as sess:
        with pytest.raises(InvitationNotFound):
            FolderInvitationService(sess).preview(issued.token)


def test_revoke_unknown_invitation_is_silent(store, seeded):
    with store.transaction() as sess:
        # No raise.
        FolderInvitationService(sess).revoke(
            invitation_id="ghost", actor_user_id=seeded["alice"]
        )
        sess.commit()
