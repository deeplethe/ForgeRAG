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
    # Where project workdirs live on disk. Pre-folder-as-cwd, this
    # was where containers bind-mounted per-project subdirs at
    # ``/workdir/<project_id>/``. Under the v0.6.0 OSS folder-as-cwd
    # model the agent path uses ``user_workdirs_root`` instead; this
    # field is retained for legacy code paths (project_files routes,
    # old agent_runs rows referencing project_id) until the cleanup
    # migration drops Project entirely.
    #
    # Local filesystem only by design — containers can't bind-mount
    # S3 / OSS, so the agent surface stays local even when figure
    # storage is remote.
    projects_root: str = "./storage/projects"

    # Where each user's private workdir tree lives on disk.
    # ``<user_workdirs_root>/<user_id>/`` is bind-mounted into the
    # user's sandbox container at ``/workdir/`` — the agent's view
    # of "the user's filesystem".
    #
    # Chat ``cwd_path`` is interpreted RELATIVE to this mount: UI
    # shows ``/sales/2025/``, host has
    # ``<user_workdirs_root>/<user_id>/sales/2025/``, container
    # sees ``/workdir/sales/2025/``. Three-layer mapping aligned.
    #
    # Auto-created on first chat. Files persist across container
    # restarts (the mount is the durable store; the container is
    # ephemeral compute).
    #
    # Set to empty string to disable the folder-as-cwd path and
    # fall back to legacy per-project mounts (rare; intended only
    # for deployments stuck on the pre-refactor data model).
    user_workdirs_root: str = "./storage/user-workdirs"

    # Per-user chat-attachment storage root. Files uploaded inline in
    # the chat (image / PDF / pasted-as-file plain text) live at
    # ``<user_uploads_root>/<user_id>/<conv_id>/<attachment_id>__<name>``.
    # Kept SEPARATE from ``user_workdirs_root`` on purpose — workdir
    # is the agent's read/write surface, attachments are user-supplied
    # context that the agent normally only reads. Mixing them would
    # let the agent overwrite an attachment with a tool call, which
    # is rarely what the user wants.
    user_uploads_root: str = "./storage/user-uploads"

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
