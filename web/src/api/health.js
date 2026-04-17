/**
 * Health & System API
 *
 * GET  /api/v1/health                    系统健康检查
 * GET  /api/v1/system/stats              全局统计
 * GET  /api/v1/system/retrieval-status   检索路径开关状态
 * POST /api/v1/system/rebuild-bm25       手动重建 BM25 索引
 * POST /api/v1/system/test-connection    测试 LLM/Embedder 连接
 */

import { get, post } from './client'

/**
 * 系统健康检查
 * @returns {Promise<{
 *   status: string,
 *   version: string,
 *   components: { relational: string, vector: string, blob: string, embedder: string },
 *   counts: { documents: number, files: number }
 * }>}
 */
export const getHealth = () => get('/api/v1/health')

/**
 * 全局统计(文档数、文件数、chunk 数、trace 数等)
 * @returns {Promise<{
 *   documents: number,
 *   files: number,
 *   chunks: number,
 *   traces: number,
 *   settings: number,
 *   bm25_indexed: number
 * }>}
 */
export const getStats = () => get('/api/v1/system/stats')

/**
 * 检索路径开关状态一览
 * @returns {Promise<{
 *   vector_enabled: boolean,
 *   bm25_enabled: boolean,
 *   tree_enabled: boolean,
 *   tree_llm_nav_enabled: boolean,
 *   query_understanding_enabled: boolean,
 *   rerank_enabled: boolean,
 *   descendant_expansion_enabled: boolean,
 *   sibling_expansion_enabled: boolean,
 *   crossref_expansion_enabled: boolean
 * }>}
 */
export const getRetrievalStatus = () => get('/api/v1/system/retrieval-status')

/**
 * 手动重建 BM25 索引
 * @returns {Promise<{ status: string, chunks_indexed: number, duration_ms: number }>}
 */
export const rebuildBM25 = () => post('/api/v1/system/rebuild-bm25')

/**
 * 测试 LLM / Embedder / Tree Nav 连接
 * @param {'embedder'|'generator'|'tree_nav'} target - 要测试的目标
 * @returns {Promise<{
 *   target: string,
 *   success: boolean,
 *   latency_ms: number,
 *   detail: string
 * }>}
 */
export const testConnection = (target) =>
  post('/api/v1/system/test-connection', { target })

/**
 * 基础设施信息 (yaml 配置的只读值)
 * @returns {Promise<{
 *   storage_mode: string, storage_root: string,
 *   relational_backend: string, relational_path: string,
 *   vector_backend: string, vector_detail: string
 * }>}
 */
export const getInfrastructure = () => get('/api/v1/system/infrastructure')

/**
 * 组件健康快照（各 pipeline 组件最近一次调用状态）
 * @returns {Promise<{ components: Object.<string, {
 *   status: 'healthy'|'degraded'|'error'|'disabled'|'unknown',
 *   last_ok_ts?: number, last_error_ts?: number,
 *   last_error_type?: string, last_error_msg?: string,
 *   last_latency_ms?: number, total_ok?: number, total_err?: number,
 *   extra?: object
 * }> }>}
 */
export const getComponentHealth = () => get('/api/v1/health/components')
