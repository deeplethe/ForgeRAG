/**
 * Agent chat — SSE-streamed agentic chat against
 * POST /api/v1/agent/hermes-chat (Hermes Agent runtime, in-process).
 *
 * Event vocabulary (each event is a `data: <json>\n\n` block, the
 * `type` field on the JSON dict is the discriminator):
 *
 *   { type: 'agent.turn_start', turn, run_id }
 *   { type: 'agent.thought',    text }              (zero or more)
 *   { type: 'tool.call_start',  id, tool, params }
 *   { type: 'tool.call_end',    id, tool, latency_ms, result_summary }
 *   { type: 'answer.delta',     text }              (token stream)
 *   { type: 'agent.turn_end',   turn, run_id }
 *   { type: 'done',             stop_reason, total_latency_ms,
 *                               final_text, run_id, error? }
 *
 * `done` is always the last event — clients should close on it.
 *
 * Conversation persistence: pass `conversationId` and the backend
 * loads prior turns from the messages table + writes the new turn
 * after the stream closes. No frontend-side addMessage needed.
 *
 * Path-filter authz scoping is resolved server-side from the
 * authenticated principal — no body field needed for it.
 */

/**
 * Stream agent chat events.
 *
 * @param {object}        params
 * @param {string}        params.message
 * @param {string|null}   [params.conversationId]
 * @param {AbortSignal}   [params.signal]
 * @yields {{ type: string, [k: string]: any }}
 */
export async function* agentChatStream({
  message,
  conversationId = null,
  signal,
}) {
  const BASE = import.meta.env.VITE_API_BASE || ''
  const res = await fetch(`${BASE}/api/v1/agent/hermes-chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({
      query: message,
      conversation_id: conversationId,
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
