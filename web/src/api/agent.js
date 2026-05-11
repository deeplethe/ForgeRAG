/**
 * Agent chat — SSE-streamed agentic chat against
 * POST /api/v1/agent/chat (Claude Agent SDK, in-process).
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
 * @param {string[]}      [params.attachmentIds]  draft attachments to bind
 * @param {string[]}      [params.pathFilters]    pinned knowledge scopes
 * @param {AbortSignal}   [params.signal]
 * @yields {{ type: string, [k: string]: any }}
 */
export async function* agentChatStream({
  message,
  conversationId = null,
  cwdPath = null,
  attachmentIds = null,
  pathFilters = null,
  signal,
}) {
  const BASE = import.meta.env.VITE_API_BASE || ''
  const body = {
    query: message,
    conversation_id: conversationId,
  }
  // Folder-as-cwd: the agent's working directory for this turn.
  // Backend resolves order: body.cwd_path > Conversation.cwd_path
  // > none (pure Q&A at /workdir root). Sending it on every turn
  // also handles the "switch folder" gesture — backend updates
  // the Conversation row when this differs from what's stored.
  if (cwdPath) body.cwd_path = cwdPath
  // Draft attachment ids — backend binds them to this turn's user
  // message after persistence and feeds their content to the agent
  // (text inlined into the prompt; image/pdf as native multimodal
  // content blocks if the configured model supports them).
  if (Array.isArray(attachmentIds) && attachmentIds.length > 0) {
    body.attachment_ids = attachmentIds
  }
  // Pinned knowledge scopes (chip-rail entries). The backend
  // persists this on the Conversation row and prepends a hint to
  // the user message naming each path so the agent fans out
  // ``search_vector(query, path_filter=…)`` calls per pin.
  // ``null`` means "no change"; the backend falls back to whatever
  // it stored on the Conversation. Send only when the local rail
  // is the truth (i.e. user just sent / reopened the chat).
  if (Array.isArray(pathFilters)) {
    body.path_filters = pathFilters
  }

  const res = await fetch(`${BASE}/api/v1/agent/chat`, {
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


// ============================================================================
// Long-task / HITL client (Inc 3-5) — POST /send + GET /stream + /feedback
// ============================================================================
//
// Different shape from the legacy /chat above:
//
//   1. POST /conversations/{id}/send   → returns { run_id, started_at }.
//      The HTTP response closes immediately; the agent runs in the
//      background on the server.
//
//   2. GET  /conversations/{id}/stream?since=N  →  SSE; replays events
//      with seq>N from buffer/DB then tails live. Reconnect on disconnect
//      with the highest seq seen.
//
//   3. POST /conversations/{id}/feedback  →  HITL channel:
//      interrupt / approve / deny / answer / message.
//
// Wire event shape (single ``data: {json}`` block per event):
//
//   { seq: number,
//     type: "phase" | "thought" | "token" | "tool_start" | "tool_end" |
//           "citation" | "approval_request" | "ask_human" |
//           "sub_agent_start" | "sub_agent_done" | "usage" |
//           "budget_warning" | "interrupted" | "error" | "done" |
//           "stream_end",
//     run_id: string,
//     conversation_id: string,
//     depth: number,           // 0 = main, 1+ = sub-agent
//     ts: ISO8601,
//     payload: { ... shape per type ... }
//   }


/**
 * Start an agent turn in the background. Returns ``run_id`` so the
 * caller can subscribe to events via ``agentStream``.
 *
 * 409 Conflict if the conversation already has an active run.
 *
 * @param {object}        params
 * @param {string}        params.conversationId  REQUIRED (route param).
 * @param {string}        params.message
 * @param {string|null}   [params.cwdPath]
 * @param {string[]}      [params.attachmentIds]
 * @param {string[]}      [params.pathFilters]
 * @returns {Promise<{run_id: string, started_at: number}>}
 */
export async function agentSendTurn({
  conversationId,
  message,
  cwdPath = null,
  attachmentIds = null,
  pathFilters = null,
}) {
  if (!conversationId) throw new Error('conversationId is required')
  const BASE = import.meta.env.VITE_API_BASE || ''
  const body = { query: message, conversation_id: conversationId }
  if (cwdPath) body.cwd_path = cwdPath
  if (Array.isArray(attachmentIds) && attachmentIds.length > 0) {
    body.attachment_ids = attachmentIds
  }
  if (Array.isArray(pathFilters)) body.path_filters = pathFilters

  const res = await fetch(
    `${BASE}/api/v1/agent/conversations/${encodeURIComponent(conversationId)}/send`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body),
    },
  )
  if (!res.ok) {
    let detail = res.statusText
    try {
      const err = await res.json()
      detail = err.detail || JSON.stringify(err)
    } catch {}
    throw new Error(`${res.status}: ${detail}`)
  }
  return res.json()
}


/**
 * Subscribe to events for a conversation's active run (or replay a
 * completed one from DB). The ``since`` cursor is the highest seq the
 * client has already processed — server fills the gap and tails live.
 *
 * Yields events. Terminates when the server sends an event with
 * ``type`` in {"done","interrupted","error","stream_end"}, OR when
 * the underlying fetch errors, OR when ``signal`` aborts.
 *
 * Reconnect is the caller's responsibility — wrap this with a loop
 * that resumes ``agentStream({since: lastSeq + 0})`` on transient
 * errors. See ``useAgentStream`` composable for the canonical pattern.
 *
 * @param {object}      params
 * @param {string}      params.conversationId
 * @param {number}      [params.since=-1]   Highest seq seen, or -1 for all.
 * @param {AbortSignal} [params.signal]
 * @yields {{seq:number,type:string,run_id:string,payload:object,...}}
 */
export async function* agentStream({ conversationId, since = -1, signal }) {
  if (!conversationId) throw new Error('conversationId is required')
  const BASE = import.meta.env.VITE_API_BASE || ''
  const url =
    `${BASE}/api/v1/agent/conversations/${encodeURIComponent(conversationId)}/stream` +
    `?since=${encodeURIComponent(since)}`

  const res = await fetch(url, {
    method: 'GET',
    credentials: 'include',
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

    const parts = buffer.split('\n\n')
    buffer = parts.pop()

    for (const block of parts) {
      const trimmed = block.trim()
      if (!trimmed) continue
      // Skip SSE comments (": keepalive" etc.)
      if (trimmed.startsWith(':')) continue
      const dataLine = trimmed
        .split('\n')
        .find((l) => l.startsWith('data:'))
      if (!dataLine) continue
      const payload = dataLine.slice(5).trim()
      try {
        const ev = JSON.parse(payload)
        yield ev
        if (
          ev.type === 'done' ||
          ev.type === 'interrupted' ||
          ev.type === 'error' ||
          ev.type === 'stream_end'
        ) {
          return
        }
      } catch {
        // Malformed JSON — skip, don't kill the stream
      }
    }
  }
}


/**
 * Deliver HITL feedback to the conversation's active run.
 *
 * @param {object} params
 * @param {string} params.conversationId
 * @param {'interrupt'|'approve'|'deny'|'answer'|'message'} params.type
 * @param {string} [params.approvalId]    Required for 'approve' / 'deny'.
 * @param {string} [params.questionId]    Required for 'answer'.
 * @param {string} [params.message]       Free-text for 'deny','answer','message'.
 * @param {object} [params.modifiedInput] Optional override for approved tool input.
 * @returns {Promise<{ok:boolean, run_id:string, delivered_type:string}>}
 */
export async function sendAgentFeedback({
  conversationId,
  type,
  approvalId = null,
  questionId = null,
  message = null,
  modifiedInput = null,
}) {
  if (!conversationId) throw new Error('conversationId is required')
  const BASE = import.meta.env.VITE_API_BASE || ''
  const body = { type }
  if (approvalId) body.approval_id = approvalId
  if (questionId) body.question_id = questionId
  if (message != null) body.message = message
  if (modifiedInput != null) body.modified_input = modifiedInput

  const res = await fetch(
    `${BASE}/api/v1/agent/conversations/${encodeURIComponent(conversationId)}/feedback`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body),
    },
  )
  if (!res.ok) {
    let detail = res.statusText
    try {
      const err = await res.json()
      detail = err.detail || JSON.stringify(err)
    } catch {}
    throw new Error(`${res.status}: ${detail}`)
  }
  return res.json()
}


