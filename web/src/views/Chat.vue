<script>
// ── Module-level state: survives component unmount/remount ──
// When the user navigates to another tab and back, the component is
// destroyed and recreated, but these refs persist so the streaming
// progress and messages are still visible.
import { ref } from 'vue'

export default { name: 'ChatView' }

const _msgs = ref([])
const _streaming = ref(false)
const _streamText = ref('')
const _retInfo = ref(null)
const _abortCtrl = ref(null)
// Chronological trace of the in-flight turn — the "chain" the user
// sees while the agent thinks-then-acts-then-thinks-then-answers.
// Each entry is one of:
//   { kind: 'phase',   phase: 'planning'|'reviewing'|'composing',
//                      t0, elapsed, status: 'running'|'done' }
//     LLM is in flight; renders "🧠 Planning… 3s". Morphs into
//     'thought' once the model emits a content preface; otherwise
//     stays as a bare timer marker.
//   { kind: 'thought', phase, text, t0, elapsed, status }
//     Same as 'phase' but with the LLM's natural-language reasoning
//     attached. Comes from the new ``agent.thought`` SSE event.
//   { kind: 'tool',    call_id, name, detail, t0, t1, status, summary }
//     One tool dispatch. ``running`` while in flight, flips to
//     ``done`` on tool.call_end.
// On 'done' the trace is attached to the assistant message so the
// reasoning chain stays visible after the stream closes. Reloads
// from DB do NOT carry the trace — it's session-only state; users
// who reload see just the answer + citations (the trace would
// require a schema migration to persist).
const _streamTrace = ref([])
// Generation overrides set via the Tools popup. ``null`` = use yaml
// defaults; otherwise a {reasoning_effort?, temperature?} dict that
// gets posted to the API as ``generation_overrides``.
const _genTools = ref(null)
let _presetGenId = 0
let _streamGenId = 0
let _skipNextWatch = false
let _timer = null

function _startTimer() {
  if (_timer) return
  _timer = setInterval(() => {
    // Tick elapsed counters on every running entry in the trace —
    // keeps the "Planning… 3s" / "Searching… 1.2s" timers ticking
    // without re-emitting events. Integer seconds for thoughts,
    // ms for tools (matches their respective UI cadence).
    const now = Date.now()
    for (const e of _streamTrace.value) {
      if (e.status !== 'running') continue
      if (e.kind === 'tool') e.elapsedMs = now - (e.t0 || now)
      else e.elapsedSec = Math.floor((now - (e.t0 || now)) / 1000)
    }
  }, 200)
}
function _stopTimer() { if (_timer) { clearInterval(_timer); _timer = null } }
</script>

<script setup>
import { ref, reactive, nextTick, computed, inject, watch, onMounted, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { agentChatStream, createConversation, getMessages, filePreviewUrl, fileDownloadUrl, getProject, getTrace } from '@/api'
import { FolderKanban as FolderKanbanIcon, X as XIcon } from 'lucide-vue-next'

const { t } = useI18n()
import { renderMarkdown } from '@/utils/renderMarkdown'
import { useDocCache } from '@/composables/useDocCache'
// Lazy-loaded so pdfjs-dist (~MB) is only fetched when the user actually
// clicks a citation. Without this the whole library + worker JS sit in
// the Chat.vue bundle, slowing the first chat load AND making the panel
// slide-in animation stutter when pdfjs-dist's main-thread init runs in
// parallel with the CSS transition.
const PdfViewer = defineAsyncComponent(() => import('@/components/PdfViewer.vue'))
import Spinner from '@/components/Spinner.vue'
import ThinkingPulse from '@/components/ThinkingPulse.vue'
import OtelTraceViewer from '@/components/OtelTraceViewer.vue'
import PathScopePicker from '@/components/PathScopePicker.vue'
import AgentMessageBody from '@/components/AgentMessageBody.vue'
// ThinkingPicker removed post-cutover (provider CoT permanently
// disabled; see commit d07f673). Component file kept for now —
// settings UI may reuse it as an advanced/debug toggle later.

const convId = inject('convId')
const loadConvs = inject('loadConvs')
// URL helper from App.vue — used after send() creates a new
// conversation. Bumps the URL to ``/chat?c=<id>`` via
// router.replace so refresh + back/forward stay aligned with the
// active conversation, without polluting history with a
// /chat → /chat?c=<id> step on every send.
const setActiveConvIdNoHistory = inject('setActiveConvIdNoHistory', () => {})

// Path scoping: read `path_filter` from URL (e.g. ?path_filter=/legal).
// User can clear the chip via the Chat UI.
const route = useRoute()
const router = useRouter()

// Per-SPA doc-name cache. Citations carry doc_id only; we resolve the
// current filename here so renames / re-ingests are reflected without
// having to re-query. ``ensure`` fires the fetch lazily; ``docName``
// returns whatever we have right now (re-renders when fetch lands).
// ``docFileId`` resolves the file_id that the PDF preview URL needs
// (agent citations carry chunk_id + doc_id but not file_id).
const { ensure: ensureDocName, getFilename: docName, getFileId: docFileId } = useDocCache()
// Combined: kick off the fetch (idempotent) and return the current
// best name. Gives us a one-call expression for templates.
function docNameFor(c) {
  if (!c?.doc_id) return ''
  ensureDocName(c.doc_id)
  return docName(c.doc_id)
}
const pathFilter = ref(route.query.path_filter || '')
// URL → local: keep in sync when user navigates to /chat?path_filter=...
watch(() => route.query.path_filter, v => { pathFilter.value = v || '' })
// Local → URL: when the user picks a different scope via PathScopePicker,
// reflect it in the URL so refresh / share / browser-back still works.
watch(pathFilter, v => {
  const cur = route.query.path_filter || ''
  if (v === cur) return
  const q = { ...route.query }
  if (v) q.path_filter = v
  else delete q.path_filter
  router.replace({ query: q })
})

// Project binding via ?project=<id>. When set, NEW conversations
// created here (the first send() in an unbound chat) will write
// ``Conversation.project_id`` so subsequent agent runs land in the
// project's workdir + run history. Resolved name is shown in the
// header banner so users see they're "working on" a project rather
// than firing free-floating chat. Phase 1.6 will use the same id
// to augment the system prompt with the project's name + workdir
// file list.
const boundProjectId = ref(route.query.project || '')
const boundProject = ref(null)  // {project_id, name, ...} once fetched
watch(() => route.query.project, v => { boundProjectId.value = v || '' })
watch(boundProjectId, async v => {
  if (!v) { boundProject.value = null; return }
  try { boundProject.value = await getProject(v) }
  catch { boundProject.value = null }
}, { immediate: true })

// Clear the binding from the URL only — the conversation row, if
// already created, keeps its project_id (un-binding the row needs
// a PATCH the Phase-1.7 polish surfaces). This intentionally
// matches the path-filter chip's "remove from URL" behaviour.
function clearProjectBinding() {
  const q = { ...route.query }
  delete q.project
  router.replace({ query: q })
}

// Bind module-level refs to local names for template access
const msgs = _msgs
const streaming = _streaming
const streamText = _streamText
const retInfo = _retInfo
const abortCtrl = _abortCtrl
const genTools = _genTools
const streamTrace = _streamTrace

// (Pre-agent-cutover this section adapted ``genTools.thinking``
// for the ThinkingPicker chip. The chip was removed when the
// agent loop became OpenAI's thinking layer; provider-side CoT
// is hard-disabled at every callsite — see commit ``d07f673``.
// Power users who need to re-enable it can set the override via
// settings.)

// Per-instance state (OK to reset on remount)
const input = ref('')
const chatEl = ref(null)
const pdf = reactive({ show: false, url: '', page: 1, highlights: [], cite: null, downloadUrl: '', sourceDownloadUrl: '', sourceLabel: '' })
// PdfViewer mount lifecycle is gated to the slide-pdf <Transition>:
//   open  → wait for ``@after-enter`` → mount  (slide-in is GPU-clean)
//   close → keep mounted, let slide-out finish → ``@after-leave`` →
//           unmount via outer v-if  (teardown happens off-screen)
// Don't tie mount to ``setTimeout`` — if the main thread is busy when
// the timer fires (e.g. async chunk for pdfjs-dist still loading),
// mount lands mid-slide and stutters the width transition.
const pdfMounted = ref(false)
let _wasAtBottomBeforePdf = false
watch(() => pdf.show, (v) => {
  if (v) {
    pdfMounted.value = false   // spinner placeholder during slide-in
    // Snapshot scroll state BEFORE the chat reflows narrower. If the
    // user was at the bottom (typical: just got an answer, clicked a
    // chip), pin them there once the slide-in lands.
    const el = chatEl.value
    _wasAtBottomBeforePdf = !!el && (el.scrollHeight - el.scrollTop - el.clientHeight < 80)
  }
  // No close-branch action: setting pdfMounted=false here would tear
  // down pdfjs synchronously and jankify the slide-out. Leave it to
  // ``onPdfAfterLeave``.
})

function onPdfAfterEnter() {
  // Slide-in finished — chat has fully reflowed. Mount the heavy
  // PdfViewer now and restore scroll if the user was at the bottom.
  pdfMounted.value = true
  if (_wasAtBottomBeforePdf) scroll()
}
function onPdfAfterLeave() {
  // Slide-out finished — outer v-if has destroyed the subtree, so
  // PdfViewer is gone. Reset the flag so the next open shows the
  // spinner again until the next ``after-enter``.
  pdfMounted.value = false
}
const trace = reactive({ show: false, data: null })
const empty = computed(() => !msgs.value.length && !streaming.value)

// On remount: reload messages from DB to ensure trace_id / citations are fresh.
// The watch(convId) only fires on *change* — if convId is the same (e.g. user
// clicked Chat tab to come back), the watch doesn't fire and we'd show stale data.
onMounted(() => {
  if (convId.value && !streaming.value) {
    _loadAndPoll(convId.value)
  }
  nextTick(() => chatEl.value && (chatEl.value.scrollTop = chatEl.value.scrollHeight))
})

// Module-level aliases for closures
const startTimer = _startTimer
const stopTimer = _stopTimer

// Tool labels live in ToolChip — that's the only consumer.
// Friendly i18n keys at ``chat.tool.*`` ("关键字检索 / Keyword
// search", etc.) are read directly inside the chain component.

/* ── Load conversation ── */
let _pollTimer = null
function stopPoll() { if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null } }

