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
