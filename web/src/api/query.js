/**
 * Query API — 检索 + 生成 (支持普通和 SSE 流式)
 *
 * POST /api/v1/query   { query, filter?, conversation_id?, stream? }
 *
 * stream=false → 返回完整 JSON
 * stream=true  → 返回 text/event-stream (SSE)
 */

import { request } from './client'

/**
 * 普通查询 (等待完整响应)
 * @param {object} params
 * @param {string} params.query                - 用户问题
 * @param {object} [params.filter]             - 可选过滤 { doc_id, content_type, ... }
 * @param {string} [params.conversationId]     - 多轮对话 ID (不传=单轮)
 * @returns {Promise<{
 *   query: string,
 *   text: string,
 *   citations_used: CitationOut[],
 *   citations_all: CitationOut[],
 *   model: string,
 *   finish_reason: string,
 *   stats: object,
 *   trace: object|null
 * }>}
 *
 * CitationOut = {
 *   citation_id, doc_id, file_id, parse_version,
 *   page_no, highlights: [{page_no, bbox: [x0,y0,x1,y1]}],
 *   snippet, score, open_url
 * }
 */
export const askQuery = ({ query, filter, conversationId }) =>
  request('/api/v1/query', {
    method: 'POST',
    body: {
      query,
      filter: filter || null,
      conversation_id: conversationId || null,
      stream: false,
    },
  })

/**
 * 流式查询 (SSE)
 *
 * 返回一个 async generator,yield 多种事件:
 *   { event: 'progress',  data: { phase, status } }         // 阶段切换
 *   { event: 'retrieval', data: { vector_hits, bm25_hits, tree_hits, context_chunks, citations_all } }
 *   { event: 'thinking',  data: { text: '...' } }           // 推理模型 (V4-Pro / o1) 的内部思考
 *   { event: 'delta',     data: { text: '...' } }          // 逐 token 追加
 *   { event: 'done',      data: { text, citations_used, stats, finish_reason } }
 *
 * @param {object} params
 * @param {string} params.query
 * @param {object} [params.filter]
 * @param {string} [params.conversationId]
 * @param {AbortSignal} [params.signal]   - 用于取消请求
 * @yields {{ event: string, data: object }}
 *
 * @example
 * for await (const { event, data } of askQueryStream({ query: 'What is RAG?' })) {
 *   if (event === 'retrieval') showSourceCount(data.context_chunks)
 *   else if (event === 'delta') appendText(data.text)
 *   else if (event === 'done') showCitations(data.citations_used)
 * }
 */
export async function* askQueryStream({ query, filter, conversationId, signal, pathFilter, generationOverrides }) {
  const BASE = import.meta.env.VITE_API_BASE || ''
  // ``generationOverrides`` is the {reasoning_effort, temperature, max_tokens}
  // dict from the Tools popup. ``null`` / unset = fall through to yaml defaults.
  const res = await fetch(`${BASE}/api/v1/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query,
      filter: filter || null,
      path_filter: pathFilter || null,
      conversation_id: conversationId || null,
      stream: true,
      generation_overrides: generationOverrides || null,
    }),
    signal,
  })

  if (!res.ok) {
    let detail = res.statusText
    try {
      const err = await res.json()
      detail = err.detail || JSON.stringify(err)
    } catch {}
    throw new Error(`${res.status}: ${detail}`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // Split SSE events (double newline separated)
    const parts = buffer.split('\n\n')
    buffer = parts.pop() // keep incomplete tail

    for (const block of parts) {
      if (!block.trim()) continue
      const eventMatch = block.match(/^event:\s*(\w+)/m)
      const dataMatch = block.match(/^data:\s*(.+)$/m)
      if (!dataMatch) continue

      const event = eventMatch ? eventMatch[1] : 'delta'
      let data
      try {
        data = JSON.parse(dataMatch[1])
      } catch {
        data = { text: dataMatch[1] }
      }
      yield { event, data }
    }
  }

  // Flush remaining buffer
  if (buffer.trim()) {
    const eventMatch = buffer.match(/^event:\s*(\w+)/m)
    const dataMatch = buffer.match(/^data:\s*(.+)$/m)
    if (dataMatch) {
      const event = eventMatch ? eventMatch[1] : 'delta'
      try {
        yield { event, data: JSON.parse(dataMatch[1]) }
      } catch {}
    }
  }
}
