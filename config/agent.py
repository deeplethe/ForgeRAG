"""
Agent-workspace configuration.

Top-level ``agent:`` section in ``opencraig.yaml``. Phase 0 only
configures the on-disk root for project workdirs; Phase 2 adds
sandbox / runtime / observability subfields here.

Example:
    agent:
      projects_root: ./storage/projects   # default
"""

from __future__ import annotations

from pydantic import BaseModel


class AgentConfig(BaseModel):
    # Where project workdirs live on disk. Containers bind-mount the
    # per-project subdir at /workdir/<project_id>/ in Phase 2; for
    # now the directory just hosts the soft-conventional layout
    # ``inputs/``, ``outputs/``, ``scratch/``, ``.agent-state/`` that
    # ProjectService.scaffold_workdir creates on project creation.
    #
    # Local filesystem only by design — containers can't bind-mount
    # S3 / OSS, so the agent surface stays local even when figure
    # storage is remote.
    projects_root: str = "./storage/projects"

    # Hard upper bound on the total size of a project's workdir
    # (uploads + agent outputs + trash combined). Default ~10 GiB
    # is generous enough for "drop a few datasets and produce some
    # charts" and small enough that a runaway agent writing in a
    # loop doesn't fill the host disk before the next heartbeat.
    # Set to 0 to disable the check (not recommended).
    max_project_workdir_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GiB

    # Soft upper bound on a single uploaded file (multipart upload
    # via the Workspace UI). The Library pipeline already has its
    # own max via ``files.max_bytes``; this is the project-workdir
    # equivalent. Defaults to 500 MiB to mirror the Library default.
    max_workdir_upload_bytes: int = 500 * 1024 * 1024  # 500 MiB
