"""
Unit tests for Phase 2.7's auto-Artifact tracker.

Covers:
- snapshot_outputs: walks outputs/ recursively; ignores symlinks +
  inputs/ + scratch/ + .trash/ + .agent-state/
- diff_outputs: detects new files, modified files, ignores
  unchanged files, picks up content changes (not just mtime)
- persist_auto_artifacts: creates Artifact rows with correct
  lineage (sources[].type='code_run', tool name, code hash);
  caps at MAX_ARTIFACTS_PER_CALL with audit trail
- hash_code: stable, 16-hex
- empty / missing outputs/ → no-op (no error)
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from persistence.artifact_auto_tracker import (
    MAX_ARTIFACTS_PER_CALL,
    TRACKED_SUBDIR,
    ChangedFile,
    OutputSnapshot,
    diff_outputs,
    hash_code,
    persist_auto_artifacts,
    snapshot_outputs,
)
from persistence.models import Artifact, AuditLogRow, AuthUser
from persistence.store import Store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path) -> Path:
    """A project workdir with the standard subdir layout."""
    for sub in ("inputs", "outputs", "scratch", ".agent-state", ".trash"):
        (tmp_path / sub).mkdir()
    return tmp_path


@pytest.fixture
def store(tmp_path) -> Store:
    from config import RelationalConfig, SQLiteConfig

    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "auto_artifact_test.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# snapshot_outputs
# ---------------------------------------------------------------------------


def test_snapshot_empty_outputs_dir(workdir):
    snap = snapshot_outputs(workdir)
    assert snap.files == {}


def test_snapshot_missing_outputs_dir(tmp_path):
    """Workdir without an outputs/ subdir: no error, empty snap."""
    snap = snapshot_outputs(tmp_path)
    assert snap.files == {}


def test_snapshot_records_size_and_mtime(workdir):
    (workdir / "outputs" / "a.txt").write_text("hello")
    (workdir / "outputs" / "b.csv").write_text("x,y\n1,2\n")
    snap = snapshot_outputs(workdir)
    assert set(snap.files) == {"outputs/a.txt", "outputs/b.csv"}
    a_size, a_mtime = snap.files["outputs/a.txt"]
    assert a_size == 5
    assert a_mtime > 0


def test_snapshot_walks_subdirs(workdir):
    sub = workdir / "outputs" / "charts"
    sub.mkdir()
    (sub / "fig.png").write_bytes(b"\x89PNG fake")
    snap = snapshot_outputs(workdir)
    assert "outputs/charts/fig.png" in snap.files


def test_snapshot_ignores_symlinks(workdir):
    target = workdir / "secret.txt"
    target.write_text("private")
    link = workdir / "outputs" / "shortcut.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    snap = snapshot_outputs(workdir)
    # Symlink not surfaced — defends against an agent crafting a
    # link to /etc/passwd that would later become an Artifact row.
    assert "outputs/shortcut.txt" not in snap.files


def test_snapshot_does_not_walk_inputs_scratch_etc(workdir):
    (workdir / "inputs" / "data.csv").write_text("...")
    (workdir / "scratch" / "tmp.txt").write_text("...")
    (workdir / ".trash" / "old.txt").write_text("...")
    (workdir / ".agent-state" / "state.json").write_text("{}")
    (workdir / "outputs" / "report.md").write_text("...")
    snap = snapshot_outputs(workdir)
    assert set(snap.files) == {"outputs/report.md"}


# ---------------------------------------------------------------------------
# diff_outputs
# ---------------------------------------------------------------------------


def test_diff_detects_new_files(workdir):
    before = snapshot_outputs(workdir)
    (workdir / "outputs" / "fresh.txt").write_text("brand new")
    changes = diff_outputs(workdir, before)
    assert len(changes) == 1
    ch = changes[0]
    assert ch.rel_path == "outputs/fresh.txt"
    assert ch.is_new is True
    assert ch.size_bytes == len("brand new")
    assert ch.sha256 == hashlib.sha256(b"brand new").hexdigest()


def test_diff_detects_modifications(workdir):
    p = workdir / "outputs" / "evolving.txt"
    p.write_text("v1")
    before = snapshot_outputs(workdir)
    # Force a different mtime so the (size, mtime) tuple changes
    # even on filesystems with coarse mtime granularity.
    time.sleep(0.01)
    p.write_text("v2 with different content")
    changes = diff_outputs(workdir, before)
    assert len(changes) == 1
    ch = changes[0]
    assert ch.rel_path == "outputs/evolving.txt"
    assert ch.is_new is False
    assert b"v2" in (workdir / ch.rel_path).read_bytes()


def test_diff_ignores_unchanged_files(workdir):
    p = workdir / "outputs" / "stable.txt"
    p.write_text("unchanged")
    before = snapshot_outputs(workdir)
    # No mutation
    changes = diff_outputs(workdir, before)
    assert changes == []


def test_diff_includes_new_file_in_subdir(workdir):
    before = snapshot_outputs(workdir)
    (workdir / "outputs" / "charts").mkdir()
    (workdir / "outputs" / "charts" / "q3.png").write_bytes(b"\x89PNG ...")
    changes = diff_outputs(workdir, before)
    assert any(c.rel_path == "outputs/charts/q3.png" for c in changes)


def test_diff_ignores_changes_outside_outputs(workdir):
    before = snapshot_outputs(workdir)
    (workdir / "scratch" / "noise.txt").write_text("dont care")
    (workdir / "inputs" / "newup.txt").write_text("dont care")
    changes = diff_outputs(workdir, before)
    assert changes == []


# ---------------------------------------------------------------------------
# persist_auto_artifacts
# ---------------------------------------------------------------------------


def _seed_user(store: Store, user_id: str = "u_alice") -> str:
    with store.transaction() as sess:
        sess.add(
            AuthUser(
                user_id=user_id,
                username=user_id.removeprefix("u_"),
                password_hash="x",
                role="user",
                status="active",
                is_active=True,
            )
        )
        sess.commit()
    return user_id


def _seed_project(store: Store, project_id: str, owner_user_id: str) -> str:
    from persistence.models import Project

    with store.transaction() as sess:
        sess.add(
            Project(
                project_id=project_id,
                name="Test",
                workdir_path=f"projects/{project_id}",
                owner_user_id=owner_user_id,
            )
        )
        sess.commit()
    return project_id


def test_persist_creates_artifact_rows(store, workdir):
    user_id = _seed_user(store)
    project_id = _seed_project(store, "p_a", user_id)

    changes = [
        ChangedFile(
            rel_path="outputs/a.csv",
            size_bytes=100,
            sha256="a" * 64,
            mime="text/csv",
            is_new=True,
        ),
        ChangedFile(
            rel_path="outputs/b.png",
            size_bytes=2000,
            sha256="b" * 64,
            mime="image/png",
            is_new=False,
        ),
    ]
    with store.transaction() as sess:
        rows = persist_auto_artifacts(
            sess,
            project_id=project_id,
            user_id=user_id,
            changes=changes,
            code_hash="abc1234567890def",
            tool="python_exec",
            actor_id=user_id,
        )
        sess.commit()

    assert len(rows) == 2
    # Read back via fresh session
    from sqlalchemy import select

    with store.transaction() as sess:
        all_arts = list(
            sess.execute(
                select(Artifact).where(Artifact.project_id == project_id)
            ).scalars()
        )
    assert {a.path for a in all_arts} == {"outputs/a.csv", "outputs/b.png"}
    a = next(x for x in all_arts if x.path == "outputs/a.csv")
    assert a.run_id is None  # Phase 2.9 wires
    assert a.size_bytes == 100
    assert a.sha256 == "a" * 64
    assert a.mime == "text/csv"
    sources = a.lineage_json["sources"]
    assert len(sources) == 1
    assert sources[0]["type"] == "code_run"
    assert sources[0]["tool"] == "python_exec"
    assert sources[0]["code_hash"] == "abc1234567890def"
    assert a.lineage_json["is_new"] is True
    assert a.metadata_json.get("auto_tracked") is True
    assert a.user_id == user_id


def test_persist_caps_at_max_per_call(store):
    user_id = _seed_user(store, "u_alice2")
    project_id = _seed_project(store, "p_cap", user_id)

    over = MAX_ARTIFACTS_PER_CALL + 5
    changes = [
        ChangedFile(
            rel_path=f"outputs/f{i:03d}.txt",
            size_bytes=10,
            sha256="c" * 64,
            mime="text/plain",
            is_new=True,
        )
        for i in range(over)
    ]
    with store.transaction() as sess:
        rows = persist_auto_artifacts(
            sess,
            project_id=project_id,
            user_id=user_id,
            changes=changes,
            code_hash="cap0000000000000",
            tool="bash_exec",
            actor_id=user_id,
        )
        sess.commit()

    assert len(rows) == MAX_ARTIFACTS_PER_CALL

    # Audit log records the cap-hit event with the original count
    from sqlalchemy import select

    with store.transaction() as sess:
        actions = list(
            sess.execute(
                select(AuditLogRow).where(
                    AuditLogRow.target_id == project_id,
                    AuditLogRow.action == "project.auto_artifact.cap_hit",
                )
            ).scalars()
        )
    assert len(actions) == 1
    details = actions[0].details
    assert details["files_total"] == over
    assert details["files_tracked"] == MAX_ARTIFACTS_PER_CALL
    assert details["files_skipped"] == 5
    assert details["tool"] == "bash_exec"


def test_persist_no_changes_no_rows(store):
    user_id = _seed_user(store, "u_alice3")
    project_id = _seed_project(store, "p_empty", user_id)
    with store.transaction() as sess:
        rows = persist_auto_artifacts(
            sess,
            project_id=project_id,
            user_id=user_id,
            changes=[],
            code_hash="",
            tool="python_exec",
            actor_id=user_id,
        )
    assert rows == []


# ---------------------------------------------------------------------------
# hash_code
# ---------------------------------------------------------------------------


def test_hash_code_stable_and_short():
    a = hash_code("print('hello')")
    b = hash_code("print('hello')")
    c = hash_code("print('different')")
    assert a == b
    assert a != c
    assert len(a) == 16
    assert all(ch in "0123456789abcdef" for ch in a)


# ---------------------------------------------------------------------------
# End-to-end flow (snapshot + diff + persist)
# ---------------------------------------------------------------------------


def test_full_flow_snapshot_diff_persist(store, workdir):
    """Realistic flow: snap before; agent runs code that writes
    outputs/x.csv + outputs/y.png; diff sees both; persist creates
    Artifact rows with the right lineage."""
    user_id = _seed_user(store, "u_full")
    project_id = _seed_project(store, "p_full", user_id)

    before = snapshot_outputs(workdir)
    # Simulate the agent's write
    (workdir / "outputs" / "x.csv").write_text("a,b\n1,2\n")
    (workdir / "outputs" / "y.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 80)

    changes = diff_outputs(workdir, before)
    assert {c.rel_path for c in changes} == {"outputs/x.csv", "outputs/y.png"}
    assert all(c.is_new for c in changes)

    code = "df.to_csv('outputs/x.csv'); plt.savefig('outputs/y.png')"
    with store.transaction() as sess:
        persist_auto_artifacts(
            sess,
            project_id=project_id,
            user_id=user_id,
            changes=changes,
            code_hash=hash_code(code),
            tool="python_exec",
            actor_id=user_id,
        )
        sess.commit()

    from sqlalchemy import select

    with store.transaction() as sess:
        arts = list(
            sess.execute(
                select(Artifact).where(Artifact.project_id == project_id)
            ).scalars()
        )
    # Lineage on every row points at the same code_hash
    code_hashes = {a.lineage_json["sources"][0]["code_hash"] for a in arts}
    assert code_hashes == {hash_code(code)}
