/**
 * Agent chat — POST /api/v1/agent/chat (SSE)
 *
 * The post-cutover replacement for the old /api/v1/query route.
 * Returns a Server-Sent Events stream of the agent loop's
 * structured events so the UI can render tool calls live.
 *
 * Event vocabulary (each event is a `data: <json>\n\n` block,
 * the `type` field on the JSON dict is the discriminator — there
 * is no `event:` line, unlike the old query stream):
 *
 *   { type: 'agent.turn_start', turn, synthesis_only? }
 *   { type: 'tool.call_start',  id, tool, params }
 *   { type: 'tool.call_end',    id, tool, latency_ms, result_summary }
 *   { type: 'agent.turn_end',   turn, tools_called, decision }
 *   { type: 'answer',           text }
 *   { type: 'done',             stop_reason, citations[],
 *                               iterations, tool_calls_count,
 *                               total_latency_ms, tokens_in, tokens_out }
 *
 * `done` is always the last event — clients should close on it.
 *
 * Conversation persistence: pass `conversationId` and the backend
 * loads prior turns from the messages table + writes the new turn
 * after the stream closes. No frontend-side addMessage needed.
 */

/**
 * Stream agent chat events.
 *
 * @param {object}        params
 * @param {string}        params.message
 * @param {string|null}   [params.conversationId]
 * @param {string[]|null} [params.pathFilters]
 * @param {AbortSignal}   [params.signal]
 * @yields {{ type: string, [k: string]: any }}
 */
export async function* agentChatStream({
  message,
  conversationId = null,
  pathFilters = null,
  signal,
}) {
  const BASE = import.meta.env.VITE_API_BASE || ''
  const res = await fetch(`${BASE}/api/v1/agent/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({
      message,
      conversation_id: conversationId,
      path_filters: pathFilters,
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

    // SSE blocks separated by double-newline.
    const parts = buffer.split('\n\n')
    buffer = parts.pop() // keep incomplete tail

    for (const block of parts) {
      const trimmed = block.trim()
      if (!trimmed) continue
      const dataLine = trimmed.split('\n').find((l) => l.startsWith('data:'))
      if (!dataLine) continue
      const payload = dataLine.slice(5).trim()
      try {
        yield JSON.parse(payload)
      } catch {
        // Malformed event — skip rather than crash the stream
      }
    }
  }

  // Drain trailing buffer if the stream ended on a final event with
  // no closing \n\n (some proxies trim).
  if (buffer.trim().startsWith('data:')) {
    const payload = buffer.trim().slice(5).trim()
    try {
      yield JSON.parse(payload)
    } catch {}
  }
}
