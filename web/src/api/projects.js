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

/** Add a registered user to the project by email. Owner / admin only.
 *  ``role`` is 'r' or 'rw'. Returns the updated members list. */
export const addProjectMember = (project_id, email, role) =>
  post(`/api/v1/projects/${project_id}/members`, { email, role })

/** Change a member's role. Owner / admin only. */
export const updateProjectMemberRole = (project_id, user_id, role) =>
  patch(`/api/v1/projects/${project_id}/members/${user_id}`, { role })

/** Remove a member from the project. Owner cannot be removed
 *  (transfer-ownership is a post-Phase-0 feature). */
export const removeProjectMember = (project_id, user_id) =>
  del(`/api/v1/projects/${project_id}/members/${user_id}`)
