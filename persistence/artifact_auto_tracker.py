"""
Auto-Artifact tracking for python_exec / bash_exec — Phase 2.7.

Replaces an explicit ``write_file`` agent tool. The contract:

> Files the agent saves into the project's ``outputs/`` directory
> become ``Artifact`` rows automatically. ``scratch/`` is NOT
> tracked (its semantics are explicitly "safe to delete").

The agent doesn't have to learn / remember to call any "save this"
tool. ``df.to_csv("outputs/x.csv")``, ``plt.savefig("outputs/y.png")``,
``Path("outputs/z.md").write_text(...)``, or even a shell command
like ``bash_exec("xlsx2csv input.xlsx > outputs/dump.csv")`` all
produce auto-tracked Artifact rows with proper lineage —
``{sources: [{type:'code_run', tool:'python_exec'|'bash_exec',
code_hash:'<16-hex>'}]}`` so the run that produced the file is
traceable from the artifact row.

Mechanism: dispatch handlers snapshot ``outputs/`` before the call
(file path → (size, mtime)), execute the code, then diff afterward.
New files OR files whose (size, mtime) changed are recorded.

Why outputs/ only:
- ``scratch/`` is system-managed (already used by ``_rich_outputs``)
  + agent prompt explicitly tells the LLM "scratch = safe to delete"
- ``inputs/`` is for incoming data (uploads + Library imports);
  agent shouldn't be writing there in normal flow
- ``.trash/`` and ``.agent-state/`` are reserved system dirs

Idempotency: re-saving the same file with identical content but
fresh mtime DOES create a fresh Artifact row. That's deliberate —
each save is a snapshot in time the user might care about ("the
report I generated at 2pm vs the one at 4pm"). Dedup by
(project_id, path, sha256) is a Phase 6 polish if customers ask.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .models import Artifact, AuditLogRow

log = logging.getLogger(__name__)


# Subdir we scan for new artifacts. Constant kept here (not imported
# from project_service) because the auto-tracker is allowed to be
# more strict than the file API — outputs/ is a soft convention there
# but a hard rule here.
TRACKED_SUBDIR = "outputs"

# Cap how many auto-Artifact rows a single call can produce. Defends
# against pathological agent loops (e.g. ``for i in range(10000):
# Path(f"outputs/{i}").touch()``) flooding the DB. Excess files are
# logged but not Artifact-rowed; user can still see them in the
# workdir file browser.
MAX_ARTIFACTS_PER_CALL = 50


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


@dataclass
class OutputSnapshot:
    """Pre-call state of ``outputs/``.

    Diffing two snapshots (or one snapshot vs the live filesystem)
    yields the set of new / modified files. Cheap — recursive walk
    of a typically tens-of-files directory; we don't read content.
    """

    # Posix relative path → (size_bytes, mtime_ns)
    files: dict[str, tuple[int, int]] = field(default_factory=dict)


def snapshot_outputs(project_workdir: Path | str) -> OutputSnapshot:
    """Walk ``<project_workdir>/outputs/`` and record (size, mtime)
    per file. Subdirectories are followed; symlinks are NOT followed
    (defense against an agent crafting a symlink to /etc/passwd
    surfacing as an artifact)."""
    out_dir = Path(project_workdir) / TRACKED_SUBDIR
    snap = OutputSnapshot()
    if not out_dir.exists():
        return snap
    try:
        for path in out_dir.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            rel = path.relative_to(project_workdir).as_posix()
            snap.files[rel] = (st.st_size, st.st_mtime_ns)
    except OSError as e:
        log.warning(
            "auto-artifact: snapshot walk failed at %s: %s", out_dir, e
        )
    return snap


# ---------------------------------------------------------------------------
# Diff + persist
# ---------------------------------------------------------------------------


@dataclass
class ChangedFile:
    rel_path: str
    size_bytes: int
    sha256: str
    mime: str
    is_new: bool        # True = wasn't there before; False = modified


def diff_outputs(
    project_workdir: Path | str,
    before: OutputSnapshot,
) -> list[ChangedFile]:
    """Compute set of new/modified files in ``outputs/`` since
    ``before`` was taken.

    Hashes every changed file's content. For typical agent outputs
    (a chart PNG, a markdown report, a CSV) this is microseconds-
    to-milliseconds; for unusual cases (a 500 MB parquet) we pay
    the I/O once. Phase 6 could lazy-hash if it's ever a hot path.
    """
    out_dir = Path(project_workdir) / TRACKED_SUBDIR
    changes: list[ChangedFile] = []
    if not out_dir.exists():
        return changes
    try:
        all_files = list(out_dir.rglob("*"))
    except OSError as e:
        log.warning(
            "auto-artifact: diff walk failed at %s: %s", out_dir, e
        )
        return changes
    for path in all_files:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        rel = path.relative_to(project_workdir).as_posix()
        prev = before.files.get(rel)
        cur = (st.st_size, st.st_mtime_ns)
        if prev == cur:
            continue
        try:
            content = path.read_bytes()
        except OSError as e:
            log.warning(
                "auto-artifact: read failed for %s: %s", path, e
            )
            continue
        sha = hashlib.sha256(content).hexdigest()
        mime, _ = mimetypes.guess_type(path.name)
        changes.append(
            ChangedFile(
                rel_path=rel,
                size_bytes=st.st_size,
                sha256=sha,
                mime=mime or "application/octet-stream",
                is_new=prev is None,
            )
        )
    return changes


def persist_auto_artifacts(
    sess: Session,
    *,
    project_id: str,
    user_id: str | None,
    changes: list[ChangedFile],
    code_hash: str,
    tool: str,
    actor_id: str = "local",
) -> list[Artifact]:
    """Insert ``Artifact`` rows for each entry in ``changes``.

    Returns the (in-session, not-yet-flushed) Artifact instances.
    Caller commits the surrounding transaction. Capped at
    ``MAX_ARTIFACTS_PER_CALL`` rows; excess is logged + audited.

    Lineage shape:
        {
          "sources": [
            {"type": "code_run", "tool": "python_exec", "code_hash": "<16hex>"}
          ],
          "is_new": true     # false = file existed before, content changed
        }

    Phase 2.9 will wire ``run_id`` (from agent_runs.run_id) and
    ``produced_by_step_id`` once the orchestrator threads them
    through. For 2.7 both stay NULL.
    """
    if not changes:
        return []

    over_cap = max(0, len(changes) - MAX_ARTIFACTS_PER_CALL)
    accepted = changes[:MAX_ARTIFACTS_PER_CALL]

    rows: list[Artifact] = []
    for ch in accepted:
        artifact = Artifact(
            artifact_id=uuid.uuid4().hex[:16],
            project_id=project_id,
            run_id=None,
            produced_by_step_id=None,
            path=ch.rel_path,
            mime=ch.mime,
            size_bytes=ch.size_bytes,
            sha256=ch.sha256,
            lineage_json={
                "sources": [
                    {
                        "type": "code_run",
                        "tool": tool,
                        "code_hash": code_hash,
                    }
                ],
                "is_new": ch.is_new,
            },
            metadata_json={"auto_tracked": True},
            user_id=user_id,
        )
        sess.add(artifact)
        rows.append(artifact)

    if over_cap:
        log.warning(
            "auto-artifact: %d files exceeded per-call cap of %d; "
            "%d Artifact rows created, the rest skipped",
            len(changes), MAX_ARTIFACTS_PER_CALL, len(accepted),
        )
        sess.add(
            AuditLogRow(
                actor_id=actor_id,
                action="project.auto_artifact.cap_hit",
                target_type="project",
                target_id=project_id,
                details={
                    "tool": tool,
                    "code_hash": code_hash,
                    "files_total": len(changes),
                    "files_tracked": len(accepted),
                    "files_skipped": over_cap,
                },
            )
        )
    return rows


def hash_code(code: str) -> str:
    """16-hex-char digest of an exec's source — just enough entropy
    for "the run that produced this file" cross-references in
    lineage_json. Stable across processes. Truncation makes it
    cheap to embed in JSON without bloating the column."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
