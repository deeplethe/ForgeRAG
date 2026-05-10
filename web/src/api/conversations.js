/**
 * Conversations API — 多轮对话管理
 *
 * GET    /api/v1/conversations                  会话列表
 * POST   /api/v1/conversations                  创建新会话
 * GET    /api/v1/conversations/{id}             会话详情
 * PATCH  /api/v1/conversations/{id}             修改标题
 * DELETE /api/v1/conversations/{id}             删除 (级联消息)
 * GET    /api/v1/conversations/{id}/messages    消息历史
 */

import { get, post, patch, del } from './client'

/**
 * 会话列表 (分页,按最后更新倒序)
 * @param {object} [params]
 * @param {number} [params.limit=50]
 * @param {number} [params.offset=0]
 * @returns {Promise<{
 *   items: Array<{
 *     conversation_id: string,
 *     title: string|null,
 *     created_at: string,
 *     updated_at: string,
 *     message_count: number
 *   }>,
 *   total: number, limit: number, offset: number
 * }>}
 */
export const listConversations = (params = {}) =>
  get('/api/v1/conversations', { limit: 50, offset: 0, ...params })

/**
 * 创建新会话
 * @param {string} [title] - 初始标题,不传则由第一条消息自动设置
 * @returns {Promise<ConversationOut>}
 */
export const createConversation = (title = null, project_id = null) =>
  post('/api/v1/conversations', { title, project_id })

/**
 * 获取会话详情 (含 message_count)
 * @param {string} conversationId
 * @returns {Promise<ConversationOut>}
 */
export const getConversation = (conversationId) =>
  get(`/api/v1/conversations/${conversationId}`)

/**
 * Patch a conversation. The backend accepts ``title`` and
 * ``is_favorite``; both are optional and PATCH semantics apply
 * (only forwarded fields get written, omitted fields stay
 * untouched).
 * @param {string} conversationId
 * @param {{ title?: string, is_favorite?: boolean }} updates
 * @returns {Promise<ConversationOut>}
 */
export const updateConversation = (conversationId, updates) =>
  patch(`/api/v1/conversations/${conversationId}`, updates)

/**
 * 删除会话 (级联删除所有消息)
 * @param {string} conversationId
 * @returns {Promise<null>}
 */
export const deleteConversation = (conversationId) =>
  del(`/api/v1/conversations/${conversationId}`)

/**
 * 获取会话的消息历史 (按时间升序)
 * @param {string} conversationId
 * @param {number} [limit=100]
 * @returns {Promise<Array<{
 *   message_id: string,
 *   conversation_id: string,
 *   role: 'user'|'assistant',
 *   content: string,
 *   trace_id: string|null,        // assistant 消息关联的 trace
 *   citations_json: string[]|null, // assistant 消息使用的 citation ids
 *   created_at: string
 * }>>}
 */
export const getMessages = (conversationId, limit = 100) =>
  get(`/api/v1/conversations/${conversationId}/messages`, { limit })

/**
 * 手动添加一条消息到会话 (用于预置问答等非流式场景)
 * @param {string} conversationId
 * @param {'user'|'assistant'} role
 * @param {string} content
 * @returns {Promise<MessageOut>}
 */
export const addMessage = (conversationId, role, content) =>
  post(`/api/v1/conversations/${conversationId}/messages`, { role, content })

/**
 * Mark a conversation as read for the current user. Sets
 * ``last_read_at = now()`` server-side; the sidebar's unread blue
 * dot clears when ``unread`` recomputes false on the next list
 * fetch. Idempotent — safe to call on every conv-open.
 * @param {string} conversationId
 * @returns {Promise<null>}
 */
export const markConversationRead = (conversationId) =>
  post(`/api/v1/conversations/${conversationId}/read`, {})
