/**
 * Chat-message attachments API.
 *
 * Two-phase lifecycle (driven by the chat composer):
 *   1. ``uploadAttachment(convId, file)`` while composing — creates
 *      a Draft row (``message_id = NULL``); the chip rail above the
 *      input box renders these as the user picks files.
 *   2. The chat-send route (separate, in agent.js) takes the list
 *      of attachment_ids and binds them server-side. After bind,
 *      ``listAttachments(convId, {only_drafts: true})`` returns
 *      empty for that conv.
 *
 * Payload limits + MIME gating live on the backend; this module is
 * pure HTTP. Errors bubble up as Error from ``request`` so the
 * caller can show the server's ``detail`` message in a toast (e.g.
 * "The configured model does not accept images").
 */

import { request, post, del } from './client'

const BASE = '/api/v1'

/**
 * Upload a single file as a draft attachment on a conversation.
 *
 * @param {string} conversationId
 * @param {File} file - browser File object (from <input> or paste/drop)
 * @returns {Promise<AttachmentOut>}
 */
export function uploadAttachment(conversationId, file) {
  const fd = new FormData()
  fd.append('file', file, file.name)
  // FormData → browser sets multipart Content-Type with boundary
  // automatically. ``request`` knows to NOT JSON-encode when the
  // body is a FormData; see api/client.js.
  return request(`${BASE}/conversations/${conversationId}/attachments`, {
    method: 'POST',
    body: fd,
  })
}

/**
 * List attachments on a conversation. ``only_drafts=true`` filters
 * to the input row's chip rail (the user's still-staged uploads).
 *
 * @param {string} conversationId
 * @param {{ only_drafts?: boolean }} [opts]
 * @returns {Promise<AttachmentOut[]>}
 */
export function listAttachments(conversationId, opts = {}) {
  const q = opts.only_drafts ? '?only_drafts=true' : ''
  return request(`${BASE}/conversations/${conversationId}/attachments${q}`, {
    method: 'GET',
  })
}

/**
 * Delete an attachment (DB row + blob). Allowed for both draft and
 * bound attachments.
 *
 * @param {string} attachmentId
 * @returns {Promise<null>}
 */
export function deleteAttachment(attachmentId) {
  return del(`${BASE}/attachments/${attachmentId}`)
}

/**
 * Build the URL for an attachment's raw blob. Used by the preview
 * affordance (clicking a chip opens the file). Auth flows through
 * cookies / Authorization headers like every other authed GET.
 *
 * @param {string} attachmentId
 * @returns {string}
 */
export function attachmentBlobUrl(attachmentId) {
  return `${BASE}/attachments/${attachmentId}/blob`
}