watch(convId, async (id) => {
  // When send() creates a new conversation, convId changes but we must NOT
  // reset the UI — the user's message and streaming state are already set up.
  if (_skipNextWatch) { _skipNextWatch = false; return }

  // Cancel any in-progress preset streaming
  _presetGenId++
  // Detach any running API stream from UI (the request continues in background;
  // backend persists the result via its own finally block).
  _streamGenId++
  streaming.value = false; streamText.value = ''; stopTimer(); stopPoll()
  streamTrace.value = []
  msgs.value = []; pdf.show = false; activeCiteId.value = null; trace.show = false
  if (id) {
    await _loadAndPoll(id)
  }
})

/** Load messages from DB; if last msg is user (answer pending), poll until done */
async function _loadAndPoll(id) {
  function _parseRaw(raw) {
    return raw.map(m => ({
      role: m.role,
      content: m.content,
      citations: normalizeCitations(m.citations_json),
      // Restore the agent reasoning chain from the DB so refresh
      // shows the same "Thought for Xs · N tools" panel it
      // showed live. The backend persists the same shape the
      // frontend's streamTrace builds (see _accumulate_trace in
      // api/routes/agent.py). Older rows have no agent_trace_json
      // column populated → undefined → AgentMessageBody renders
      // just the answer body with no inline tool chips.
      agentTrace: Array.isArray(m.agent_trace_json) ? m.agent_trace_json : null,
      traceId: m.trace_id || null,
    }))
  }
  try {
    const raw = await getMessages(id)
    msgs.value = _parseRaw(raw)
    enrichHistoricalCitations()
  } catch {}
  scroll()

  // If the last message is from the user, the backend is still processing.
  // Show a waiting indicator and poll DB until the assistant reply lands.
  // 3-minute cap so a generation that was lost server-side (e.g. process
  // restart between user message persist and assistant message persist)
  // doesn't leave the spinner running forever — at the cap we surface
  // a synthetic "interrupted" message instead.
  const lastMsg = msgs.value[msgs.value.length - 1]
  if (lastMsg?.role === 'user') {
    streaming.value = true; streamText.value = ''
    stopPoll()
    const POLL_INTERVAL_MS = 2000
    const POLL_CAP_MS = 3 * 60 * 1000
    const pollStartedAt = Date.now()
    _pollTimer = setInterval(async () => {
      if (convId.value !== id) { stopPoll(); return }
      try {
        const raw = await getMessages(id)
        const parsed = _parseRaw(raw)
        const last = parsed[parsed.length - 1]
        if (last?.role === 'assistant') {
          // Answer arrived — update msgs and stop polling
          stopPoll()
          msgs.value = parsed
          streaming.value = false; streamText.value = ''
          enrichHistoricalCitations()
          scroll()
          return
        }
      } catch {}
      // Cap the wait — beyond 3 minutes we assume the generation was
      // lost (process crash / OOM kill / network swallowed the stream
      // never to complete). Show a recoverable placeholder so the user
      // can retry instead of staring at a perpetual spinner.
      if (Date.now() - pollStartedAt > POLL_CAP_MS) {
        stopPoll()
        streaming.value = false; streamText.value = ''
        msgs.value = [
          ...msgs.value,
          {
            role: 'assistant',
            content: '_(Answer was interrupted — please re-send the question.)_',
            citations: null,
            thinking: null,
            traceId: null,
            _interrupted: true,
          },
        ]
        scroll()
      }
    }, POLL_INTERVAL_MS)
  }
}

function normalizeCitations(raw) {
  if (!raw?.length) return null
  if (typeof raw[0] === 'object' && raw[0] !== null) {
    // DB persistence (api/routes/agent.py::_persist_turn) writes
    // pool entries verbatim: ``cite_id`` / ``page_start`` etc.
    // The chip renderer + buildCiteDisplayMap read ``citation_id``
    // / ``page_no`` (the shape ``_agentCitationsToOldShape``
    // produces on the live stream path). Without this mapping
    // every reload entry looks like ``citation_id=undefined`` →
    // buildCiteDisplayMap's cidSet is empty → orderedCitations
    // returns [] and the user sees zero chips after refresh.
    // Mirror the live-path mapping here so both feeds present
    // the same shape downstream.
    return raw.map((c) => ({
      ...c,
      citation_id: c.citation_id || c.cite_id,
      page_no: c.page_no || c.page_start || c.page || 1,
    }))
  }
  return raw.map(id => (typeof id === 'string' ? { citation_id: id, _needsEnrich: true } : id))
}

// Convert agent-path citations into the shape the existing
// citation card / PDF preview expects. Agent citations carry
// ``cite_id`` (sequential ``c_N`` assigned by the dispatch layer
// in registration order) + ``chunk_id`` / ``doc_id`` /
// ``page_start`` / ``content``; the legacy chat UI expects
// ``citation_id`` (matched against ``[c_N]`` markers in the
// answer text) / ``page_no`` / ``snippet`` / ``highlights`` /
// ``file_id``. ``file_id`` is resolved lazily at click time via
// ``docFileId(doc_id)``.
//
// Ordering: walk the answer text in order, collect ``[c_N]``
// markers, put those chunks first (in citation-appearance order).
// Fill remaining slots up to ``_CITATION_DISPLAY_CAP`` with the
// highest-scored unreferenced chunks. Net effect: the chip rail
// reads top-to-bottom as ``[1] [2] [3]…`` matching the inline
// markers, with a few extra "also looked at this" sources tacked
// on if the LLM only cited a subset of the pool.
const _CITATION_DISPLAY_CAP = 8

