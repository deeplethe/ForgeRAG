"""
Authorization tests — folder grants + admin bypass + path resolution.

Sets up a tiny folder tree with a few users and asserts every cell
of the permission matrix:

    folders:
        /                 owner=admin
        /research         owner=alice
        /research/2024    owner=alice
        /legal            owner=bob, shared_with [{alice, rw}, {carol, r}]
        /scratch          owner=bob

    users:
        admin   role=admin     status=active
        alice   role=user      status=active
        bob     role=user      status=active
        carol   role=user      status=active
        dan     role=user      status=suspended
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from api.auth.authz import (
    AuthorizationService,
    UnauthorizedPath,
    _role_in_shared_with,
    minimize_paths,
)
from config import RelationalConfig, SQLiteConfig
from persistence.models import AuthUser, Folder
from persistence.store import Store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "authz.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


def _seed(store: Store) -> dict[str, str]:
    """Seed users + folders. Returns ``{name: user_id}`` map."""
    ids: dict[str, str] = {}
    with store.transaction() as sess:
        for username, role, status in (
            ("admin", "admin", "active"),
            ("alice", "user", "active"),
            ("bob", "user", "active"),
            ("carol", "user", "active"),
            ("dan", "user", "suspended"),
        ):
            uid = f"u_{username}"
            ids[username] = uid
            sess.add(
                AuthUser(
                    user_id=uid,
                    username=username,
                    password_hash="x",
                    role=role,
                    status=status,
                    is_active=(status == "active"),
                )
            )
        sess.flush()

        # __root__ already seeded by ensure_schema; admin owns it.
        root = sess.get(Folder, "__root__")
        root.owner_user_id = ids["admin"]

        sess.add(
            Folder(
                folder_id="f_research",
                path="/research",
                path_lower="/research",
                parent_id="__root__",
                name="research",
                owner_user_id=ids["alice"],
            )
        )
        sess.add(
            Folder(
                folder_id="f_research_2024",
                path="/research/2024",
                path_lower="/research/2024",
                parent_id="f_research",
                name="2024",
                owner_user_id=ids["alice"],
            )
        )
        sess.add(
            Folder(
                folder_id="f_legal",
                path="/legal",
                path_lower="/legal",
                parent_id="__root__",
                name="legal",
                owner_user_id=ids["bob"],
                shared_with=[
                    {"user_id": ids["alice"], "role": "rw"},
                    {"user_id": ids["carol"], "role": "r"},
                ],
            )
        )
        sess.add(
            Folder(
                folder_id="f_scratch",
                path="/scratch",
                path_lower="/scratch",
                parent_id="__root__",
                name="scratch",
                owner_user_id=ids["bob"],
            )
        )
        sess.commit()
    return ids


@pytest.fixture
def authz(store) -> AuthorizationService:
    return AuthorizationService(store)


@pytest.fixture
def users(store) -> dict[str, str]:
    return _seed(store)


# ---------------------------------------------------------------------------
# Pure helpers (no DB)
# ---------------------------------------------------------------------------


def test_role_in_shared_with_finds_user():
    sw = [
        {"user_id": "u1", "role": "rw"},
        {"user_id": "u2", "role": "r"},
    ]
    assert _role_in_shared_with(sw, "u1") == "rw"
    assert _role_in_shared_with(sw, "u2") == "r"
    assert _role_in_shared_with(sw, "u3") is None
    assert _role_in_shared_with([], "u1") is None


def test_role_in_shared_with_ignores_garbage():
    sw = [
        "not-a-dict",
        {"user_id": "u1"},  # missing role
        {"user_id": "u1", "role": "weird"},  # bad role
        {"user_id": "u1", "role": "rw"},  # the real one
    ]
    assert _role_in_shared_with(sw, "u1") == "rw"


def test_minimize_paths_drops_redundant_subpaths():
    assert minimize_paths(["/a", "/a/b", "/x"]) == ["/a", "/x"]
    assert minimize_paths(["/a/b", "/a", "/a/b/c"]) == ["/a"]
    # Root absorbs everything.
    assert minimize_paths(["/", "/a", "/b"]) == ["/"]
    # Disjoint paths preserved.
    assert minimize_paths(["/x", "/y", "/z"]) == ["/x", "/y", "/z"]
    # Empty / dedupe.
    assert minimize_paths([]) == []
    assert minimize_paths(["/a", "/a", "/a"]) == ["/a"]


# ---------------------------------------------------------------------------
# can()
# ---------------------------------------------------------------------------


def test_admin_can_do_anything_anywhere(authz, users):
    admin = users["admin"]
    for action in ("read", "upload", "share", "transfer", "purge", "delete_folder"):
        assert authz.can(admin, "f_research", action) is True
        assert authz.can(admin, "f_legal", action) is True


def test_owner_can_do_everything_on_own_folder(authz, users):
    alice = users["alice"]
    for action in ("read", "upload", "share", "transfer", "purge"):
        assert authz.can(alice, "f_research", action) is True


def test_rw_member_can_read_and_write_but_not_manage(authz, users):
    """Alice has rw on /legal (owned by bob)."""
    alice = users["alice"]
    assert authz.can(alice, "f_legal", "read") is True
    assert authz.can(alice, "f_legal", "upload") is True
    assert authz.can(alice, "f_legal", "soft_delete") is True
    # MANAGE_ACTIONS — only owner / admin
    assert authz.can(alice, "f_legal", "share") is False
    assert authz.can(alice, "f_legal", "transfer") is False
    assert authz.can(alice, "f_legal", "purge") is False
    assert authz.can(alice, "f_legal", "delete_folder") is False


def test_r_member_can_only_read(authz, users):
    """Carol has read-only on /legal."""
    carol = users["carol"]
    assert authz.can(carol, "f_legal", "read") is True
    assert authz.can(carol, "f_legal", "search") is True
    assert authz.can(carol, "f_legal", "upload") is False
    assert authz.can(carol, "f_legal", "soft_delete") is False
    assert authz.can(carol, "f_legal", "share") is False


def test_outsider_cannot_read(authz, users):
    """Carol has no grant on /research."""
    carol = users["carol"]
    assert authz.can(carol, "f_research", "read") is False
    assert authz.can(carol, "f_research", "upload") is False


def test_suspended_user_cannot_do_anything(authz, users):
    """Dan is suspended."""
    dan = users["dan"]
    assert authz.can(dan, "__root__", "read") is False


def test_unknown_user_returns_false(authz):
    assert authz.can("ghost", "f_legal", "read") is False


def test_unknown_folder_returns_false(authz, users):
    alice = users["alice"]
    assert authz.can(alice, "ghost_folder", "read") is False


# ---------------------------------------------------------------------------
# resolve_paths()
# ---------------------------------------------------------------------------


def test_resolve_paths_default_returns_minimal_accessible_set(authz, users):
    """Alice owns /research and /research/2024 — the minimal set drops
    the subfolder. She also has rw on /legal."""
    alice = users["alice"]
    paths = authz.resolve_paths(alice, None)
    # /research absorbs /research/2024; /legal stands alone.
    assert sorted(paths) == ["/legal", "/research"]


def test_resolve_paths_admin_default_collapses_to_root(authz, users):
    """Admin owns / — minimal set is just ['/']."""
    admin = users["admin"]
    paths = authz.resolve_paths(admin, None)
    assert paths == ["/"]


def test_resolve_paths_explicit_under_grant_passes(authz, users):
    """Alice can scope to /research/2024 — it's under her /research grant."""
    alice = users["alice"]
    assert authz.resolve_paths(alice, ["/research/2024"]) == ["/research/2024"]


