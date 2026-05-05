"""
FolderShareService — cascading membership management tests.

Folder tree used across these cases (only ``shared_with`` matters
for authz; admin reaches everything via role bypass):

    /                    (no shared_with)
    └── /research        shared_with [{alice, rw}]
        └── /research/2024  shared_with [{alice, rw}]
    └── /legal           shared_with [{bob, rw}, {carol, r}]
        └── /legal/contracts  shared_with [{bob, rw}, {carol, r}]

The invariant:  for every user U, every folder F satisfies
    F.shared_with[U]  >=  F.parent.shared_with[U]
(where role weights are r < rw; "no grant" = 0).

The service maintains it at write time so path-prefix retrieval can
trust it at read time.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from config import RelationalConfig, SQLiteConfig
from persistence.folder_share_service import (
    FolderNotFound,
    FolderShareService,
    MembershipConstraintError,
    UserNotFound,
    _grant_for,
)
from persistence.models import AuditLogRow, AuthUser, Folder
from persistence.store import Store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "share.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict[str, str]:
    """Seed users + the folder tree above. Returns ``{name: user_id}``."""
    ids: dict[str, str] = {}
    with store.transaction() as sess:
        for username, role in (
            ("admin", "admin"),
            ("alice", "user"),
            ("bob", "user"),
            ("carol", "user"),
            ("dan", "user"),
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
                shared_with=[
                    {"user_id": ids["bob"], "role": "rw"},
                    {"user_id": ids["carol"], "role": "r"},
                ],
            )
        )
        sess.flush()
        sess.add(
            Folder(
                folder_id="f_research_2024",
                path="/research/2024",
                path_lower="/research/2024",
                parent_id="f_research",
                name="2024",
                shared_with=[{"user_id": ids["alice"], "role": "rw"}],
            )
        )
        sess.add(
            Folder(
                folder_id="f_legal_contracts",
                path="/legal/contracts",
                path_lower="/legal/contracts",
                parent_id="f_legal",
                name="contracts",
                # Cascade copy of /legal's shared_with — the create-
                # subfolder hook does this automatically in
                # production; we hand-fill it here so the fixture
                # already satisfies the superset invariant.
                shared_with=[
                    {"user_id": ids["bob"], "role": "rw"},
                    {"user_id": ids["carol"], "role": "r"},
                ],
            )
        )
        sess.commit()
    return ids


# ---------------------------------------------------------------------------
# set_member_role — basic
# ---------------------------------------------------------------------------


def test_add_member_persists_grant(store, seeded):
    """alice (admin of /research) grants dan read access."""
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        svc.set_member_role(
            folder_id="f_research",
            user_id=seeded["dan"],
            role="r",
            actor_user_id=seeded["alice"],
        )
        sess.commit()
    with store.transaction() as sess:
        f = sess.get(Folder, "f_research")
        assert _grant_for(f.shared_with, seeded["dan"]) == "r"


def test_add_member_cascades_to_descendants(store, seeded):
    """A grant on /research must propagate to /research/2024 to keep
    the superset rule trivial."""
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        svc.set_member_role(
            folder_id="f_research",
            user_id=seeded["dan"],
            role="rw",
            actor_user_id=seeded["alice"],
        )
        sess.commit()
    with store.transaction() as sess:
        f = sess.get(Folder, "f_research")
        sub = sess.get(Folder, "f_research_2024")
        assert _grant_for(f.shared_with, seeded["dan"]) == "rw"
        assert _grant_for(sub.shared_with, seeded["dan"]) == "rw"


def test_add_member_does_not_override_stronger_subfolder_grant(
    store, seeded
):
    """If a subfolder already has a stronger grant for the user
    (manually upgraded earlier), cascading a weaker parent grant
    must NOT downgrade it. This is the asymmetric "child can be more
    permissive" rule in action."""
    with store.transaction() as sess:
        # Pre-seed: dan has rw on /research/2024 directly.
        sub = sess.get(Folder, "f_research_2024")
        sub.shared_with = [{"user_id": seeded["dan"], "role": "rw"}]
        sess.commit()

    with store.transaction() as sess:
        svc = FolderShareService(sess)
        # Now grant dan only 'r' at /research — this must not downgrade
        # /research/2024's existing rw.
        svc.set_member_role(
            folder_id="f_research",
            user_id=seeded["dan"],
            role="r",
            actor_user_id=seeded["alice"],
        )
        sess.commit()
    with store.transaction() as sess:
        sub = sess.get(Folder, "f_research_2024")
        assert _grant_for(sub.shared_with, seeded["dan"]) == "rw"


