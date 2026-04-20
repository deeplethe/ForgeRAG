/**
 * Folder & document-path API client.
 *
 * All endpoints live under /api/v1/folders and /api/v1/documents/.../path.
 *
 * Returned shapes match the FastAPI schemas in api/routes/folders.py.
 */

import { del, get, patch, post, request } from './client'

/** List all folders (flat). Trashed subtree is excluded unless opted in. */
export const listFolders = (params = {}) =>
  get('/api/v1/folders', params)

/** Tree view — returns `path` + N levels of children. */
export const getFolderTree = (path = '/', depth = 2, include_trashed = false) =>
  get('/api/v1/folders/tree', { path, depth, include_trashed })

/** Folder info for a specific path. */
export const getFolderInfo = (path) =>
  get('/api/v1/folders/info', { path })

/** Create a new folder under `parent_path` with `name`. */
export const createFolder = (parent_path, name) =>
  post('/api/v1/folders', { parent_path, name })

/** Rename a folder (identified by its current path). */
export const renameFolder = (path, new_name) =>
  patch('/api/v1/folders/rename', { path, new_name })

/** Move a folder to a different parent. */
export const moveFolder = (path, to_parent_path) =>
  post('/api/v1/folders/move', { path, to_parent_path })

/** Soft-delete a folder to /__trash__. */
export const deleteFolder = (path) =>
  del('/api/v1/folders', { path })

// ── Document path operations ────────────────────────────────────────

/** Move a single document to another folder (by path). */
export const moveDocument = (doc_id, to_path) =>
  request(`/api/v1/documents/${doc_id}/path`, {
    method: 'PATCH',
    body: { to_path },
  })

/** Move multiple documents at once. */
export const bulkMoveDocuments = (doc_ids, to_path) =>
  post('/api/v1/documents/bulk-move', { doc_ids, to_path })