function _agentCitationsToOldShape(cits, answerText) {
  if (!cits || !cits.length) return null

  // Parse answer for ``[c_N]`` markers in first-appearance order.
  const citedIds = []
  if (answerText) {
    const re = /\[(c_\d+(?:\s*,\s*c_\d+)*)\]/g
    let m
    while ((m = re.exec(answerText)) !== null) {
      for (const cid of m[1].split(/\s*,\s*/).map((s) => s.trim())) {
        if (cid && !citedIds.includes(cid)) citedIds.push(cid)
      }
    }
  }

  // Honour the model's citation choices strictly: only render
  // chunks the answer actually references via ``[c_N]``. Earlier
  // we padded the rail with the highest-scored unreferenced pool
  // entries, but that misfires when the model decides the corpus
  // didn't have what it needed — the answer carries zero markers
  // (model fell back to general knowledge), yet the rail dumps 8
  // unrelated chunks the search just happened to surface, making
  // it look like the answer cited mushroom-farming docs to
  // discuss Irish wooden flutes. If the model didn't cite, we
  // show nothing.
  if (!citedIds.length) return null

  // Lookup the pool by cite_id; entries lacking cite_id (legacy
  // pre-cite_id pool entries) get a synthesised one from index.
  const byId = new Map()
  cits.forEach((c, i) => {
    const id = c.cite_id || `c_${i + 1}`
    byId.set(id, { ...c, cite_id: id })
  })

  // Cited entries in answer order. We don't pad with unreferenced
  // pool entries — the chain UI already shows the user that
  // retrieval ran ("阅读了 N 个段落"); the citation rail is for
  // grounded sources only.
  //
  // IMPORTANT: do NOT cap the inline list here. Every ``[c_N]``
  // marker in the answer text needs a corresponding entry in
  // ``m.citations`` so renderMsg can turn it into a clickable
  // chip; capping at 8 silently broke the 9th, 10th, … markers
  // (rendered as raw ``[c_11]`` text instead). The chip RAIL
  // below the answer applies its own ``_CITATION_DISPLAY_CAP``
  // for visual hygiene — see ``mergedCitationsForRail``.
  const ordered = []
  for (const cid of citedIds) {
    if (byId.has(cid)) ordered.push(byId.get(cid))
  }

  if (!ordered.length) return null

  return ordered.map((c) => ({
    citation_id: c.cite_id,
    chunk_id: c.chunk_id,
    doc_id: c.doc_id,
    page_no: c.page_start || c.page || 1,
    snippet: typeof c.content === 'string'
      ? c.content.slice(0, 280)
      : (c.snippet || ''),
    score: c.score ?? null,
    // file_id + highlights + source come straight from the backend's
    // ``enrich_citations`` (api/agent/dispatch.py) — same fields the
    // deleted retrieval/citations.py produced for the fixed pipeline.
    // ``file_id`` is the renderable PDF (pdf_file_id when uploaded
    // file is non-PDF, else original); ``highlights`` is one rect
    // per parsed block the chunk covers, with real bboxes. Old
    // clients (or pre-enrichment legacy citations on conversation
    // reload) get the page-only fallback so the panel still opens
    // on the right page even without bbox.
    file_id: c.file_id || null,
    source_file_id: c.source_file_id || null,
    source_format: c.source_format || null,
    highlights: Array.isArray(c.highlights) && c.highlights.length
      ? c.highlights
      : (c.page_start ? [{ page_no: c.page_start, bbox: null }] : []),
  }))
}

async function enrichHistoricalCitations() {
  for (const m of msgs.value) {
    if (m.role !== 'assistant' || !m.traceId || !m.citations?.length) continue
    const needsEnrich = m.citations.some(c => c._needsEnrich || !c.file_id)
    if (!needsEnrich) continue
    try {
      const t = await getTrace(m.traceId)
      const full = t.trace_json?.generation?.citations_full
      if (!full?.length) continue
      const lookup = Object.fromEntries(full.map(c => [c.citation_id, c]))
      m.citations = m.citations.map(c => lookup[c.citation_id] || c)
    } catch {}
  }
}

// Empty-state suggestion chips. Each entry is a translation key
// under "chat.preset.*". Clicking a chip fires the question
// straight into the agent loop — no more canned essays, the
// real retrieval/KG/LLM stack answers exactly as it would for
// any user-typed question.
const presetChipKeys = [
  'whats_here',
  'recent',
  'entities',
  'find_topic',
]

