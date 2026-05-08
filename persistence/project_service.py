"""
ProjectService — sole entry point for agent-workspace project ops.

All routes that create / rename / share / soft-delete projects MUST
go through this service. Owns:

    1. The relational lifecycle (``projects`` row + ``shared_with``
       grants), audited on every mutation via ``audit_log``.
    2. The on-disk workdir under ``<projects_root>/<project_id>/``,
       with the soft-conventional ``inputs/ outputs/ scratch/
       .agent-state/`` layout the agent's system prompt expects.
    3. Soft-delete: the workdir moves to
       ``<projects_root>/__trash__/<ts>_<id>/`` in lockstep with the
       row's ``trashed_metadata`` write, so a hand-purge of one side
       can't strand the other.

Membership semantics mirror ``Folder.shared_with`` exactly:

    shared_with = [{"user_id": "u_alice", "role": "rw"}, ...]

Owner is implicit (always full access); admin role on ``auth_users``
bypasses every per-project check globally. Day-1 routes only emit
``rw`` collaborators — ``r`` viewers are reserved for a follow-up.
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLogRow, AuthUser, Conversation, Project

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Soft-delete root inside the projects directory. Mirrors the
# Library's ``__trash__`` convention so operators have one mental
# model for "deleted but recoverable" content.
TRASH_DIR_NAME = "__trash__"

_NAME_FORBIDDEN = re.compile(r"[\\/?*<>|\":\x00-\x1f]")
_MAX_NAME_LEN = 255
_MAX_DESC_LEN = 4096

# Soft-conventional subdirs the agent's prompt expects to find.
# Created on every project; the agent is encouraged but NOT forced
# to keep things in the right bucket.
_DEFAULT_SUBDIRS = ("inputs", "outputs", "scratch", ".agent-state")

# README emitted into a fresh project workdir so an operator
# hand-inspecting the directory understands what they're looking at.
_README_TEMPLATE = """\
# {name}

{description}

This directory is the workdir for an OpenCraig agent project.

- `inputs/`  — files imported from the Library or uploaded by the user
- `outputs/` — artifacts the agent produced that are meant to be kept
- `scratch/` — intermediate files (safe to delete)
- `.agent-state/` — plan checkpoints and retry counters (managed by OpenCraig)

