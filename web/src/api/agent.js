/**
 * Agent chat — SSE-streamed agentic chat.
 *
 * Two backends speak the same wire format and can be toggled
 * via the `VITE_AGENT_BACKEND` env var:
 *
 *   - 'legacy'  → POST /api/v1/agent/chat       (handcrafted loop.py)
 *   - 'hermes'  → POST /api/v1/agent/hermes-chat (Hermes Agent runtime)
 *
 * Default is 'hermes' (B-MVP). Wave 3 will delete the legacy path
 * + this toggle once the Hermes route has bake time in production.
 *
 * Event vocabulary (each event is a `data: <json>\n\n` block, the
 * `type` field on the JSON dict is the discriminator — both routes
 * emit identical envelopes):
 *
 *   { type: 'agent.turn_start', turn, run_id? }
 *   { type: 'agent.thought',    text }                  (Hermes-only, optional)
 *   { type: 'tool.call_start',  id, tool, params }
 *   { type: 'tool.call_end',    id, tool, latency_ms, result_summary }
 *   { type: 'answer.delta',     text }                  (token stream)
 *   { type: 'agent.turn_end',   turn, run_id? }
 *   { type: 'done',             stop_reason, total_latency_ms,
 *                               final_text?, error?,
 *                               citations[]?, iterations?, ... }
 *
 * `done` is always the last event — clients should close on it.
 *
 * Conversation persistence: pass `conversationId` and the backend
 * loads prior turns from the messages table + writes the new turn
 * after the stream closes. No frontend-side addMessage needed.
 *
 * Body schema differs slightly between backends (legacy expects
 * `message`, Hermes expects `query` + optional `model`); we
 * normalise client-side so callers pass `message` to either.
 */

const AGENT_BACKEND =
  (import.meta.env.VITE_AGENT_BACKEND || 'hermes').toLowerCase()

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

  // Pick the backend route + body shape based on the build-time
  // toggle. The Hermes route's body uses `query` instead of
  // `message` and doesn't need `path_filters` (path-filter authz
  // already runs server-side per request via the principal /
  // ToolContext, not via an explicit body field).
  const useHermes = AGENT_BACKEND === 'hermes'
  const url = useHermes
    ? `${BASE}/api/v1/agent/hermes-chat`
    : `${BASE}/api/v1/agent/chat`
  const body = useHermes
    ? { query: message, conversation_id: conversationId }
    : { message, conversation_id: conversationId, path_filters: pathFilters }

  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
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