async function send(text) {
  const q = (text || input.value).trim(); if (!q || streaming.value) return; input.value = ''

  // Optimistic UI: push the user bubble + flip the streaming flag
  // BEFORE awaiting any network roundtrip. Without this, fresh
  // conversations had a perceptible delay between clicking send
  // and the loading indicator appearing — the await on
  // ``createConversation`` (one HTTP roundtrip to the backend)
  // blocked everything below, including ``streaming.value=true``
  // and the user-bubble push. The user clicked send and stared at
  // a still UI for ~1-3s.
  msgs.value.push({ role: 'user', content: q })
  streaming.value = true; streamText.value = ''; retInfo.value = null
  streamTrace.value = []
  startTimer(); scroll()

  if (!convId.value) try {
    _skipNextWatch = true  // prevent watch from resetting UI on convId change
    // Pass the URL's bound project (if any) so the new conversation
    // row carries ``project_id`` from the moment it lands. Subsequent
    // agent runs see this on Conversation.project_id and route
    // tool-calls into the project's workdir.
    const newId = (
      await createConversation(q.slice(0, 60), boundProjectId.value || null)
    ).conversation_id
    convId.value = newId
    // Sync the URL → ``/chat?c=<id>``. Without this, a refresh
    // mid-stream loses the conversation entirely (App.vue's
    // convId boots from route.query.c, which would still be
    // empty). Uses replace so the address bar mutation doesn't
    // add a history step the user has to back-button through.
    setActiveConvIdNoHistory(newId)
    loadConvs()  // refresh sidebar immediately so the new conversation is visible
  } catch { _skipNextWatch = false }

  // (streaming.value, streamText, streamTrace, startTimer already
  // initialised at the top of send() via the optimistic-UI block.)

  const myGenId = ++_streamGenId
  abortCtrl.value = new AbortController()
  try {
    let fin = null
    let citationsList = null
    let turnsCompleted = 0  // count of agent.turn_end events seen so far

    // Helper: walk back through the trace and find the most-recent
    // entry of one of the requested kinds. Used to find which phase
    // entry to attach a thought event to or to mark done when tools
    // start firing.
    const lastEntry = (...kinds) => {
      for (let i = streamTrace.value.length - 1; i >= 0; i--) {
        if (kinds.includes(streamTrace.value[i].kind)) return streamTrace.value[i]
      }
      return null
    }

    // Map agent SSE events onto the chronological trace:
    //   agent.turn_start → push a 'phase' entry (running). The
    //                      timer ticks until the LLM produces
    //                      tools or an answer.
    //   agent.thought    → upgrade the trailing phase to 'thought'
    //                      with the model's reasoning text.
    //   tool.call_start  → mark the trailing phase/thought done,
    //                      push a 'tool' entry (running).
    //   tool.call_end    → flip the matching tool entry to done +
    //                      attach latency / hit-count summary.
    //   agent.turn_end   → mark the trailing phase/thought done
    //                      (it may have been a no-op turn that
    //                      went straight to answer with no tools).
    //   answer.delta     → append to streamText (synthesis stream).
    //   answer           → set streamText if empty (non-stream
    //                      direct answer path).
    //   done             → final state with citations + stop_reason.
    //
    // Persistence happens server-side once the SSE stream closes
    // (see api/routes/agent.py `_persist_turn`); the trace itself
    // is session-only — we attach it to the in-memory message
    // below but it isn't written to the DB.
    for await (const evt of agentChatStream({
      message: q,
      conversationId: convId.value,
      pathFilters: pathFilter.value ? [pathFilter.value] : null,
      signal: abortCtrl.value.signal,
    })) {
      // If conversation switched away, stop updating UI (but don't
      // abort the request — backend persistence still runs).
      if (myGenId !== _streamGenId) break
      const t = evt.type
      if (t === 'agent.turn_start') {
        // 3-way phase choice: forced synthesis (budget hit) → composing;
        // first turn → planning; otherwise → reviewing.
        const phase = evt.synthesis_only
          ? 'composing'
          : (turnsCompleted === 0 ? 'planning' : 'reviewing')
        streamTrace.value.push({
          kind: 'phase',
          phase,
          text: '',
          t0: Date.now(),
          elapsedSec: 0,
          status: 'running',
        })
        scroll()
      } else if (t === 'agent.thought') {
        // The LLM produced a natural-language preface alongside
        // tool calls. Upgrade the trailing phase entry into a
        // thought entry by storing the text — same slot, richer
        // content. Don't mark done yet; tool.call_start does that.
        const last = lastEntry('phase', 'thought')
        if (last && last.status === 'running') {
          last.kind = 'thought'
          last.text = evt.text || ''
        } else {
          // No active phase (shouldn't happen, but guard anyway):
          // append a standalone thought so the text isn't lost.
          streamTrace.value.push({
            kind: 'thought',
            phase: turnsCompleted === 0 ? 'planning' : 'reviewing',
            text: evt.text || '',
            t0: Date.now(),
            elapsedSec: 0,
            status: 'done',
          })
        }
        scroll()
      } else if (t === 'agent.turn_end') {
        turnsCompleted += 1
        const last = lastEntry('phase', 'thought')
        if (last && last.status === 'running') last.status = 'done'
      } else if (t === 'tool.call_start') {
        // Mark the trailing phase/thought done. If we'd been
        // streaming preface deltas into ``streamText`` for this
        // turn (the model wrote "Let me search for X first..."
        // before emitting the tool_call), MOVE that text into
        // the trailing chain entry as a thought — text was a
        // preface to the tool, not a final answer. The chain
        // CSS renders thought text inline with the phase label
        // ("Reviewing results — Let me search..."), so the move
        // shifts the text from below the chain to inline within
        // it on the tool.call_start boundary.
        const last = lastEntry('phase', 'thought')
        if (last && last.status === 'running') {
          if (streamText.value) {
            last.kind = 'thought'
            last.text = streamText.value
            streamText.value = ''
          }
          last.status = 'done'
        }
        const detail = evt.params?.query || evt.params?.chunk_id || evt.params?.doc_id || ''
        streamTrace.value.push({
          kind: 'tool',
          call_id: evt.id,
          name: evt.tool,
          detail: typeof detail === 'string' ? detail.slice(0, 64) : '',
          t0: Date.now(),
          t1: null,
          elapsedMs: 0,
          status: 'running',
          summary: '',
        })
        scroll()
      } else if (t === 'tool.call_end') {
        const summary = evt.result_summary || {}
        const sumText = summary.hit_count != null ? `${summary.hit_count} hits`
          : summary.entity_count != null ? `${summary.entity_count} entities`
          : summary.chunk_count != null ? `${summary.chunk_count} chunks`
          : summary.error ? 'error'
          : ''
        const entry = streamTrace.value.find(
          (e) => e.kind === 'tool' && e.call_id === evt.id,
        )
        if (entry) {
          entry.status = 'done'
          entry.t1 = (entry.t0 || Date.now()) + (evt.latency_ms || 0)
          entry.elapsedMs = evt.latency_ms || 0
          if (sumText) entry.summary = sumText
        }
      } else if (t === 'answer.delta') {
        // All deltas accumulate in streamText (rendered below the
        // chain). On tool turns, ``tool.call_start`` will MOVE
        // the accumulated text into the trailing chain entry as
        // an inline thought ("Reviewing results — let me…").
        // On direct-answer / synthesis turns, no tool.call_start
        // fires, so streamText stays as the final answer body.
        // This keeps the streaming → final-answer flow seamless
        // (no text "jumping" from chain to body at the end of a
        // long composing turn).
        streamText.value += evt.text || ''
        scroll()
      } else if (t === 'answer') {
        // Final aggregated answer event — backend always emits
        // this with the full answer text. For synthesis turns we
        // already accumulated via deltas; for non-streaming direct
        // answers (DSML fallback path) this is the only delivery.
        // Mark trailing entry done. Set streamText if empty.
        const last = lastEntry('phase', 'thought')
        if (last && last.status === 'running') last.status = 'done'
        if (!streamText.value) streamText.value = evt.text || ''
        scroll()
      } else if (t === 'done') {
        fin = evt
        citationsList = evt.citations || null
      }
    }
    if (fin && myGenId === _streamGenId) {
      const answerContent = fin.answer || streamText.value || ''
      // Detect error termination: backend sets stop_reason="error"
      // on LLM auth/rate-limit/network failures and now also fills
      // ``error`` with a user-readable string. Without this
      // surfacing the assistant bubble would render empty and
      // users see "nothing happened" with no way to know why.
      const hasError = fin.stop_reason === 'error' || !!fin.error
      const errorMessage = fin.error
        || (hasError ? t('chat.error_generic') : '')
      // Snapshot the entire trace verbatim. Earlier we filtered
      // out bare phase entries (no text, just a timer) so a
      // trailing "Planning… 1s" wouldn't float above a direct
      // answer — but that also dropped MID-stream phases
      // ("Reviewing… 5s") whose timing is the most useful piece
      // of feedback in the chain (it's what tells the user
      // "model spent N seconds thinking between tool batches").
      // The summary in the chain header sums elapsedSec over all
      // entries; dropping phases zeroed it out. Keep everything;
      // the UI chooses how to render bare phases (a slim
      // "Reviewing · 3s" line).
      const traceSnapshot = streamTrace.value.map((e) => ({ ...e }))
      msgs.value.push({
        role: 'assistant',
        content: answerContent,
        thinking: '',  // thinking is disabled in the agent LLM call
        citations: _agentCitationsToOldShape(citationsList, answerContent),
        agentTrace: traceSnapshot,
        // ``error`` triggers the red bubble renderer below — see
        // the v-if branch on the assistant message in the
        // template. Only set when there's actually an error so
        // successful turns leave it falsy.
        error: hasError ? errorMessage : null,
        stats: {
          stop_reason: fin.stop_reason,
          iterations: fin.iterations,
          tool_calls_count: fin.tool_calls_count,
          tokens_in: fin.tokens_in,
          tokens_out: fin.tokens_out,
          total_latency_ms: fin.total_latency_ms,
        },
        trace: null,
        traceId: null,
      })
      streamText.value = ''
      streamTrace.value = []
    }
  } catch (e) {
    // AbortError from user clicking stop — not an error
    if (e.name !== 'AbortError' && myGenId === _streamGenId) {
      // Use the same red-bubble path as backend-surfaced errors
      // so network / HTTP / parse failures render identically
      // instead of as a plain prose "Error: ..." message.
      msgs.value.push({
        role: 'assistant',
        content: '',
        error: e.message || String(e),
      })
    }
    streamText.value = ''
    streamTrace.value = []
  }
  finally {
    if (myGenId === _streamGenId) { streaming.value = false; stopTimer() }
    loadConvs()
  }
}

function stopGeneration() {
  if (abortCtrl.value) { abortCtrl.value.abort(); abortCtrl.value = null }
  _presetGenId++
  streaming.value = false; stopTimer(); stopPoll()
  // Keep whatever streamed text we have as a partial message
  if (streamText.value.trim()) {
    msgs.value.push({ role: 'assistant', content: streamText.value, citations: null })
    streamText.value = ''
  }
  loadConvs()
}

function scroll() { nextTick(() => chatEl.value && (chatEl.value.scrollTop = chatEl.value.scrollHeight)) }
function onKey(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!streaming.value) send() } }

