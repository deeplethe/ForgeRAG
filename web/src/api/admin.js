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
import { get, post, patch, del, request } from './client'

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

// Per-user LLM token usage (admin scope).
export const listUserUsage = () =>
  get('/api/v1/admin/users/usage')

export const getUserUsage = (userId) =>
  get(`/api/v1/admin/users/${userId}/usage`)

// Self-edit (regular users) — currently just display_name.
export const patchMe = (updates) =>
  patch('/api/v1/auth/me', updates)

// Self usage (every user can see their own).
export const getMyUsage = () =>
  get('/api/v1/auth/me/usage')

// ── Avatar (self-edit + lookup) ────────────────────────────
// Upload uses multipart; the backend route accepts ``file`` as
// the form field name and replaces any prior avatar atomically.
export const uploadMyAvatar = (file) => {
  const fd = new FormData()
  fd.append('file', file)
  return request('/api/v1/auth/me/avatar', { method: 'POST', body: fd })
}

export const deleteMyAvatar = () =>
  del('/api/v1/auth/me/avatar')

// URL builder for any user's avatar. Pass ``cacheBust`` when
// you've just mutated the image (e.g. fresh upload) so the
// browser bypasses any cached response. ``hasAvatar`` short-
// circuits the URL generation: returns null when the user has
// no avatar so the caller can skip rendering an <img> entirely.
export const avatarUrlFor = (userId, hasAvatar, cacheBust = 0) => {
  if (!userId || !hasAvatar) return null
  const base = `/api/v1/auth/users/${userId}/avatar`
  return cacheBust ? `${base}?t=${cacheBust}` : base
}
