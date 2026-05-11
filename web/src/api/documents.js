/**
 * Documents API — 入库、管理、子资源(blocks/chunks/tree)
 *
 * POST   /api/v1/documents                      入库 (传 file_id + 选项)
 * POST   /api/v1/documents/upload-and-ingest     一步到位 (multipart)
 * GET    /api/v1/documents                       文档列表 (分页+搜索)
 * GET    /api/v1/documents/{id}                  文档详情
 * DELETE /api/v1/documents/{id}                  删除 (级联 chunks/tree/blocks/向量)
 * POST   /api/v1/documents/{id}/reparse          重新解析
 * GET    /api/v1/documents/{id}/blocks            blocks 列表
 * GET    /api/v1/documents/{id}/chunks            chunks 列表
 * GET    /api/v1/documents/{id}/tree              完整目录树
 * GET    /api/v1/documents/{id}/tree/{nodeId}     单个树节点
 */

import { get, del, request } from './client'

/**
 * 入库一个已上传的文件
 * @param {object} params
 * @param {string} params.fileId              - 已上传文件的 file_id
 * @param {string} [params.docId]             - 自定义 doc_id,不传则自动生成
 * @param {number} [params.parseVersion=1]
 * @param {boolean|null} [params.enrichSummary] - 显式控制是否做 LLM 摘要 (null=自动)
 * @param {boolean} [params.forceReparse=false] - 自动 bump parse_version 重新解析
 * @returns {Promise<{
 *   file_id: string, doc_id: string, status: string, message: string
 * }>}
 */
export const ingestDocument = ({
  fileId, docId, parseVersion = 1,
  enrichSummary = null, forceReparse = false,
}) =>
  request('/api/v1/documents', {
    method: 'POST',
    body: {
      file_id: fileId,
      doc_id: docId || null,
      parse_version: parseVersion,
      enrich_summary: enrichSummary,
      force_reparse: forceReparse,
    },
  })

/**
 * 一步到位:上传 + 入库 (multipart)
 * @param {File} file
 * @param {object} [options]
 * @param {string} [options.docId]
 * @param {string} [options.folderPath] 目标文件夹路径 (如 '/legal/2024')，默认 '/'
 * @returns {Promise<{ file_id: string, doc_id: string, status: string, message: string }>}
 */
export function uploadAndIngest(file, options = {}) {
  const form = new FormData()
  form.append('file', file)
  if (options.docId) form.append('doc_id', options.docId)
  if (options.folderPath) form.append('folder_path', options.folderPath)
  return request('/api/v1/documents/upload-and-ingest', {
    method: 'POST',
    body: form,
  })
}

/**
 * Fetch a URL into the file store + queue ingestion in one shot.
 * Mirror of ``uploadAndIngest`` for the URL-source case — server-side
 * fetch is SSRF-protected (private IPs blocked unless opted in).
 *
 * @param {string} url        http/https/s3/oss URL
 * @param {object} [options]
 * @param {string} [options.originalName] override the auto-detected filename
 * @param {string} [options.mimeType]     override the auto-detected MIME
 * @param {string} [options.folderPath]   destination folder (default '/')
 * @param {string} [options.docId]        override the auto-generated doc_id
 * @returns {Promise<{ file_id: string, doc_id: string, status: string, message: string }>}
 */
export function ingestFromUrl(url, options = {}) {
  const body = { url }
  if (options.originalName) body.original_name = options.originalName
  if (options.mimeType) body.mime_type = options.mimeType
  if (options.folderPath) body.folder_path = options.folderPath
  if (options.docId) body.doc_id = options.docId
  return request('/api/v1/documents/upload-url-and-ingest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

/**
 * 文档列表
 * @param {object} [params]
 * @param {number} [params.limit=50]           分页大小 (≤ 200)
 * @param {number} [params.offset=0]
 * @param {string} [params.search]             按 filename / doc_id 模糊搜索
 * @param {string} [params.status]             按 status 过滤，逗号分隔可多值
 * @param {string} [params.path_filter]        folder 路径过滤，如 '/legal/2024'
 * @param {boolean} [params.recursive=true]    path_filter 生效时：true=子树，false=仅直接子项
 * @returns {Promise<{
 *   items: DocumentOut[],
 *   total: number, limit: number, offset: number
 * }>}
 */
export const listDocuments = (params = {}) =>
  get('/api/v1/documents', { limit: 50, offset: 0, ...params })

/**
 * 文档详情 (含 doc_profile_json, parse_trace_json, num_blocks, num_chunks, file_name)
 * @param {string} docId
 * @returns {Promise<DocumentOut>}
 */
export const getDocument = (docId) => get(`/api/v1/documents/${docId}`)

/**
 * 批量按 doc_id 查文档（一次往返）。漏掉的 id 会从返回中静默剔除。
 *
 * 用途：聊天页引用卡片需要按 doc_id 把当前 filename 拉出来，对一条
 * 对话里若干 citations 来说，N 次 GET /documents/{id} 太傻；这里
 * 一个 IN(...) 解决。
 *
 * @param {string[]} docIds  最多 200 个；前端不要传过大列表
 * @returns {Promise<DocumentOut[]>}
 */
export const lookupDocuments = (docIds) =>
  request('/api/v1/documents/lookup', {
    method: 'POST',
    body: { doc_ids: Array.from(docIds || []) },
  })

/**
 * 删除文档 (级联删 blocks/chunks/tree + 清理向量库)
 * @param {string} docId
 * @returns {Promise<null>}
 */
export const deleteDocument = (docId) => del(`/api/v1/documents/${docId}`)

/**
 * 重新解析 (自动 bump parse_version)
 * @param {string} docId
 * @param {object} [options]
 * @param {boolean|null} [options.enrichSummary]
 * @returns {Promise<{ file_id: string, doc_id: string, status: string, message: string }>}
 */
export const stopDocument = (docId) =>
  request(`/api/v1/documents/${docId}/stop`, { method: 'POST' })

export const reparseDocument = (docId, options = {}) =>
  request(`/api/v1/documents/${docId}/reparse?` + new URLSearchParams({
    ...(options.enrichSummary != null ? { enrich_summary: options.enrichSummary } : {}),
  }), { method: 'POST' })

/**
 * 该文档的 blocks (分页)
 * @param {string} docId
 * @param {object} [params] - { limit, offset }
 * @returns {Promise<{ items: BlockOut[], total, limit, offset }>}
 */
export const listBlocks = (docId, params = {}) =>
  get(`/api/v1/documents/${docId}/blocks`, { limit: 100, offset: 0, ...params })

/**
 * 该文档的 chunks (分页)
 * @param {string} docId
 * @param {object} [params] - { limit, offset }
 * @returns {Promise<{ items: ChunkOut[], total, limit, offset }>}
 */
export const listChunks = (docId, params = {}) =>
  get(`/api/v1/documents/${docId}/chunks`, { limit: 100, offset: 0, ...params })

/**
 * 完整目录树
 * @param {string} docId
 * @returns {Promise<{
 *   doc_id: string, parse_version: number, root_id: string,
 *   quality_score: number, generation_method: string,
 *   nodes: Record<string, TreeNodeOut>
 * }>}
 */
export const getTree = (docId) => get(`/api/v1/documents/${docId}/tree`)

/**
 * 单个树节点
 * @param {string} docId
 * @param {string} nodeId
 * @returns {Promise<TreeNodeOut>}
 */
export const getTreeNode = (docId, nodeId) =>
  get(`/api/v1/documents/${docId}/tree/${nodeId}`)