/* ── PDF ── */
const activeCiteId = ref(null)    // citation_id for legacy compat
const activeChunkId = ref(null)   // chunk_id — the real identity

function onCiteClick(c) {
  // Agent-path citations carry only ``doc_id`` + ``chunk_id`` —
  // ``file_id`` is resolved lazily below via the doc cache. So
  // we can't bail early on ``!c.file_id`` (the old check skipped
  // every agent click silently — clicked chip lit up but PDF
  // never opened). Bail only if we have neither file_id nor
  // doc_id to resolve from.
  if (!c || (!c.file_id && !c.doc_id)) return

  // Toggle: click same chunk again → close PDF & clear highlight
  if (activeChunkId.value === c.chunk_id && pdf.show) {
    pdf.show = false
    activeCiteId.value = null
    activeChunkId.value = null
    return
  }

  activeCiteId.value = c.citation_id
  activeChunkId.value = c.chunk_id
  const hl = (c.highlights || []).map(h => {
    const b = h.bbox
    const bbox = Array.isArray(b) ? { x0: b[0], y0: b[1], x1: b[2], y1: b[3] } : b
    return { page_no: h.page_no, bbox }
  })

  // file_id resolution: agent-path citations carry only doc_id /
  // chunk_id, so we resolve file_id from the doc cache (which the
  // citation card's name lookup already populates). Falls back to
  // any explicit file_id on the citation (legacy /query payloads).
  if (c.doc_id) ensureDocName(c.doc_id)
  const resolvedFileId = c.file_id || docFileId(c.doc_id) || ''

  // Download URLs
  const dlUrl = resolvedFileId ? fileDownloadUrl(resolvedFileId) : ''
  const srcUrl = c.source_file_id ? fileDownloadUrl(c.source_file_id) : ''
  const srcLabel = c.source_format ? c.source_format.toUpperCase() : ''

  Object.assign(pdf, {
    show: true,
    url: resolvedFileId ? filePreviewUrl(resolvedFileId) : '',
    page: c.page_no || 1,
    highlights: hl,
    cite: c,
    downloadUrl: dlUrl,
    sourceDownloadUrl: srcUrl,
    sourceLabel: srcLabel,
  })
}

/**
 * Render assistant message to HTML with markdown/latex + citation tags.
 * Citations become <span class="cite-tag" data-cite-idx="N" data-cite-id="c_1">[c_1]</span>
 * so event delegation can handle clicks.
 * Active state is NOT baked in — a watcher on activeCiteId toggles .cite-active via DOM.
 */
/**
 * Build the c_N → academic display number map by scanning the answer
 * text in order. First citation to appear gets [1], second gets [2], etc.
 * This eliminates gaps that arise when retrieval produces c_1..c_N but
 * the LLM only references a non-contiguous subset (e.g. c_1, c_3, c_4).
 */
function buildCiteDisplayMap(text, cites) {
  if (!cites?.length || !text) return {}
  const cidSet = new Set(cites.map(c => c?.citation_id).filter(Boolean))
  const map = {}
  let next = 1
  const re = /\[(c_\d+(?:\s*,\s*c_\d+)*)\]/g
  let m
  while ((m = re.exec(text)) !== null) {
    for (const cid of m[1].split(/\s*,\s*/).map(s => s.trim())) {
      if (cidSet.has(cid) && !(cid in map)) {
        map[cid] = next++
      }
    }
  }
  return map
}

/**
 * Citation card list, sorted to match the inline display map (so the
 * card reads top-to-bottom as [1] [2] [3] ...). Falls back to the
 * original retrieval order for any cite the LLM declared but the
 * regex didn't surface (shouldn't happen, but harmless).
 */
function orderedCitations(m) {
  const cites = m?.citations
  if (!cites?.length) return []
  // Filter to citations the answer text ACTUALLY references via
  // ``[c_N]`` markers, in first-appearance order. Two paths feed
  // this:
  //   * live stream — _agentCitationsToOldShape already pre-filtered
  //                   (returns null when answer cited nothing)
  //   * DB reload  — citations_json on the message row carries the
  //                  full pool (every chunk any tool surfaced),
  //                  not the cited subset. Without this gate, a
  //                  reload would dump 40+ chips below the answer
  //                  even though the model only [c_1] [c_18]'d
  //                  two of them.
  // No markers → no chips. Same invariant as the live path.
  const map = buildCiteDisplayMap(m.content || '', cites)
  if (!Object.keys(map).length) return []
  return cites
    .filter((c) => c?.citation_id && c.citation_id in map)
    .sort((a, b) => map[a.citation_id] - map[b.citation_id])
}

/**
 * Build the rail entries — same content as orderedCitations, but
 * adjacent entries that share (doc_id, page_no) get folded into
 * one chip with the labels concatenated (``[2,3,5] p114 …``).
 *
 * Why: the chunker splits long pages into multiple chunks, each
 * with its own cite_id. The agent legitimately reads a few of
 * them in different rounds, so the answer carries distinct
 * ``[c_2]`` ``[c_3]`` ``[c_5]`` markers grounding different
 * facts. The CHIP rail though shows them as three identical-
 * looking rows ("p114 08_natural_beekeeping.md" three times),
 * which reads like a mistake — same source listed thrice.
 *
 * Folding by (doc_id, page_no) preserves the inline marker
 * granularity (each [N] still opens to its own chunk) while
 * keeping the rail visually clean. Click on a merged chip uses
 * the combined ``highlights`` so the PDF panel shows every
 * bbox the merged chunks cover.
 */
function mergedCitationsForRail(m) {
  const flat = orderedCitations(m)
  const groups = []
  for (let i = 0; i < flat.length; i++) {
    const c = flat[i]
    const label = i + 1
    const key = `${c.doc_id || '?'}::${c.page_no || '?'}`
    const last = groups[groups.length - 1]
    if (last && last._key === key) {
      last._labels.push(label)
      last._chunkIds.push(c.chunk_id)
      last.highlights = [...(last.highlights || []), ...(c.highlights || [])]
    } else {
      groups.push({
        ...c,
        _key: key,
        _labels: [label],
        _chunkIds: [c.chunk_id],
      })
    }
  }
  // Cap the RAIL only — inline ``[c_N]`` chips in the answer
  // body rendered via renderMsg are NOT capped (every marker
  // needs its citation entry to remain clickable). The rail
  // is for visual-summary purposes; once you have ~8 distinct
  // (doc, page) groups, more chips just clutter the rail.
  return groups.slice(0, _CITATION_DISPLAY_CAP)
}

function renderMsg(text, cites) {
  if (!text) return ''

  // 1. Pull citation markers out BEFORE markdown/latex processing
  //    so [c_1] isn't parsed as a markdown link reference.
  //    Lookup by citation_id (e.g. "c_3") — NOT by array index,
  //    because cites is a filtered subset (only citations the LLM used).
  const citePH = []           // { idx, cid, chunkId, label }
  let processed = text
  if (cites?.length) {
    // Build a map: citation_id → index in cites array (for click handling)
    const citeMap = {}
    cites.forEach((c, i) => { if (c?.citation_id) citeMap[c.citation_id] = i })

    // Academic-style sequential numbering: scan the text, assign 1,2,3,...
    // by first-appearance order. ``c_2`` skipped by the LLM no longer
    // creates a [1][3][4] gap — instead we get [1][2][3].
    const displayMap = buildCiteDisplayMap(text, cites)

    // Handle both [c_1] and [c_1, c_3, c_5] formats. Display number
    // comes from displayMap; data-cite-id keeps the original c_N so
    // click handlers + chunk-active highlighting still work.
    processed = text.replace(/\[(c_\d+(?:\s*,\s*c_\d+)*)\]/g, (match, inner) => {
      // Split "c_1, c_3, c_5" into individual citations
      const cids = inner.split(/\s*,\s*/)
      const parts = []
      for (const cid of cids) {
        const idx = citeMap[cid]
        if (idx != null) {
          const chunkId = cites[idx]?.chunk_id || ''
          // Sequential display number from first-appearance map; fall
          // back to the raw N if for some reason the cid isn't mapped
          // (should never happen since cidSet drives the map).
          const displayNum = displayMap[cid] ?? cid.replace(/^c_/, '')
          citePH.push({ idx, cid, chunkId, label: String(displayNum) })
          parts.push(`<!--CITE:${citePH.length - 1}-->`)
        } else {
          // Unknown citation — keep as plain text (with c_ prefix so
          // it's obvious something's missing from the citation map)
          parts.push(`[${cid}]`)
        }
      }
      return parts.join('')
    })
  }

  // 2. Render markdown + latex (HTML comments survive untouched)
  let html = renderMarkdown(processed)

  // 3. Replace comment placeholders with real HTML cite spans
  html = html.replace(/<!--CITE:(\d+)-->/g, (_, i) => {
    const p = citePH[parseInt(i)]
    return `<span class="cite-tag" data-cite-idx="${p.idx}" data-cite-id="${p.cid}" data-chunk-id="${p.chunkId}">${p.label}</span>`
  })
  return html
}