Files written here are NOT auto-indexed into the Library. Promote
specific outputs back to the Library via the Workspace UI when you
want them searchable.
"""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProjectError(RuntimeError):
    """Base class for project-service errors."""


class ProjectNotFound(ProjectError):
    pass


class InvalidProjectName(ProjectError):
    pass


class InvalidProjectRole(ProjectError):
    pass


class ProjectMemberNotFound(ProjectError):
    pass


class ProjectMemberConflict(ProjectError):
    """Raised when adding/removing a member would create an
    inconsistent state (e.g. demoting the owner)."""


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class ProjectMember:
    user_id: str
    role: str  # "rw" | "r"


@dataclass
class ProjectInfo:
    project_id: str
    name: str
    description: str | None
    workdir_path: str  # relative to storage root
    owner_user_id: str
    members: list[ProjectMember]
    metadata: dict
    trashed: bool
    created_at: datetime | None
    updated_at: datetime | None
    last_active_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def normalize_project_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise InvalidProjectName("project name cannot be empty")
    if len(name) > _MAX_NAME_LEN:
        raise InvalidProjectName(f"project name exceeds {_MAX_NAME_LEN} chars")
    if _NAME_FORBIDDEN.search(name):
        raise InvalidProjectName(
            f"project name contains forbidden characters: {name!r}"
        )
    return name


def normalize_description(desc: str | None) -> str | None:
    if desc is None:
        return None
    desc = desc.strip()
    if not desc:
        return None
    if len(desc) > _MAX_DESC_LEN:
        raise InvalidProjectName(
            f"description exceeds {_MAX_DESC_LEN} chars"
        )
    return desc


def _validate_role(role: str) -> str:
    if role not in ("r", "rw"):
        raise InvalidProjectRole(f"invalid role {role!r}; expected 'r' or 'rw'")
    return role


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ProjectService:
    """Transactional boundary for project lifecycle operations.

    Usage:
        with store.transaction() as sess:
            svc = ProjectService(sess, projects_root=Path("storage/projects"),
                                 actor_id="u_alice")
            proj = svc.create(name="Q3 contracts", owner_user_id="u_alice")
            sess.commit()

    The on-disk workdir is created BEFORE commit. If the transaction
    rolls back the workdir lingers — the next nightly maintenance run
    sweeps orphan workdirs (rows with no project_id match). The
    inverse failure (commit succeeds, mkdir fails) is the one we work
    to avoid: we mkdir first, and if mkdir raises the row never lands.
    """

    def __init__(
        self,
        sess: Session,
        *,
        projects_root: Path | str,
        actor_id: str = "local",
    ):
        self.sess = sess
        self.projects_root = Path(projects_root)
        self.actor_id = actor_id

    # ── Path helpers ───────────────────────────────────────────────

    @property
    def trash_root(self) -> Path:
        return self.projects_root / TRASH_DIR_NAME

    def workdir_for(self, project_id: str) -> Path:
        """Absolute on-disk path for a project's workdir."""
        return self.projects_root / project_id

    def relative_workdir_path(self, project_id: str) -> str:
        """Path stored in ``projects.workdir_path`` — relative to the
        storage root, so deployments that move ``storage/`` don't have
        to rewrite the column."""
        return f"projects/{project_id}"

    # ── Read ───────────────────────────────────────────────────────

    def get(self, project_id: str) -> Project | None:
        return self.sess.get(Project, project_id)

    def require(self, project_id: str) -> Project:
        p = self.get(project_id)
        if p is None:
            raise ProjectNotFound(project_id)
        return p

    def list_for_user(
        self,
        user_id: str,
        *,
        is_admin: bool = False,
        include_trashed: bool = False,
    ) -> list[Project]:
        """Projects this user can see: owned + shared. Admins see all."""
        rows = list(self.sess.execute(select(Project)).scalars())
        out: list[Project] = []
        for p in rows:
            if not include_trashed and p.trashed_metadata is not None:
                continue
            if is_admin:
                out.append(p)
                continue
            if p.owner_user_id == user_id:
                out.append(p)
                continue
            if any((m or {}).get("user_id") == user_id for m in (p.shared_with or [])):
                out.append(p)
        # Most-recently-active first; never-active rows fall to the end
        # but stay newest-created-first within the NULL bucket.
        out.sort(
            key=lambda p: (
                p.last_active_at or datetime.min,
                p.created_at or datetime.min,
            ),
            reverse=True,
        )
        return out

    def can_access(
        self,
        project: Project,
        user_id: str,
        action: str,
        *,
        is_admin: bool = False,
    ) -> bool:
        """Check whether ``user_id`` may perform ``action`` on
        ``project``.

        Action vocabulary:
          * ``read`` — owner / any member / admin
          * ``write`` — owner / rw member / admin
          * ``share`` — owner / admin (rw members can't reshare; that's
            a deliberately stricter rule than the Library has, because
            project access is tighter scoped)
          * ``delete`` — owner / admin
        """
        if is_admin:
            return True
        if project.owner_user_id == user_id:
            return True
        members = project.shared_with or []
        member = next(
            (m for m in members if (m or {}).get("user_id") == user_id),
            None,
        )
        if member is None:
            return False
        role = member.get("role", "r")
        if action == "read":
            return role in ("r", "rw")
        if action == "write":
            return role == "rw"
        # share / delete are owner-or-admin only — fall through
        return False

    # ── Create ─────────────────────────────────────────────────────

    def create(
        self,
        *,
        name: str,
        owner_user_id: str,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> Project:
        name = normalize_project_name(name)
        description = normalize_description(description)

        project_id = _new_id()
        workdir_rel = self.relative_workdir_path(project_id)

        # Materialize the on-disk layout BEFORE the row lands so that a
        # post-mkdir failure aborts the row insert too. The directory
        # created here lingers if the SQLAlchemy session rolls back —
        # nightly maintenance reaps orphan workdirs.
        self._scaffold_workdir(project_id, name=name, description=description or "")

        proj = Project(
            project_id=project_id,
            name=name,
            description=description,
            workdir_path=workdir_rel,
            owner_user_id=owner_user_id,
            shared_with=[],
            metadata_json=metadata or {},
            trashed_metadata=None,
            last_active_at=None,
        )
        self.sess.add(proj)
        self._audit(
            "project.create",
            project_id,
            {
                "name": name,
                "owner_user_id": owner_user_id,
                "workdir_path": workdir_rel,
            },
        )
        self.sess.flush()
        return proj

    # ── Update ─────────────────────────────────────────────────────

    def rename(self, project_id: str, new_name: str) -> Project:
        proj = self.require(project_id)
        new_name = normalize_project_name(new_name)
        if proj.name == new_name:
            return proj
        old = proj.name
        proj.name = new_name
        self._audit(
            "project.rename",
            project_id,
            {"old_name": old, "new_name": new_name},
        )
        self.sess.flush()
        return proj

    def update_description(
        self, project_id: str, description: str | None
    ) -> Project:
        proj = self.require(project_id)
        desc = normalize_description(description)
        if proj.description == desc:
            return proj
        proj.description = desc
        self._audit(
            "project.update",
            project_id,
            {"field": "description"},
        )
        self.sess.flush()
        return proj

    def touch(self, project_id: str) -> None:
        """Bump ``last_active_at`` to now. Cheap; safe to call on any
        agent-run / artifact write."""
        proj = self.get(project_id)
        if proj is None:
            return
        proj.last_active_at = datetime.utcnow()
        # No audit row for touches — they'd swamp the log.
        self.sess.flush()

    # ── Membership ─────────────────────────────────────────────────

    def list_members(self, project_id: str) -> list[ProjectMember]:
        proj = self.require(project_id)
        return [
            ProjectMember(
                user_id=m.get("user_id", ""),
                role=m.get("role", "r"),
            )
            for m in (proj.shared_with or [])
            if (m or {}).get("user_id")
        ]

    def add_or_update_member(
        self,
        project_id: str,
        *,
        user_id: str,
        role: str,
    ) -> Project:
        role = _validate_role(role)
        proj = self.require(project_id)
        if proj.owner_user_id == user_id:
            raise ProjectMemberConflict(
                "owner already has full access; cannot add as member"
            )
        # Confirm the target user exists — defends against grants to
        # ghost user_ids that auth deletion missed.
        existing = self.sess.get(AuthUser, user_id)
        if existing is None:
            raise ProjectMemberNotFound(user_id)

        members = list(proj.shared_with or [])
        prior_role: str | None = None
        for m in members:
            if (m or {}).get("user_id") == user_id:
                prior_role = m.get("role")
                m["role"] = role
                break
        else:
            members.append({"user_id": user_id, "role": role})
        # SQLAlchemy doesn't track in-place JSON mutations across all
        # backends — reassigning the column makes the change visible.
        proj.shared_with = members
        self._audit(
            "project.update_role" if prior_role else "project.share",
            project_id,
            {
                "user_id": user_id,
                "role": role,
                "prior_role": prior_role,
            },
        )
        self.sess.flush()
        return proj

    def remove_member(self, project_id: str, user_id: str) -> Project:
        proj = self.require(project_id)
        if proj.owner_user_id == user_id:
            raise ProjectMemberConflict("cannot remove the owner from the project")
        members = list(proj.shared_with or [])
        new_members = [m for m in members if (m or {}).get("user_id") != user_id]
        if len(new_members) == len(members):
            raise ProjectMemberNotFound(user_id)
        proj.shared_with = new_members
        self._audit(
            "project.unshare",
            project_id,
            {"user_id": user_id},
        )
        self.sess.flush()
        return proj

    # ── Soft delete / restore ──────────────────────────────────────

    def move_to_trash(self, project_id: str) -> Project:
        proj = self.require(project_id)
        if proj.trashed_metadata is not None:
            return proj
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        original_workdir = proj.workdir_path
        trash_rel = f"projects/{TRASH_DIR_NAME}/{ts}_{project_id}"

        # Move the on-disk workdir BEFORE flipping the row, mirroring
        # the create path's "filesystem first" rule. If the move
        # fails the row is untouched.
        src = self.workdir_for(project_id)
        dst = self.trash_root / f"{ts}_{project_id}"
        if src.exists():
            self.trash_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

        proj.workdir_path = trash_rel
        proj.trashed_metadata = {
            "original_workdir_path": original_workdir,
            "trashed_at": datetime.utcnow().isoformat(),
            "trashed_by": self.actor_id,
        }
        # Drop project_id from any conversations that pointed here so
        # those chats keep working as plain Q&A. The DB FK is ON
        # DELETE SET NULL, but soft-delete doesn't actually delete
        # the row, so we have to do this by hand.
        self.sess.execute(
            Conversation.__table__.update()
            .where(Conversation.project_id == project_id)
            .values(project_id=None)
        )
        self._audit(
            "project.trash",
            project_id,
            {
                "original_workdir_path": original_workdir,
                "trash_workdir_path": trash_rel,
            },
        )
        self.sess.flush()
        return proj

    # ── Internals ──────────────────────────────────────────────────

    def _scaffold_workdir(
        self,
        project_id: str,
        *,
        name: str,
        description: str,
    ) -> Path:
        """Create the on-disk directory tree + README.

        Idempotent: re-running on an existing workdir will not error
        and will not overwrite a hand-edited README.
        """
        workdir = self.workdir_for(project_id)
        workdir.mkdir(parents=True, exist_ok=True)
        for sub in _DEFAULT_SUBDIRS:
            (workdir / sub).mkdir(parents=True, exist_ok=True)
        readme = workdir / "README.md"
        if not readme.exists():
            readme.write_text(
                _README_TEMPLATE.format(
                    name=name,
                    description=description or "_No description yet._",
                ),
                encoding="utf-8",
            )
        return workdir

    def _audit(
        self,
        action: str,
        project_id: str,
        details: dict | None = None,
    ) -> None:
        self.sess.add(
            AuditLogRow(
                actor_id=self.actor_id,
                action=action,
                target_type="project",
                target_id=project_id,
                details=details,
            )
        )