def test_add_member_upgrades_weaker_existing_grant(store, seeded):
    """If a subfolder has a weaker grant than what's being cascaded
    in, upgrade it. Otherwise the superset invariant breaks the
    moment the parent grant lands."""
    with store.transaction() as sess:
        sub = sess.get(Folder, "f_research_2024")
        sub.shared_with = [{"user_id": seeded["dan"], "role": "r"}]
        sess.commit()

    with store.transaction() as sess:
        svc = FolderShareService(sess)
        svc.set_member_role(
            folder_id="f_research",
            user_id=seeded["dan"],
            role="rw",
            actor_user_id=seeded["alice"],
        )
        sess.commit()
    with store.transaction() as sess:
        sub = sess.get(Folder, "f_research_2024")
        assert _grant_for(sub.shared_with, seeded["dan"]) == "rw"


def test_add_member_same_role_is_idempotent(store, seeded):
    """Setting the same role alice already has on /research (rw) is
    a no-op — no audit row written, no shared_with churn."""
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        svc.set_member_role(
            folder_id="f_research",
            user_id=seeded["alice"],
            role="rw",
            actor_user_id=seeded["alice"],
        )
        sess.commit()
    # No new audit row — alice already had rw at fixture time.
    with store.transaction() as sess:
        rows = (
            sess.execute(
                select(AuditLogRow).where(AuditLogRow.action == "folder.member.set")
            )
            .scalars()
            .all()
        )
        assert rows == []


def test_set_member_role_idempotent(store, seeded):
    """Same role twice writes nothing the second time (no audit
    spam)."""
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        svc.set_member_role(
            folder_id="f_research",
            user_id=seeded["dan"],
            role="r",
            actor_user_id=seeded["alice"],
        )
        sess.commit()
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        svc.set_member_role(
            folder_id="f_research",
            user_id=seeded["dan"],
            role="r",
            actor_user_id=seeded["alice"],
        )
        sess.commit()
    with store.transaction() as sess:
        rows = (
            sess.execute(select(AuditLogRow).where(AuditLogRow.action == "folder.member.set"))
            .scalars()
            .all()
        )
        # Only the first set wrote an audit row.
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# set_member_role — superset enforcement
# ---------------------------------------------------------------------------


def test_set_member_role_rejects_downgrade_below_ancestor(store, seeded):
    """carol has 'r' on /legal (ancestor). Granting her 'r' on
    /legal/contracts is fine; but trying to set 'r' when ancestor
    already has 'rw' would violate superset.

    To exercise that branch, first upgrade carol on /legal to 'rw'
    via the service (cascading to contracts), then try to set 'r'
    explicitly on /legal/contracts."""
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        svc.set_member_role(
            folder_id="f_legal",
            user_id=seeded["carol"],
            role="rw",
            actor_user_id=seeded["bob"],
        )
        sess.commit()

    with store.transaction() as sess:
        svc = FolderShareService(sess)
        with pytest.raises(MembershipConstraintError):
            svc.set_member_role(
                folder_id="f_legal_contracts",
                user_id=seeded["carol"],
                role="r",
                actor_user_id=seeded["bob"],
            )


def test_set_member_role_invalid_role_raises(store, seeded):
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        with pytest.raises(Exception):  # FolderShareError
            svc.set_member_role(
                folder_id="f_research",
                user_id=seeded["dan"],
                role="admin",  # invalid
                actor_user_id=seeded["alice"],
            )


def test_set_member_role_unknown_user(store, seeded):
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        with pytest.raises(UserNotFound):
            svc.set_member_role(
                folder_id="f_research",
                user_id="ghost",
                role="r",
                actor_user_id=seeded["alice"],
            )


def test_set_member_role_unknown_folder(store, seeded):
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        with pytest.raises(FolderNotFound):
            svc.set_member_role(
                folder_id="ghost",
                user_id=seeded["dan"],
                role="r",
                actor_user_id=seeded["alice"],
            )


# ---------------------------------------------------------------------------
# remove_member
# ---------------------------------------------------------------------------