/** Render streaming text with markdown/latex */
function renderStream(text) {
  if (!text) return ''
  return renderMarkdown(text)
}

/** Event delegation handler for inline cite tag clicks */
function onMsgClick(e, cites) {
  const el = e.target.closest('.cite-tag[data-cite-idx]')
  if (!el || !cites?.length) return
  const idx = parseInt(el.dataset.citeIdx)
  if (idx >= 0 && idx < cites.length) onCiteClick(cites[idx])
}

/** Sync activeChunkId → .cite-active class on inline cite tags via DOM.
 *  Highlights by chunk_id across ALL messages — the same source chunk
 *  lights up everywhere it's cited, regardless of citation_id label. */
watch(activeChunkId, (newChunkId) => {
  nextTick(() => {
    const container = chatEl.value
    if (!container) return
    container.querySelectorAll('.cite-tag.cite-active').forEach(el => el.classList.remove('cite-active'))
    if (newChunkId) {
      container.querySelectorAll(`.cite-tag[data-chunk-id="${newChunkId}"]`).forEach(el => el.classList.add('cite-active'))
    }
  })
})

/* ── Trace ── */
/**
 * Trace display. `trace.data` is the OTel payload:
 *   { spans: [{ name, parent_span_id, duration_ms, attributes, events, ... }] }
 * Live queries carry this in the message itself (`m.trace`); historical
 * messages only have a `traceId` so we fetch from /traces/{id} on click.
 */
function openTraceFromMessage(m) {
  // m.trace = raw OTel payload from the live response / SSE "trace" event
  trace.data = m.trace || null
  trace.show = true
}

async function openTraceById(traceId) {
  if (!traceId) return
  try {
    const t = await getTrace(traceId)
    // Persisted trace_json holds the same {spans: [...]} shape written
    // by AnsweringPipeline._persist_trace.
    trace.data = t.trace_json || null
    trace.show = true
  } catch (e) { console.warn('Failed to load trace:', e) }
}

function onTraceClick(m) {
  if (trace.show) { trace.show = false; return }
  if (m.trace) openTraceFromMessage(m)
  else if (m.traceId) openTraceById(m.traceId)
  else console.warn('No trace available for this message')
}

// Don't abort or stop timer on unmount — streaming state is module-level
// and must survive component remount. Timer stops in stream's finally block.
</script>

