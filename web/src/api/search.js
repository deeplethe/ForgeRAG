/**
 * Search API — 统一检索原语 (无 LLM 答案)
 *
 * POST /api/v1/search   { query, include?, limit?, filter?, path_filter?, overrides? }
 *
 * 跟 /query 的关系：
 *   /query   = retrieval + LLM 合成 + 引用    (Chat 用)
 *   /search  = retrieval 本体, 不调 LLM       (Search 页 / agent / debug)
 *
 * 响应：
 *   { chunks: ScoredChunkOut[], files?: FileHitOut[], stats: object }
 *
 * include 控制返回哪些视图：
 *   ["chunks"]        默认；只返回 chunk 排名
 *   ["files"]         只返回文件级聚合
 *   ["chunks","files"] 一次拿两份（一次往返就够了）
 */

import { request } from './client'

/**
 * @typedef {Object} ScoredChunkOut
 * @property {string} chunk_id
 * @property {string} doc_id
 * @property {string} filename
 * @property {string} path
 * @property {number} page_no
 * @property {string} snippet
 * @property {number} score
 * @property {boolean} boosted_by_filename
 */

/**
 * @typedef {Object} ChunkMatchOut
 * @property {string} chunk_id
 * @property {string} snippet
 * @property {number} page_no
 * @property {number} score
 */

/**
 * @typedef {Object} FileHitOut
 * @property {string} doc_id
 * @property {string} filename
 * @property {string} path
 * @property {string} format
 * @property {number} score
 * @property {string[]} matched_in   - subset of {"filename", "content"}
 * @property {ChunkMatchOut|null} best_chunk
 * @property {string[]|null} filename_tokens
 */

/**
 * Run unified search.
 *
 * @param {Object} params
 * @param {string} params.query
 * @param {string[]} [params.include=['chunks']]
 * @param {{chunks?: number, files?: number}} [params.limit]
 * @param {Object} [params.filter]
 * @param {string} [params.pathFilter]
 * @param {Object} [params.overrides]   - QueryOverrides shape
 * @returns {Promise<{
 *   query: string,
 *   chunks: ScoredChunkOut[],
 *   files: FileHitOut[]|null,
 *   stats: object
 * }>}
 */
export const search = ({
  query,
  include = ['chunks'],
  limit = null,
  filter = null,
  pathFilter = null,
  overrides = null,
}) =>
  request('/api/v1/search', {
    method: 'POST',
    body: {
      query,
      include,
      limit: limit || null,
      filter,
      path_filter: pathFilter,
      overrides,
    },
  })
