/**
 * Chunks & Blocks API — 独立访问 + 搜索 + 邻居 + 图片
 *
 * GET /api/v1/chunks/{id}                  单个 chunk 详情
 * GET /api/v1/chunks/{id}/neighbors        上下文邻居 chunks
 * GET /api/v1/chunks/search?q=...          全局 BM25 关键词搜索
 * GET /api/v1/chunks/by-node/{nodeId}      按树节点查 chunks
 * GET /api/v1/blocks/{id}                  单个 block 详情
 * GET /api/v1/blocks/{id}/image            获取 block 的提取图片
 * GET /api/v1/blocks/by-page/{docId}/{pg}  获取某页所有 blocks
 */

import { get } from './client'

// ===================== Chunks =====================

/**
 * 获取单个 chunk
 * @param {string} chunkId
 * @returns {Promise<{
 *   chunk_id: string, doc_id: string, parse_version: number,
 *   node_id: string, content: string,
 *   content_type: 'text'|'table'|'image'|'formula'|'code'|'mixed',
 *   block_ids: string[], page_start: number, page_end: number,
 *   token_count: number, section_path: string[],
 *   ancestor_node_ids: string[], cross_ref_chunk_ids: string[]
 * }>}
 */
export const getChunk = (chunkId) => get(`/api/v1/chunks/${chunkId}`)

/**
 * 获取 chunk 的上下文邻居 (同文档,按 chunk_id 顺序)
 * @param {string} chunkId
 * @param {object} [params]
 * @param {number} [params.before=2] - 前面几个
 * @param {number} [params.after=2]  - 后面几个
 * @returns {Promise<{
 *   target_index: number,    // 目标 chunk 在返回数组中的位置
 *   chunks: ChunkOut[]
 * }>}
 */
export const getChunkNeighbors = (chunkId, params = {}) =>
  get(`/api/v1/chunks/${chunkId}/neighbors`, { before: 2, after: 2, ...params })

/**
 * 全局 BM25 关键词搜索 (跨所有文档)
 * @param {string} query   - 搜索关键词
 * @param {number} [topK=20]
 * @returns {Promise<{
 *   items: Array<{
 *     chunk_id: string, score: number, doc_id: string,
 *     node_id: string, content_type: string, page_start: number,
 *     section_path: string[], snippet: string
 *   }>,
 *   total: number
 * }>}
 */
export const searchChunks = (query, topK = 20) =>
  get('/api/v1/chunks/search', { q: query, top_k: topK })

/**
 * 按树节点获取 chunks
 * @param {string} nodeId
 * @returns {Promise<ChunkOut[]>}
 */
export const getChunksByNode = (nodeId) =>
  get(`/api/v1/chunks/by-node/${nodeId}`)

/**
 * 根据 block_id 反查所属 chunk (单个文档范围内)
 * @param {string} blockId
 * @param {string} docId
 * @returns {Promise<{ chunk: ChunkOut, position: number }>}
 */
export const getChunkByBlock = (blockId, docId) =>
  get(`/api/v1/chunks/by-block/${blockId}`, { doc_id: docId })

// ===================== Blocks =====================

/**
 * 获取单个 block
 * @param {string} blockId
 * @returns {Promise<{
 *   block_id: string, doc_id: string, parse_version: number,
 *   page_no: number, seq: number,
 *   bbox: { x0: number, y0: number, x1: number, y1: number },
 *   type: 'heading'|'paragraph'|'list'|'table'|'image'|'formula'|'code'|'caption'|'header'|'footer',
 *   level: number|null, text: string, confidence: number,
 *   table_html: string|null, table_markdown: string|null,
 *   image_storage_key: string|null, image_caption: string|null,
 *   formula_latex: string|null, code_text: string|null, code_language: string|null,
 *   excluded: boolean,
 *   excluded_reason: string|null, caption_of: string|null,
 *   cross_ref_targets: string[]
 * }>}
 */
export const getBlock = (blockId) => get(`/api/v1/blocks/${blockId}`)

/**
 * 获取 block 的提取图片 URL (用于 <img src>)
 * 仅 type=image 且 image_storage_key 非空的 block 有图片
 * @param {string} blockId
 * @returns {string} 图片 URL
 */
export const blockImageUrl = (blockId) =>
  `${import.meta.env.VITE_API_BASE || ''}/api/v1/blocks/${blockId}/image`

/**
 * 获取某页的所有 blocks (PDF viewer 叠加层用)
 * @param {string} docId
 * @param {number} pageNo - 1-based 页码
 * @returns {Promise<{
 *   doc_id: string,
 *   page_no: number,
 *   blocks: BlockOut[]    // 每个含 bbox, type, text 等
 * }>}
 */
export const getBlocksByPage = (docId, pageNo) =>
  get(`/api/v1/blocks/by-page/${docId}/${pageNo}`)