def test_resolve_paths_explicit_outside_grant_403s(authz, users):
    """Alice cannot search /scratch (bob's private)."""
    alice = users["alice"]
    with pytest.raises(UnauthorizedPath) as e:
        authz.resolve_paths(alice, ["/scratch"])
    assert e.value.path == "/scratch"


def test_resolve_paths_admin_passes_arbitrary_paths(authz, users):
    """Admin can pass / explicitly even though they own / — and could
    pass any arbitrary path; admins bypass validation."""
    admin = users["admin"]
    assert authz.resolve_paths(admin, ["/"]) == ["/"]
    assert authz.resolve_paths(admin, ["/scratch", "/research"]) == [
        "/scratch", "/research",
    ]


def test_resolve_paths_multiple_explicit_validates_all(authz, users):
    """Alice has access to /legal and /research — both pass."""
    alice = users["alice"]
    out = authz.resolve_paths(alice, ["/legal", "/research"])
    assert sorted(out) == ["/legal", "/research"]


def test_resolve_paths_one_bad_in_list_raises(authz, users):
    """The first unauthorized path stops validation; carol can't see
    /research even if she also lists /legal."""
    carol = users["carol"]
    with pytest.raises(UnauthorizedPath):
        authz.resolve_paths(carol, ["/legal", "/research"])


def test_resolve_paths_suspended_user_raises(authz, users):
    """Suspended users get an UnauthorizedPath rather than silently
    returning ``['/']`` — they're not allowed to scope to anything."""
    dan = users["dan"]
    with pytest.raises(UnauthorizedPath):
        authz.resolve_paths(dan, None)


# ---------------------------------------------------------------------------
# list_accessible_folders()
# ---------------------------------------------------------------------------


def test_list_accessible_folders_for_owner_and_member(authz, users):
    """Alice owns /research + /research/2024 and is in /legal's shared_with."""
    alice = users["alice"]
    folders = authz.list_accessible_folders(alice)
    paths = sorted(f.path for f in folders)
    assert paths == ["/legal", "/research", "/research/2024"]


def test_list_accessible_folders_for_outsider(authz, users):
    """Carol only has read on /legal."""
    carol = users["carol"]
    folders = authz.list_accessible_folders(carol)
    paths = [f.path for f in folders]
    assert paths == ["/legal"]


def test_list_accessible_folders_for_admin_returns_all_non_trashed(authz, users):
    admin = users["admin"]
    folders = authz.list_accessible_folders(admin)
    paths = sorted(f.path for f in folders)
    # Includes / but excludes /__trash__ (system trash folder).
    assert "/" in paths
    assert "/__trash__" not in paths
    assert "/research" in paths
    assert "/legal" in paths
    assert "/scratch" in paths


def test_list_accessible_folders_excludes_trashed(authz, users, store):
    """Soft-trash a folder; it disappears from list_accessible_folders."""
    with store.transaction() as sess:
        sess.execute(
            select(Folder).where(Folder.folder_id == "f_legal")
        ).scalar_one().trashed_metadata = {"trashed_at": "2026-05-05T00:00:00"}
        sess.commit()
    alice = users["alice"]
    paths = sorted(f.path for f in authz.list_accessible_folders(alice))
    assert "/legal" not in paths
    # Her owned folders stay visible.
    assert "/research" in paths


def test_list_accessible_folders_suspended_user_empty(authz, users):
    dan = users["dan"]
    assert authz.list_accessible_folders(dan) == []
