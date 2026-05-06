/**
 * Admin user management API.
 *
 * All endpoints below are gated by ``role='admin'`` server-side
 * (see ``api/routes/admin.py::_require_admin``). The frontend ALSO
 * gates the /settings/users route via ``meta.requiresAdmin`` —
 * defence in depth. A regular user hitting these helpers directly
 * gets a 403 from the server.
 *
 * GET    /api/v1/admin/users                  list users (filterable)
 * GET    /api/v1/admin/users/{id}             single user
 * POST   /api/v1/admin/users/{id}/approve     pending_approval → active
 * POST   /api/v1/admin/users/{id}/suspend     active → suspended
 * POST   /api/v1/admin/users/{id}/reactivate  suspended → active
 * PATCH  /api/v1/admin/users/{id}             { role?, display_name? }
 * DELETE /api/v1/admin/users/{id}             hard-delete
 */
import { get, post, patch, del } from './client'

export const listUsers = (params) =>
  get('/api/v1/admin/users', params)

export const getUser = (userId) =>
  get(`/api/v1/admin/users/${userId}`)

export const approveUser = (userId) =>
  post(`/api/v1/admin/users/${userId}/approve`)

export const suspendUser = (userId) =>
  post(`/api/v1/admin/users/${userId}/suspend`)

export const reactivateUser = (userId) =>
  post(`/api/v1/admin/users/${userId}/reactivate`)

export const patchUser = (userId, updates) =>
  patch(`/api/v1/admin/users/${userId}`, updates)

export const deleteUser = (userId) =>
  del(`/api/v1/admin/users/${userId}`)

// Self-edit (regular users) — currently just display_name.
export const patchMe = (updates) =>
  patch('/api/v1/auth/me', updates)