def test_remove_member_drops_grant_and_cascades(store, seeded):
    # Grant dan rw on /research, cascading to /research/2024.
    with store.transaction() as sess:
        FolderShareService(sess).set_member_role(
            folder_id="f_research",
            user_id=seeded["dan"],
            role="rw",
            actor_user_id=seeded["alice"],
        )
        sess.commit()

    with store.transaction() as sess:
        FolderShareService(sess).remove_member(
            folder_id="f_research",
            user_id=seeded["dan"],
            actor_user_id=seeded["alice"],
        )
        sess.commit()

    with store.transaction() as sess:
        assert _grant_for(sess.get(Folder, "f_research").shared_with, seeded["dan"]) is None
        assert _grant_for(sess.get(Folder, "f_research_2024").shared_with, seeded["dan"]) is None


def test_remove_member_rejected_when_user_in_ancestor(store, seeded):
    """carol is on /legal. We can't remove her from /legal/contracts
    while she's still in the ancestor — that would violate the
    superset rule."""
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        # First propagate carol's grant down (cascade fills it in).
        svc.set_member_role(
            folder_id="f_legal",
            user_id=seeded["carol"],
            role="r",
            actor_user_id=seeded["bob"],
        )
        sess.commit()
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        with pytest.raises(MembershipConstraintError) as e:
            svc.remove_member(
                folder_id="f_legal_contracts",
                user_id=seeded["carol"],
                actor_user_id=seeded["bob"],
            )
        assert "f_legal" in str(e.value)


def test_remove_member_idempotent_on_missing_user(store, seeded):
    """Removing a user who was never a member is a silent no-op."""
    with store.transaction() as sess:
        svc = FolderShareService(sess)
        svc.remove_member(
            folder_id="f_research",
            user_id=seeded["dan"],
            actor_user_id=seeded["alice"],
        )
        sess.commit()
    # No audit row for the no-op.
    with store.transaction() as sess:
        rows = (
            sess.execute(
                select(AuditLogRow).where(AuditLogRow.action == "folder.member.remove")
            )
            .scalars()
            .all()
        )
        assert rows == []


# ---------------------------------------------------------------------------
# list_members
# ---------------------------------------------------------------------------


def test_list_members_marks_direct(store, seeded):
    """/legal's shared_with: bob:rw, carol:r — both labelled
    ``direct`` because no ancestor grants the same roles."""
    with store.transaction() as sess:
        rows = FolderShareService(sess).list_members("f_legal")
    sources = {r.user_id: r.source for r in rows}
    assert sources[seeded["bob"]] == "direct"
    assert sources[seeded["carol"]] == "direct"


def test_list_members_marks_inherited(store, seeded):
    """A subfolder shows the parent grant as inherited."""
    with store.transaction() as sess:
        # Grant dan r on /legal (cascades to /legal/contracts).
        FolderShareService(sess).set_member_role(
            folder_id="f_legal",
            user_id=seeded["dan"],
            role="r",
            actor_user_id=seeded["bob"],
        )
        sess.commit()

    with store.transaction() as sess:
        rows = FolderShareService(sess).list_members("f_legal_contracts")
    by_user = {r.user_id: r for r in rows}
    # bob, carol, dan all inherit from /legal because the same role
    # exists on the ancestor (cascade kept them in sync).
    assert by_user[seeded["bob"]].source == "inherited:f_legal"
    assert by_user[seeded["dan"]].source == "inherited:f_legal"
    assert by_user[seeded["carol"]].source == "inherited:f_legal"


# ---------------------------------------------------------------------------
# FolderService.create — copies parent shared_with on new subfolder
# ---------------------------------------------------------------------------


def test_create_subfolder_copies_parent_shared_with(store, seeded):
    """A new folder under an existing parent must inherit the parent's
    shared_with so the subfolder-superset invariant holds the moment
    the folder exists. After creation the lists are independent —
    later edits on either don't move the other."""
    from persistence.folder_service import FolderService

    with store.transaction() as sess:
        sub = FolderService(sess).create(
            parent_path="/legal",
            name="2025",
        )
        sess.commit()
        sub_id = sub.folder_id

    with store.transaction() as sess:
        sub = sess.get(Folder, sub_id)
        # Parent /legal had bob:rw + carol:r at fixture time, so the
        # new /legal/2025 inherits both.
        assert _grant_for(sub.shared_with, seeded["bob"]) == "rw"
        assert _grant_for(sub.shared_with, seeded["carol"]) == "r"