<template>
  <div class="flex h-full relative">
    <!-- (Old top-center scope chip removed — PathScopePicker above the
         input is now the single source of truth and entry point.) -->

    <!-- ═══════ Trace panel (OTel waterfall) ═══════ -->
    <Transition name="slide-trace">
      <div v-if="trace.show" class="w-96 shrink-0 border-r border-line flex flex-col bg-bg overflow-hidden">
        <div class="flex-none flex items-center justify-between px-4 py-2.5 border-b border-line">
          <span class="text-[11px] text-t1 font-medium">Trace</span>
          <button @click="trace.show = false" class="p-1 text-t3 hover:text-t1 rounded hover:bg-bg-hover transition-colors">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <OtelTraceViewer :trace="trace.data" />
      </div>
    </Transition>

    <!-- ═══════ Chat main ═══════ -->
    <div class="flex-1 flex flex-col min-w-0">

      <!-- Project binding banner — visible whenever ?project=<id> is
           in the URL. Tells the user this chat is "working on" a
           project so the agent's tool calls (Phase 2+) will land in
           that project's workdir. Click the project name to jump
           into the project detail view; the small × clears the
           binding for THIS view only (the conversation row, if it
           exists, keeps its project_id — un-binding the row needs
           a separate PATCH which Phase 1.7 will surface). -->
      <div
        v-if="boundProject"
        class="flex-none flex items-center justify-between gap-3 px-4 py-1.5 border-b border-line bg-bg2 text-[11.5px] text-t2"
      >
        <div class="flex items-center gap-2 min-w-0">
          <FolderKanbanIcon :size="13" :stroke-width="1.75" class="text-t3 shrink-0" />
          <span class="text-t3 shrink-0">{{ t('chat.project_banner.working_on') }}</span>
          <button
            class="font-medium text-t1 truncate hover:underline"
            @click="router.push(`/workspace/${boundProject.project_id}`)"
          >
            {{ boundProject.name }}
          </button>
        </div>
        <button
          class="p-0.5 text-t3 hover:text-t1 rounded hover:bg-bg-hover transition-colors shrink-0"
          :title="t('chat.project_banner.clear')"
          @click="clearProjectBinding"
        >
          <XIcon :size="13" :stroke-width="1.75" />
        </button>
      </div>

      <!-- EMPTY STATE -->
      <div v-if="empty" class="flex-1 flex flex-col">
        <div class="flex-[3]"></div>
        <div class="pl-8 pr-16">
          <div class="max-w-2xl mx-auto text-center">
            <img src="/craig.png" alt="" class="w-20 h-20 rounded-full mx-auto mb-3" />
            <h1 class="wordmark text-[32px] mb-2">OpenCraig</h1>
            <p class="text-sm text-t3 mb-8 max-w-md mx-auto leading-relaxed">{{ t('chat.tagline') }}</p>
            <div class="flex flex-wrap justify-center gap-2 mb-10">
              <button v-for="key in presetChipKeys" :key="key" @click="send(t('chat.preset.' + key))"
                class="px-4 py-2 rounded-full border border-line text-xs text-t2 hover:bg-bg3 hover:border-line2 transition-colors"
              >{{ t('chat.preset.' + key) }}</button>
            </div>
          </div>
        </div>
        <div class="flex-[4]"></div>
        <div class="pl-8 pr-16 pb-6">
          <div class="max-w-2xl mx-auto">
            <!-- Scope picker + Tools popup: badge-style chips above the
                 input. Use ``flex gap`` so they sit side-by-side; both
                 share the borderless-fill chip styling so they read as
                 belonging to the input card below. -->
            <div class="mb-1.5 pl-1 flex items-center gap-1.5">
              <PathScopePicker v-model="pathFilter" />
              <!-- ThinkingPicker removed post-cutover. Provider CoT
                   is permanently disabled (the agent loop is the
                   thinking layer); users see the agent's reasoning
                   inline as "🧠 审视检索结果中…" between tool rounds.
                   See commit d07f673. -->
              <!-- Web search placeholder. Disabled chip until a
                   real retriever (Tavily / SearXNG / etc.) lands. -->
              <span
                class="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-bg3/40 text-[11px] text-t3 cursor-not-allowed opacity-60"
                :title="t('tools.web_search_coming_soon')"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="12" cy="12" r="10"/>
                  <path d="M2 12h20M12 2a15 15 0 010 20M12 2a15 15 0 000 20"/>
                </svg>
                <span>{{ t('tools.web_search') }}</span>
                <span class="text-t3/60">·</span>
                <span class="italic">{{ t('tools.web_search_coming_soon') }}</span>
              </span>
            </div>
            <div class="flex items-end gap-3 px-4 py-3 rounded-xl border border-line shadow-sm bg-bg">
              <textarea v-model="input" @keydown="onKey" :placeholder="t('chat.ask_a_question')" rows="2"
                class="flex-1 bg-transparent border-none outline-none resize-none text-sm text-t1 leading-relaxed"
                style="min-height: 40px; max-height: 120px" autofocus />
              <button @click="send()" :disabled="!input.trim()"
                class="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 transition-colors"
                :class="input.trim() ? 'bg-brand text-white' : 'bg-bg3 text-t3'">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
              </button>
            </div>
            <div class="text-center mt-2 text-[10px] text-t3">{{ t('chat.may_make_mistakes') }}</div>
          </div>
        </div>
      </div>

      <!-- MESSAGES -->
      <template v-else>
        <!-- ``scrollbar-gutter: stable both-edges`` makes the
             vertical scrollbar reserve space on BOTH sides of the
             scroll container (10px on each, even when not scrolling).
             Without it the scrollbar eats 10px from the right of the
             content area, and the centred ``max-w-2xl`` inner wrapper
             sits 5px (= half the scrollbar width) LEFT of the input
             box's inner wrapper below — visible as a tiny but
             noticeable misalignment between message blocks and input
             box edges. ``both-edges`` symmetrises the padding so the
             two centred wrappers land at exactly the same X. -->
        <div ref="chatEl" class="flex-1 overflow-y-auto pl-6 pr-14 py-6"
             style="scrollbar-gutter: stable both-edges">
          <!-- Slightly wider than the default ``max-w-2xl`` (672px)
               so message text and tables breathe a bit. The input
               wrapper below sits one notch wider still (744 vs 720),
               so the input box visually frames the conversation —
               ChatGPT-style. -->
          <div class="max-w-[720px] mx-auto space-y-4">
            <div v-for="(m, i) in msgs" :key="i" class="fadein">
              <!-- User -->
              <div v-if="m.role === 'user'" class="flex justify-end mb-2">
                <div class="px-4 py-2.5 rounded-2xl text-sm bg-bg3 text-t1 max-w-[75%]">{{ m.content }}</div>
              </div>
              <!-- Assistant — Claude-Code-style interleaved body:
                   the model's narration text, tool-chip groups,
                   and the final answer all weave together in
                   chronological order. The standalone chain panel
                   that used to hover above the body has been
                   absorbed; thoughts now ARE the message text. -->
              <div v-else class="group mb-2">
                <!-- Error block — backend signaled stop_reason="error"
                     OR the SSE call threw at the network layer.
                     Renders BEFORE the body so the chain (if any)
                     still shows the partial work that got done
                     before the failure. -->
                <div v-if="m.error" class="error-bubble">
                  <svg class="error-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="10" />
                    <line x1="12" y1="8" x2="12" y2="12" />
                    <line x1="12" y1="16" x2="12.01" y2="16" />
                  </svg>
                  <div class="error-body">
                    <div class="error-title">{{ t('chat.error_title') }}</div>
                    <div class="error-detail">{{ m.error }}</div>
                  </div>
                </div>
                <AgentMessageBody
                  :trace="m.agentTrace || []"
                  :content="m.content || ''"
                  :citations="m.citations || null"
                  :render-text="renderMsg"
                  :on-cite-click="(e) => onMsgClick(e, m.citations)"
                />
                <div v-if="m.citations?.length" class="flex flex-wrap gap-1.5 mt-3">
                  <!-- Citation card: paper-style reference list under the
                       answer. Sorted to match the inline ``[1][2][3]``
                       order in the body text (first-appearance, no
                       gaps). Display label is just ``[N]``; data layer
                       still references the original c_N via the click
                       handler. The filename column is resolved live
                       from useDocCache (keyed by doc_id) so renames
                       reflect immediately and persisted citations
                       never carry stale names. -->
                  <!-- Rail chips folded by (doc_id, page_no) — see
                       ``mergedCitationsForRail``. Inline ``[c_N]``
                       markers in the answer body remain independent
                       (each marker still opens its specific chunk
                       via onMsgClick); the rail just stops listing
                       p114 three times when the agent read three
                       chunks of one page. -->
                  <button v-for="(c, ci) in mergedCitationsForRail(m)" :key="c._key + ':' + ci"
                    class="flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[10px] transition-colors"
                    :class="c._chunkIds.includes(activeChunkId)
                      ? 'border-brand bg-brand/10 text-brand'
                      : 'border-line text-t2 hover:bg-bg3'"
                    @click="onCiteClick(c)">
                    <span class="font-medium" :class="c._chunkIds.includes(activeChunkId) ? '' : 'text-brand'">[{{ c._labels.join(',') }}]</span>
                    <span v-if="c.page_no" class="text-t3">p{{ c.page_no }}</span>
                    <span v-if="c.doc_id" class="text-t2 truncate max-w-64">
                      {{ docNameFor(c) || t('chat.citation_untitled') }}
                    </span>
                  </button>
                </div>
                <button v-if="m.role === 'assistant' && (m.trace || m.traceId)"
                  class="mt-1.5 text-[10px] text-t3 invisible group-hover:visible flex items-center gap-1"
                  @click="onTraceClick(m)">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20V10M18 20V4M6 20v-4"/></svg>
                  Trace
                </button>
              </div>
            </div>

            <!-- ═══ Streaming: live message body ═══
                 Same component as the persisted assistant message,
                 just driven by the in-flight streamTrace +
                 streamText. The Claude-Code-style body weaves
                 thought paragraphs and tool chips chronologically
                 as events arrive. Once 'done' fires the streaming
                 state moves into msgs[] and this branch unmounts. -->
            <div v-if="streaming" class="fadein group mb-2">
              <AgentMessageBody
                :trace="streamTrace"
                :content="streamText"
                :citations="null"
              />
              <!-- Calm pulse indicator — always visible while the
                   agent is working, even between events. See
                   ThinkingPulse.vue for the design. -->
              <div class="thinking-indicator">
                <ThinkingPulse :size="18" />
              </div>
            </div>
          </div>
        </div>

        <!-- Bottom input -->
        <div class="pl-6 pr-14 pb-4 border-t border-line bg-bg">
          <!-- 744px = 24px wider than the messages column (720px),
               12px on each side. Subtle visual cue that the input
               sits "around" the conversation rather than inside it. -->
          <div class="max-w-[744px] mx-auto pt-3">
            <!-- Scope + Tools chips above the input. Aligned to the
                 input box's outer left edge (no leading padding) so
                 the chip cards form a single straight visual rail
                 with the rounded input box below. -->
            <div class="mb-1.5 flex items-center gap-1.5">
              <PathScopePicker v-model="pathFilter" />
              <!-- ThinkingPicker removed post-cutover. Provider CoT
                   is permanently disabled (the agent loop is the
                   thinking layer); users see the agent's reasoning
                   inline as "🧠 审视检索结果中…" between tool rounds.
                   See commit d07f673. -->
              <!-- Web search placeholder. Disabled chip until a
                   real retriever (Tavily / SearXNG / etc.) lands. -->
              <span
                class="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-bg3/40 text-[11px] text-t3 cursor-not-allowed opacity-60"
                :title="t('tools.web_search_coming_soon')"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="12" cy="12" r="10"/>
                  <path d="M2 12h20M12 2a15 15 0 010 20M12 2a15 15 0 000 20"/>
                </svg>
                <span>{{ t('tools.web_search') }}</span>
                <span class="text-t3/60">·</span>
                <span class="italic">{{ t('tools.web_search_coming_soon') }}</span>
              </span>
            </div>
            <div class="flex items-end gap-3 px-4 py-2.5 rounded-xl border border-line bg-bg">
              <textarea v-model="input" @keydown="onKey" :placeholder="t('chat.ask_followup')" rows="1"
                class="flex-1 bg-transparent border-none outline-none resize-none text-sm text-t1 leading-relaxed"
                style="min-height: 20px; max-height: 80px"
                @input="$event.target.style.height='auto';$event.target.style.height=$event.target.scrollHeight+'px'" />
              <!-- Stop button while streaming -->
              <button v-if="streaming" @click="stopGeneration()"
                class="w-7 h-7 rounded-lg flex items-center justify-center shrink-0 bg-bg3 hover:bg-bg3/80 text-t1 transition-colors"
                :title="t('chat.stop_generation')">
                <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
              </button>
              <!-- Send button -->
              <button v-else @click="send()" :disabled="!input.trim()"
                class="w-7 h-7 rounded-lg flex items-center justify-center shrink-0 transition-colors"
                :class="input.trim() ? 'bg-brand text-white' : 'bg-bg3 text-t3'">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
              </button>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- ═══════ PDF panel ═══════ -->
    <Transition name="slide-pdf" @after-enter="onPdfAfterEnter" @after-leave="onPdfAfterLeave">
      <div v-if="pdf.show" class="w-[45%] max-w-[620px] min-w-[400px] shrink-0 border-l border-line flex flex-col overflow-hidden">
        <div class="flex-none flex items-center justify-between px-3 py-2 border-b border-line">
          <div class="flex items-center gap-2 text-xs text-t3">
            <span>Page {{ pdf.page }}</span>
            <span v-if="pdf.cite" class="text-brand">{{ pdf.cite.citation_id }}</span>
          </div>
          <button @click="pdf.show = false; activeCiteId = null" class="p-1 text-t3 hover:text-t1 rounded hover:bg-bg-hover transition-colors">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <PdfViewer v-if="pdfMounted"
          :url="pdf.url" :page="pdf.page" :highlight-blocks="pdf.highlights"
          :downloadUrl="pdf.downloadUrl" :sourceDownloadUrl="pdf.sourceDownloadUrl" :sourceLabel="pdf.sourceLabel"
          class="flex-1" />
        <div v-else class="flex-1 flex items-center justify-center"><Spinner /></div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.fadein { animation: fadein .2s ease; }
