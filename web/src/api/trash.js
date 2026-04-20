/**
 * Trash API client.
 *
 * Trash is implemented as a special system folder (/__trash__). These
 * endpoints wrap soft-delete / restore / permanent delete flows.
 */

import { del, get, post } from './client'

export const listTrash = () =>
  get('/api/v1/trash')

export const getTrashStats = () =>
  get('/api/v1/trash/stats')

/** Restore selected docs and/or folders back to their original locations. */
export const restoreFromTrash = ({ doc_ids, folder_paths } = {}) =>
  post('/api/v1/trash/restore', { doc_ids, folder_paths })

/** Permanently delete selected items from trash (cascades to vector/KG/BM25). */
export const purgeTrashItems = ({ doc_ids, folder_paths } = {}) =>
  del('/api/v1/trash/items', null, { body: { doc_ids, folder_paths } })

/** Empty the trash entirely. */
export const emptyTrash = () =>
  del('/api/v1/trash')
