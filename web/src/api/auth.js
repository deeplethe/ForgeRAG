/**
 * Auth API — session cookies + password + SK token management.
 *
 * POST /api/v1/auth/login               {username, password} → Set-Cookie + user info
 * POST /api/v1/auth/logout              → 204
 * POST /api/v1/auth/change-password     {old_password, new_password}
 * GET  /api/v1/auth/me                  current principal
 *
 * GET    /api/v1/auth/tokens            list SKs
 * POST   /api/v1/auth/tokens            {name, expires_days?} → {token, ...} (once)
 * DELETE /api/v1/auth/tokens/{id}
 * PATCH  /api/v1/auth/tokens/{id}       {name?, expires_days?}
 *
 * GET    /api/v1/auth/sessions          active sessions (current has is_current=true)
 * DELETE /api/v1/auth/sessions/{id}
 * POST   /api/v1/auth/sessions/sign-out-others
 */

import { get, post, del, patch, request } from './client'

// ── Password / session flow ─────────────────────────────────────────────

export const login = (username, password) =>
  post('/api/v1/auth/login', { username, password })

export const logout = () =>
  request('/api/v1/auth/logout', { method: 'POST' })

export const changePassword = (oldPassword, newPassword) =>
  post('/api/v1/auth/change-password', {
    old_password: oldPassword,
    new_password: newPassword,
  })

export const getMe = () => get('/api/v1/auth/me')

// ── Tokens ──────────────────────────────────────────────────────────────

export const listTokens = () => get('/api/v1/auth/tokens')

export const createToken = (name, expiresDays = null) =>
  post('/api/v1/auth/tokens', { name, expires_days: expiresDays })

export const revokeToken = (tokenId) =>
  del(`/api/v1/auth/tokens/${tokenId}`)

export const patchToken = (tokenId, updates) =>
  patch(`/api/v1/auth/tokens/${tokenId}`, updates)

// ── Sessions ────────────────────────────────────────────────────────────

export const listSessions = () => get('/api/v1/auth/sessions')

export const revokeSession = (sessionId) =>
  del(`/api/v1/auth/sessions/${sessionId}`)

export const signOutOtherSessions = () =>
  post('/api/v1/auth/sessions/sign-out-others')