@keyframes fadein { from { opacity: 0; transform: translateY(4px) } }

/* Always-on streaming indicator pinned at the bottom of the
   in-flight assistant message. Visible the whole time
   ``streaming.value`` is true — closes the "is anything happening?"
   gap before the first SSE event lands and after the last one
   while the model is still composing its final answer. The icon
   matches the agent identity (4-pointed sparkle, same SVG used
   on the original ThinkingPicker chip). 4s rotation period — slow
   enough to read as "calm working" rather than "spinning out of
   control". */
.thinking-indicator {
  display: flex;
  align-items: center;
  margin-top: 10px;
  color: var(--color-t3);
}

/* ── Markdown body styles ── */
.msg-body :deep(p) { margin: 0.4em 0; }
.msg-body :deep(p:first-child) { margin-top: 0; }
.msg-body :deep(p:last-child) { margin-bottom: 0; }
.msg-body :deep(h1),
.msg-body :deep(h2),
.msg-body :deep(h3),
.msg-body :deep(h4) { font-weight: 600; margin: 0.8em 0 0.3em; line-height: 1.4; }
.msg-body :deep(h1) { font-size: 1.25em; }
.msg-body :deep(h2) { font-size: 1.15em; }
.msg-body :deep(h3) { font-size: 1.05em; }
.msg-body :deep(ul),
.msg-body :deep(ol) { padding-left: 1.5em; margin: 0.4em 0; }
.msg-body :deep(li) { margin: 0.15em 0; }
.msg-body :deep(ul) { list-style: disc; }
.msg-body :deep(ol) { list-style: decimal; }
.msg-body :deep(blockquote) {
  border-left: 3px solid var(--color-line, #ddd); padding-left: 0.8em;
  margin: 0.5em 0; color: var(--color-t2, #666);
}
.msg-body :deep(code) {
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 0.85em; padding: 0.15em 0.35em; border-radius: 4px;
  background: var(--color-bg3, #f0f0f0);
}
.msg-body :deep(pre) {
  margin: 0.5em 0; padding: 0.75em 1em; border-radius: 8px;
  background: var(--color-bg3, #f0f0f0); overflow-x: auto;
  font-size: 0.82em; line-height: 1.5;
}
.msg-body :deep(pre code) {
  padding: 0; background: none; font-size: inherit;
}
.msg-body :deep(table) {
  border-collapse: collapse; margin: 0.5em 0; font-size: 0.9em; width: auto;
}
.msg-body :deep(th),
.msg-body :deep(td) {
  border: 1px solid var(--color-line, #ddd); padding: 0.35em 0.65em; text-align: left;
}
.msg-body :deep(th) { background: var(--color-bg3, #f0f0f0); font-weight: 600; }
.msg-body :deep(hr) { border: none; border-top: 1px solid var(--color-line, #ddd); margin: 0.8em 0; }
.msg-body :deep(a) { color: var(--color-brand, #3d3d3d); text-decoration: underline; }
.msg-body :deep(strong) { font-weight: 600; }
.msg-body :deep(img) { max-width: 100%; border-radius: 6px; }

/* KaTeX overrides */
.msg-body :deep(.katex-display) { margin: 0.5em 0; overflow-x: auto; }
.msg-body :deep(.katex) { font-size: 1em; }

/* cite-tag lives inside v-html, so needs :deep() under scoped styles */
.msg-body :deep(.cite-tag) {
  display: inline; padding: 1px 5px; margin: 0 1px; border-radius: 4px;
  font-size: 10px; font-weight: 600;
  color: var(--color-brand, #3d3d3d); background: var(--color-bg3, #f0f0f0);
  cursor: pointer; transition: background .15s;
}
.msg-body :deep(.cite-tag:hover) { background: var(--color-line2, #ddd); }
.msg-body :deep(.cite-tag.cite-active) { background: var(--color-brand, #3d3d3d); color: #fff; }



/* Slide panels animate ``transform`` (compositor, no per-frame reflow)
   instead of ``width`` (main-thread layout × 12+ frames at 60fps).
   The chat still reflows once when the panel enters/leaves flex space
   (v-if true/false), but the slide itself runs on the GPU. With
   ``width`` animation, every frame of the 200ms slide forced full
   chat reflow → the markdown body + cite chips + code blocks made it
   stutter visibly.

   Note: ``will-change: transform`` hints the browser to promote the
   panel to its own layer up-front; without it the first frame of the
   slide stutters as a layer is created on demand. */
.slide-trace-enter-active, .slide-trace-leave-active,
.slide-pdf-enter-active,   .slide-pdf-leave-active {
  transition: transform .2s cubic-bezier(.4,0,.2,1), opacity .2s ease;
  will-change: transform;
}
.slide-trace-enter-from, .slide-trace-leave-to { transform: translateX(-100%); opacity: 0; }
.slide-pdf-enter-from,   .slide-pdf-leave-to   { transform: translateX(100%);  opacity: 0; }

/* ── Assistant error bubble ───────────────────────────────────
   Surfaces backend / network failures inline in the chat
   instead of a silent empty assistant message. Sits ABOVE the
   AgentMessageBody so any partial trace (tool calls that ran
   before the error) is still visible — the user can see what
   the agent did get done plus exactly what failed. */
.error-bubble {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  margin-bottom: 8px;
  border: 1px solid color-mix(in srgb, #ef4444 35%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, #ef4444 8%, transparent);
  color: var(--color-err-fg, #b91c1c);
  font-size: 12px;
  line-height: 1.55;
}
.error-icon { flex-shrink: 0; margin-top: 2px; }
.error-body { min-width: 0; flex: 1; }
.error-title { font-weight: 600; margin-bottom: 2px; }
.error-detail {
  /* Provider error messages can be long URLs / class paths;
     break anywhere so they don't blow out the bubble width. */
  word-break: break-word;
  white-space: pre-wrap;
  color: var(--color-t2);
}
</style>
