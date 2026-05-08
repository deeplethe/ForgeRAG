/**
 * Project API client (agent Workspace surface).
 *
 * All endpoints under /api/v1/projects. Returned shapes match the
 * FastAPI schemas in api/routes/projects.py.
 *
 * Distinct from the Library (file manager): Projects are the
 * agent-driven workdir surface — each one owns an on-disk directory
 * under ``storage/projects/<id>/`` plus a chat / artifact lineage.
 * Phase 0 only ships CRUD + member management; Phase 1 adds the
 * file-manager view of the project workdir.
 */

import { del, get, patch, post } from './client'

/** List projects visible to the caller (owned + shared; admins see all). */
export const listProjects = (params = {}) =>
  get('/api/v1/projects', params)

/** Create a new project owned by the caller. */
export const createProject = (name, description = null) =>
  post('/api/v1/projects', { name, description })

/** Single-project detail. */
export const getProject = (project_id) =>
  get(`/api/v1/projects/${project_id}`)

/** Rename / edit description. Pass only the fields you want to change. */
export const updateProject = (project_id, patch_body) =>
  patch(`/api/v1/projects/${project_id}`, patch_body)

/** Soft-delete to projects/__trash__/. Owner / admin only. */
export const deleteProject = (project_id) =>
  del(`/api/v1/projects/${project_id}`)

// ── Membership ──────────────────────────────────────────────────────

/** List effective members (owner + shared_with), joined to auth_users. */
export const listProjectMembers = (project_id) =>
  get(`/api/v1/projects/${project_id}/members`)

/** Add a read-only viewer to the project by email. Owner / admin only.
 *  Role is fixed to 'r' — projects don't support write-share. The
 *  field is left in the request shape for forward-compat with Phase 6+. */
export const addProjectMember = (project_id, email) =>
  post(`/api/v1/projects/${project_id}/members`, { email, role: 'r' })


// ── Project workdir file operations (Phase 1) ───────────────────────

/** List the contents of a directory inside the project workdir.
 *  Empty ``path`` lists the workdir root (excludes the system
 *  ``.trash`` and ``.agent-state`` subdirs). */
export const listProjectFiles = (project_id, path = '') =>
  get(`/api/v1/projects/${project_id}/files`, { path })

/** Multipart upload a file to a path inside the project workdir.
 *  ``file`` is a browser File object; ``path`` is the target relative
 *  path (e.g. "inputs/sales.csv"). ``overwrite`` defaults to false —
 *  collisions return 409 unless explicitly opted in. */
export const uploadProjectFile = async (
  project_id,
  file,
  path,
  { overwrite = false } = {},
) => {
  const fd = new FormData()
  fd.append('file', file, file.name)
  fd.append('path', path)
  fd.append('overwrite', String(overwrite))
  return request(`/api/v1/projects/${project_id}/files`, {
    method: 'POST',
    body: fd,
  })
}

/** Build a download URL for a project file. The route is auth-gated
 *  by the same cookie that drives the rest of the app, so anchor
 *  ``href`` works directly without extra wiring. */
export const projectFileDownloadUrl = (project_id, path) =>
  `/api/v1/projects/${project_id}/files/download?path=${encodeURIComponent(path)}`

/** Rename / move a file within the project workdir. */
export const moveProjectFile = (project_id, from_path, to_path) =>
  patch(`/api/v1/projects/${project_id}/files/move`, { from_path, to_path })

/** Soft-delete a file or directory (lands in .trash/). */
export const deleteProjectFile = (project_id, path) =>
  del(`/api/v1/projects/${project_id}/files`, { path })

/** Create a new subdirectory inside the workdir. */
export const mkdirProjectFile = (project_id, path) =>
  post(`/api/v1/projects/${project_id}/files/mkdir`, { path })


// ── Project workdir trash ───────────────────────────────────────────

/** List trash entries for a project (system-managed). */
export const listProjectTrash = (project_id) =>
  get(`/api/v1/projects/${project_id}/trash`)

/** Restore a single trash entry. Resolves a name collision by
 *  appending "(restored)" before the extension. */
export const restoreProjectTrash = (project_id, trash_id) =>
  post(`/api/v1/projects/${project_id}/trash/${trash_id}/restore`, {})

/** Hard-purge a single trash entry. */
export const purgeProjectTrash = (project_id, trash_id) =>
  del(`/api/v1/projects/${project_id}/trash/${trash_id}`)

/** Empty the project trash. Returns ``{purged_count}``. */
export const emptyProjectTrash = (project_id) =>
  post(`/api/v1/projects/${project_id}/trash/empty`, {})


// ── Library → Workspace import (Phase 1 manual UI; Phase 2 agent tool) ──

/** Copy a Library document's blob into this project's workdir as
 *  an Artifact. Idempotent — importing the same doc twice returns
 *  the existing artifact with ``reused=true``. */
export const importDocFromLibrary = (
  project_id,
  doc_id,
  { target_subdir = 'inputs' } = {},
) =>
  post(`/api/v1/projects/${project_id}/import`, {
    doc_id,
    target_subdir,
  })

/** Remove a member from the project. Owner cannot be removed
 *  (transfer-ownership is a post-Phase-0 feature). */
export const removeProjectMember = (project_id, user_id) =>
  del(`/api/v1/projects/${project_id}/members/${user_id}`)
