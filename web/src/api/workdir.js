/**
 * Workdir file API — folder-as-cwd.
 *
 * Talks to /api/v1/workdir/* (not /api/v1/projects/*; projects is
 * legacy, see workdir_files.py). Each user has a private workdir
 * tree; chat ``cwd_path`` is a subpath within it.
 *
 * Endpoints used here:
 *   GET    /api/v1/workdir/info             { user_id, root_path }
 *   GET    /api/v1/workdir/files?path=…     [{ path, name, is_dir, size_bytes, modified_at }, …]
 *   POST   /api/v1/workdir/folders          { path }
 *   POST   /api/v1/workdir/upload           multipart
 *   GET    /api/v1/workdir/download?path=…  binary stream
 */

import { get, post, request } from './client'

/**
 * Fetch the caller's workdir info (auto-creates the dir on first hit).
 * @returns {Promise<{ user_id: string, root_path: string }>}
 */
export function getWorkdirInfo() {
  return get('/api/v1/workdir/info')
}

/**
 * List the contents of a folder. Empty path = root.
 *
 * @param {string} [path='']
 * @returns {Promise<Array<{ path: string, name: string, is_dir: boolean, size_bytes: number, modified_at: string }>>}
 */
export function listWorkdir(path = '') {
  const qs = path ? `?path=${encodeURIComponent(path)}` : ''
  return get(`/api/v1/workdir/files${qs}`)
}

/**
 * Create a folder. Idempotent — re-creating an existing folder
 * returns its current entry, no 409.
 *
 * @param {string} path
 */
export function makeWorkdirFolder(path) {
  return post('/api/v1/workdir/folders', { path })
}

/**
 * Upload a file into a folder.
 *
 * @param {string} path - destination FOLDER (not the full file path)
 * @param {File}   file
 */
export async function uploadWorkdirFile(path, file) {
  const fd = new FormData()
  fd.append('path', path)
  fd.append('file', file)
  // post() helper sets JSON content-type for object bodies; for
  // FormData we use request() with FormData, which the client.js
  // detects and sends as multipart.
  return request('/api/v1/workdir/upload', { method: 'POST', body: fd })
}

/**
 * Build a download URL for a workdir file. The Workbench UI uses
 * this for "open file" / "download" affordances; agent-produced
 * artifacts in chat trace also link via this URL.
 *
 * Returns a URL string (not a Promise) — the caller drops it into
 * an <a href>, an <iframe src>, or window.open. The route streams
 * with Content-Disposition: attachment so the browser saves rather
 * than navigating away.
 *
 * @param {string} path
 * @returns {string}
 */
export function workdirDownloadUrl(path) {
  const base = import.meta.env.VITE_API_BASE || ''
  return `${base}/api/v1/workdir/download?path=${encodeURIComponent(path)}`
}

/**
 * Build an INLINE preview URL for a workdir file. Same endpoint as
 * the download URL but with ``inline=1`` so the response carries a
 * mime type derived from the extension and ``Content-Disposition:
 * inline``. Lets ``<img>`` / ``<video>`` / ``<iframe>`` consume the
 * bytes directly. Used by the Workbench's file preview modal.
 *
 * @param {string} path
 * @returns {string}
 */
export function workdirPreviewUrl(path) {
  const base = import.meta.env.VITE_API_BASE || ''
  return `${base}/api/v1/workdir/download?path=${encodeURIComponent(path)}&inline=1`
}
