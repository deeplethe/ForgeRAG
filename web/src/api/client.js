/**
 * HTTP client wrapper.
 *
 * All API modules import `request` from here. Change the baseURL
 * in one place to point at a different backend.
 *
 * Features:
 *   - Automatic JSON content-type for non-FormData bodies
 *   - Centralized error handling (throws with detail message)
 *   - Base URL from env (VITE_API_BASE) or defaults to ''
 *     (same-origin, works when backend serves frontend)
 */

const BASE = import.meta.env.VITE_API_BASE || ''
/**
 * Generic fetch wrapper.
 * @param {string} path    - e.g. '/api/v1/health'
 * @param {object} options - fetch options (method, body, headers, ...)
 * @returns {Promise<any>} parsed JSON or raw Response for streams
 */
export async function request(path, options = {}) {
  const url = `${BASE}${path}`
  const headers = { ...(options.headers || {}) }

  // Auto-set JSON content-type unless it's FormData
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = headers['Content-Type'] || 'application/json'
    if (typeof options.body === 'object') {
      options.body = JSON.stringify(options.body)
    }
  }

  const res = await fetch(url, { ...options, headers })

  // For streaming responses, return raw Response
  if (options.stream) return res

  if (!res.ok) {
    let detail = res.statusText
    try {
      const err = await res.json()
      detail = err.detail || JSON.stringify(err)
    } catch {}
    throw new Error(`${res.status}: ${detail}`)
  }

  // 204 No Content
  if (res.status === 204) return null

  return res.json()
}

/**
 * Shorthand helpers.
 */
export const get = (path, params) => {
  if (!params) return request(path)
  // Strip null / undefined so they don't appear as "?key=undefined"
  const clean = Object.fromEntries(
    Object.entries(params).filter(([, v]) => v != null),
  )
  const qs = Object.keys(clean).length ? '?' + new URLSearchParams(clean).toString() : ''
  return request(`${path}${qs}`)
}

export const post = (path, body) =>
  request(path, { method: 'POST', body })

export const put = (path, body) =>
  request(path, { method: 'PUT', body })

export const patch = (path, body) =>
  request(path, { method: 'PATCH', body })

export const del = (path, params, options = {}) => {
  // Support optional query-string `params` and optional `body` via `options.body`
  let url = path
  if (params) {
    const clean = Object.fromEntries(
      Object.entries(params).filter(([, v]) => v != null),
    )
    if (Object.keys(clean).length) {
      url += '?' + new URLSearchParams(clean).toString()
    }
  }
  return request(url, { method: 'DELETE', ...options })
}