/**
 * Compat-mapping helper: translate new-format events
 * (``{seq, type, payload}``) into the old wire vocabulary the legacy
 * Chat.vue reducer already understands (``agent.turn_start`` /
 * ``agent.thought`` / ``tool.call_start`` / ``tool.call_end`` /
 * ``answer.delta`` / ``agent.turn_end`` / ``done`` / etc.).
 *
 * Lets the rest of the UI keep its event-shape contract while we
 * roll out the disconnect-survival routes. Returns an array of
 * legacy events (often 0, 1, or 2 — done emits both turn_end AND done).
 *
 * New-format events without a legacy equivalent (ask_human,
 * approval_request, sub_agent_start/done, budget_warning, interrupted)
 * return ``null`` here — callers route them to dedicated handlers
 * (HITL cards / sub-agent UI / etc.).
 */
function _newEventToLegacy(ev, ctx) {
  const t = ev.type
  const p = ev.payload || {}
  const runId = ev.run_id
  const out = []

  // Emit synthetic turn_start once, on the first real event.
  if (!ctx.turnStarted && t !== 'done' && t !== 'error' && t !== 'interrupted'
      && t !== 'stream_end' && t !== '_send_meta') {
    ctx.turnStarted = true
    out.push({ type: 'agent.turn_start', turn: 1, run_id: runId })
  }

  if (t === 'phase') {
    // Phase events have no 1:1 legacy mapping; the old reducer
    // inferred phase implicitly from turn_start. Skip — emitting a
    // synthetic agent.thought with no text would create an empty
    // chain entry.
    return out
  }
  if (t === 'thought') {
    out.push({ type: 'agent.thought', text: p.text || '' })
    return out
  }
  if (t === 'token') {
    out.push({ type: 'answer.delta', text: p.delta || '' })
    return out
  }
  if (t === 'tool_start') {
    out.push({
      type: 'tool.call_start',
      id: p.call_id,
      tool: p.tool,
      params: (p.input && typeof p.input === 'object') ? p.input : {},
    })
    return out
  }
  if (t === 'tool_end') {
    out.push({
      type: 'tool.call_end',
      id: p.call_id,
      latency_ms: p.latency_ms || 0,
      is_error: !!p.is_error,
      result_summary: p.result_summary || {},
      output: p.output || '',
    })
    return out
  }
  if (t === 'citation') {
    out.push({ type: 'citations', items: p.items || [] })
    return out
  }
  if (t === 'usage') {
    out.push({
      type: 'usage',
      input_tokens: p.input_tokens || 0,
      output_tokens: p.output_tokens || 0,
    })
    return out
  }
  if (t === 'done') {
    out.push({ type: 'agent.turn_end', turn: 1, run_id: runId })
    out.push({
      type: 'done',
      stop_reason: p.stop_reason || 'end_turn',
      total_latency_ms: p.total_latency_ms || 0,
      final_text: p.final_text || '',
      run_id: runId,
      error: p.error,
      citations: p.citations || null,
    })
    return out
  }
  if (t === 'error') {
    out.push({ type: 'agent.turn_end', turn: 1, run_id: runId })
    out.push({
      type: 'done',
      stop_reason: 'error',
      error: p.message || 'agent error',
      run_id: runId,
    })
    return out
  }
  if (t === 'interrupted') {
    out.push({ type: 'agent.turn_end', turn: 1, run_id: runId })
    out.push({
      type: 'done',
      stop_reason: 'interrupted',
      error: p.reason ? `interrupted: ${p.reason}` : 'interrupted',
      run_id: runId,
    })
    return out
  }
  // Unknown / HITL-only types (ask_human, approval_request,
  // sub_agent_start, sub_agent_done, budget_warning, stream_end):
  // no legacy equivalent. Caller's HITL handler picks up the raw
  // event passed in alongside.
  return out
}


/**
 * Compat wrapper around ``agentSendAndStream``: yields events in the
 * LEGACY wire format Chat.vue's reducer already speaks, AND also
 * emits the raw new-format event (with ``_raw: true``) so callers can
 * handle HITL types (ask_human, approval_request, sub_agent_*,
 * budget_warning) without parsing.
 *
 * The first emitted event is always a meta object carrying ``run_id``
 * (so the caller can pin ``/feedback`` to the right run on
 * interrupt). Shape: ``{type: '_run_meta', run_id, seq: -1}``.
 *
 * Reconnect: pass the highest ``_raw_seq`` seen as ``since`` to
 * ``agentStream`` to resume.
 */
export async function* agentSendAndStreamCompat(params) {
  const meta = await agentSendTurn(params)
  yield { type: '_run_meta', run_id: meta.run_id, started_at: meta.started_at }

  const ctx = { turnStarted: false }
  for await (const ev of agentStream({
    conversationId: params.conversationId,
    since: -1,
    signal: params.signal,
  })) {
    // Always pass the raw event for HITL / sub-agent / budget handling.
    yield { _raw: true, _raw_seq: ev.seq, ...ev }
    // Then yield the translated legacy events for the existing reducer.
    const legacy = _newEventToLegacy(ev, ctx)
    for (const lev of legacy) yield lev
  }
}


/**
 * High-level helper: send a turn AND subscribe to its events in one
 * call. Equivalent to the legacy ``agentChatStream`` ergonomically
 * but uses the disconnect-survival /send + /stream pair under the
 * hood. Tracks the highest seq seen so callers can reconnect by
 * passing it as ``since`` on a fresh ``agentStream`` call.
 *
 * Reconnect after a disconnect mid-stream:
 *
 *   let lastSeq = -1
 *   try {
 *     for await (const ev of agentSendAndStream({...})) {
 *       lastSeq = ev.seq
 *       handle(ev)
 *     }
 *   } catch (e) {
 *     // Network blip — resume from where we left off
 *     for await (const ev of agentStream({conversationId, since: lastSeq})) {
 *       ...
 *     }
 *   }
 */
export async function* agentSendAndStream(params) {
  const { run_id } = await agentSendTurn(params)
  // Subscribe from seq=-1 (replay nothing — we just kicked off the run
  // so there can't be events older than seq 0, but ``-1`` is the
  // documented "give me everything" value).
  yield { type: '_send_meta', run_id, seq: -1 }
  yield* agentStream({
    conversationId: params.conversationId,
    since: -1,
    signal: params.signal,
  })
}
