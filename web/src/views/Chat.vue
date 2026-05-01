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
const _streamThinking = ref('')   // reasoning content from V4-Pro / o1 / deepseek-reasoner
const _streamThinkingCollapsed = ref(false)   // default expanded
const _thinkingCollapsed = ref({})   // {msgIndex: boolean} — default expanded for all
const _retInfo = ref(null)
const _livePhases = ref({})
const _liveElapsed = ref({})
const _abortCtrl = ref(null)
const _progressExpanded = ref(false)
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
    const now = Date.now(), obj = {}
    for (const [k, p] of Object.entries(_livePhases.value)) {
      obj[k] = p.status === 'running' ? now - p.t0 : (p.t1 || now) - p.t0
    }
    _liveElapsed.value = obj
  }, 200)
}
function _stopTimer() { if (_timer) { clearInterval(_timer); _timer = null } }
</script>

<script setup>
import { ref, reactive, nextTick, computed, inject, watch, onMounted, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { askQueryStream, createConversation, addMessage, getMessages, filePreviewUrl, fileDownloadUrl, getTrace } from '@/api'

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
import OtelTraceViewer from '@/components/OtelTraceViewer.vue'
import PathScopePicker from '@/components/PathScopePicker.vue'
import ThinkingPicker from '@/components/ThinkingPicker.vue'

const convId = inject('convId')
const loadConvs = inject('loadConvs')

// Path scoping: read `path_filter` from URL (e.g. ?path_filter=/legal).
// User can clear the chip via the Chat UI.
const route = useRoute()
const router = useRouter()

// Per-SPA doc-name cache. Citations carry doc_id only; we resolve the
// current filename here so renames / re-ingests are reflected without
// having to re-query. ``ensure`` fires the fetch lazily; ``docName``
// returns whatever we have right now (re-renders when fetch lands).
const { ensure: ensureDocName, getFilename: docName } = useDocCache()
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

// Bind module-level refs to local names for template access
const msgs = _msgs
const streaming = _streaming
const streamText = _streamText
const streamThinking = _streamThinking
const streamThinkingCollapsed = _streamThinkingCollapsed
const thinkingCollapsed = _thinkingCollapsed
const retInfo = _retInfo
const livePhases = _livePhases
const liveElapsed = _liveElapsed
const abortCtrl = _abortCtrl
const progressExpanded = _progressExpanded
const genTools = _genTools

// Thinking lives inside ``genTools`` (so the API gets one
// ``generation_overrides`` payload), but the UI exposes it through a
// dedicated chip. This adapter reads/writes just the ``thinking``
// field while preserving any other fields a programmatic caller may
// have set (reasoning_effort, temperature, ...).
const thinkingValue = computed({
  get: () => (typeof genTools.value?.thinking === 'boolean' ? genTools.value.thinking : null),
  set: (v) => {
    const next = { ...(genTools.value || {}) }
    if (v === null || v === undefined) delete next.thinking
    else next.thinking = v
    genTools.value = Object.keys(next).length ? next : null
  },
})

// Per-instance state (OK to reset on remount)
const input = ref('')
const chatEl = ref(null)
const thinkingStreamEl = ref(null)   // live thinking pane element — auto-scrolls to bottom

// Keep the live thinking pane scrolled to its latest content while
// reasoning streams in. ``flush: 'post'`` runs after Vue has applied
// the new ``streamThinking`` text to the DOM.
watch(_streamThinking, () => {
  if (!thinkingStreamEl.value) return
  thinkingStreamEl.value.scrollTop = thinkingStreamEl.value.scrollHeight
}, { flush: 'post' })
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

const allPhasesSorted = computed(() =>
  Object.entries(livePhases.value)
    .sort((a, b) => a[1].t0 - b[1].t0)
    .map(([name, p]) => ({ name, ...p }))
)

/** Current summary: what's running right now, as a single sentence */
const progressSummary = computed(() => {
  const running = allPhasesSorted.value.filter(p => p.status === 'running')
  if (!running.length) {
    const done = allPhasesSorted.value.filter(p => p.status === 'done')
    if (done.length) {
      const last = done[done.length - 1]
      return { text: pLabel[last.name] || last.name, done: true, elapsed: liveElapsed.value[last.name] }
    }
    return null
  }
  const names = running.map(p => pLabel[p.name] || p.name)
  // Show longest-running phase's elapsed time
  const maxElapsed = Math.max(...running.map(p => liveElapsed.value[p.name] || 0))
  return { text: names.join(', '), done: false, elapsed: maxElapsed }
})

const pLabel = {
  query_understanding: 'Understanding query',
  query_expansion: 'Expanding queries',
  bm25_path: 'BM25 search',
  vector_path: 'Vector search',
  tree_path: 'Tree navigation',
  kg_path: 'KG traversal',
  rrf_merge: 'Merging',
  expansion: 'Expanding context',
  rerank: 'Reranking',
  citations: 'Building citations',
  generation: 'Generating',
}

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
  livePhases.value = {}; liveElapsed.value = {}
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
      thinking: m.thinking || null,
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
  if (typeof raw[0] === 'object' && raw[0] !== null) return raw
  return raw.map(id => (typeof id === 'string' ? { citation_id: id, _needsEnrich: true } : id))
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

const presetQA = [
  {
    q: 'What is ForgeRAG?',
    a: `**ForgeRAG** is a self-hosted document Q&A system built on one principle: **"Don't just search. Reason. And show me where."**

Most RAG systems treat documents as bags of chunks — they split text into fixed-size fragments, embed them, and hope the nearest vectors contain the answer. This works for simple lookups but falls apart when questions require understanding **where** information lives within a document's structure, or **how** entities relate across multiple documents.

ForgeRAG takes a different approach.

### Structural Tree Reasoning

For every document ingested, ForgeRAG builds a **hierarchical tree** with per-node summaries using LLM-based page-group analysis. Pages are grouped into windows, and an LLM infers logical section boundaries, titles, and one-sentence summaries — all in a single call. Existing TOC and heading signals are passed as hints, but the LLM makes all structural decisions, ensuring reliable trees even for flat documents without headings.

At query time, BM25 and vector search first identify "hot" regions in candidate documents. These hits are **annotated onto the tree outline** as heat-map markers. An LLM then reads the annotated outline (titles + summaries + hit markers) and reasons about which sections are truly relevant and which adjacent sections may also contain answers. This "verify + expand" approach is more accurate than blind exploration because the LLM starts from evidence, not from scratch.

### Knowledge Graph & Multi-hop Reasoning

During ingestion, ForgeRAG uses LLM-based extraction to build a knowledge graph of **entities and relations** from each chunk. At query time, this enables two retrieval modes:

- **Local retrieval** — direct entity chunks + 1-hop and 2-hop neighbors in the graph
- **Global retrieval** — keyword-based entity search across the entire graph

This powers **multi-hop questions** that no keyword or vector search could answer: *"Which suppliers of Apple also supply Samsung?"* — the KG traverses Apple → supplier relations → shared entities → Samsung, discovering connections across documents.

### Dual-Reasoning Retrieval

Every query follows a two-phase retrieval pipeline:

**Phase 1 — Fast Pre-filtering:** BM25 keyword matching and vector semantic search run in parallel to identify candidate documents and "hot" regions.

**Phase 2 — Deep Reasoning:** Two reasoning paths operate on the pre-filtered results:

| Path | What it does |
|------|-------------|
| **Tree Navigation** | LLM reads document outlines annotated with Phase 1 hits, verifies relevance and discovers adjacent sections |
| **KG Traversal** | Graph walk — discovers entity relationships and cross-document links |

Results are merged via **Reciprocal Rank Fusion (RRF)**. When tree navigation is unavailable, BM25/vector results enter RRF as fallback.

### Pixel-precise Citations

Every citation in ForgeRAG's answers carries **page number + bounding box coordinates**. The built-in PDF viewer highlights the exact source region — not just "page 5", but the specific paragraph, table cell, or figure caption the answer was derived from.

### Forge Your RAG — From Simple to Sophisticated

| Level | What you get | Config effort |
|-------|-------------|---------------|
| **Minimal** | BM25 + vector search, SQLite, local storage | Just set an API key |
| **Standard** | + LLM tree navigation, query understanding, reranking | Enable in web UI |
| **Advanced** | + KG extraction, multi-hop reasoning, VLM image enrichment | Toggle features on |
| **Production** | + PostgreSQL/pgvector, S3, Neo4j, Docker one-click deploy | Setup wizard |

Every component is independently toggleable. All settings — LLM providers, retrieval parameters, parsing strategies — live in \`forgerag.yaml\` (or \`myconfig.yaml\` for secrets); edit and restart to change.

Upload PDFs, DOCX, PPTX, XLSX, HTML, or Markdown. Ask questions. Get grounded answers with highlighted source regions you can actually verify.`,
  },
  {
    q: 'vs PageIndex',
    a: `### ForgeRAG vs PageIndex

Both reject the traditional "chunk-and-embed" paradigm in favor of **structure-aware reasoning** — using LLMs to navigate document hierarchies rather than relying solely on vector similarity. PageIndex (by VectifyAI) pioneered vectorless, reasoning-based RAG; ForgeRAG builds on this foundation and extends it significantly.

| Dimension | **ForgeRAG** | **PageIndex** |
|-----------|-------------|---------------|
| **Core Idea** | Dual-reasoning: BM25/vector pre-filter → tree reasoning + KG | Pure reasoning-based: **no vector database**, no chunking |
| **Retrieval** | BM25/vector pre-filter + tree + KG, fused via RRF | LLM tree navigation as the sole retrieval mechanism |
| **Vector Search** | ✅ Pre-filter for tree navigation + fallback path | ❌ Deliberately eliminated — relies entirely on LLM reasoning |
| **Tree Building** | LLM page-group inference with TOC/heading hints + per-node summaries | Hierarchical indexing with node-level summaries |
| **Knowledge Graph** | Built-in entity/relation extraction + multi-hop traversal | None |
| **Multi-hop** | Cross-document entity connections via KG graph traversal | Within-document structural navigation only |
| **Citation** | Pixel-level bbox highlighting on PDF | Page / section level references |
| **Deployment** | Web UI + REST API + Docker one-click deploy | Framework / SDK for integration |
| **Performance** | Balanced latency (parallel paths, vector for speed) | Higher latency per query (LLM reasoning at every step) |
| **Best For** | General-purpose document Q&A with full-stack deployment | Structured professional documents (finance, legal) where hierarchy is paramount |

**Key insight:** PageIndex proves that LLM reasoning over document structure can outperform vector similarity (98.7% on FinanceBench). ForgeRAG incorporates this insight in its tree navigation path, but takes a different approach: BM25/vector pre-filter provides "hot region" hints that make the LLM's structural reasoning faster and more accurate than cold-start exploration. Even for flat documents without headings, the LLM infers section structure during indexing, making tree navigation universally applicable.`,
  },
  {
    q: 'vs GraphRAG',
    a: `### ForgeRAG vs GraphRAG (Microsoft)

Both use **knowledge graphs** to enhance RAG, but their architectural philosophies are fundamentally different:

| Dimension | **ForgeRAG** | **GraphRAG (Microsoft)** |
|-----------|-------------|--------------------------|
| **Retrieval Strategy** | BM25/vector pre-filter → tree reasoning + KG, fused via RRF | Graph-centric: community summaries + entities |
| **Role of the Graph** | One of two primary reasoning paths (alongside tree nav), fused via RRF | Core and only retrieval mechanism |
| **Document Structure** | LLM-built hierarchical tree with per-node summaries, heat-map guided navigation | No document structure awareness |
| **Graph Construction** | Entity + relation extraction (per chunk, incremental) | Entity + relation + community detection + hierarchical summaries |
| **Citations** | Pixel-level bbox, pinpoints exact document region | Community summary level, no precise location |
| **Incremental Updates** | Document-level incremental, no full index rebuild | Requires rebuilding entire graph + communities |
| **Configurability** | Every component independently toggleable, runtime hot-reload | Relatively fixed pipeline |
| **Resource Cost** | Enable KG on demand — zero cost if unused | Must build full graph + community summaries upfront |

**Key Differences:**

1. **Complementary vs Dependent** — ForgeRAG's graph is one of two reasoning paths (alongside tree navigation); turn it off and retrieval still works via tree + BM25/vector fallback. GraphRAG's graph is the core; no graph, no retrieval
2. **Structure + Semantics** — ForgeRAG understands both document structure (tree) and semantic relations (graph); GraphRAG has only the semantic graph
3. **Precise Citations** — ForgeRAG traces back to the exact location in a document (pixel-level); GraphRAG traces to community summaries
4. **Opt-in graph construction** — ForgeRAG's KG extraction is off by default (toggle \`retrieval.kg_extraction.enabled\`); GraphRAG requires full graph construction before any query`,
  },
]

async function send(text) {
  const q = (text || input.value).trim(); if (!q || streaming.value) return; input.value = ''
  if (!convId.value) try {
    _skipNextWatch = true  // prevent watch from resetting UI on convId change
    convId.value = (await createConversation(q.slice(0, 60))).conversation_id
    loadConvs()  // refresh sidebar immediately so the new conversation is visible
  } catch { _skipNextWatch = false }
  msgs.value.push({ role: 'user', content: q })

  // Check if this is a preset question — simulate fast streaming + persist
  const preset = presetQA.find(p => p.q === q)
  if (preset) {
    const myGenId = ++_presetGenId
    const cid = convId.value
    streaming.value = true; streamText.value = ''
    scroll()
    const text = preset.a
    const step = 8
    for (let i = 0; i < text.length; i += step) {
      if (myGenId !== _presetGenId) return  // cancelled by navigation
      streamText.value = text.slice(0, i + step)
      scroll()
      await new Promise(r => setTimeout(r, 18))
    }
    if (myGenId !== _presetGenId) return  // cancelled
    streamText.value = ''
    msgs.value.push({ role: 'assistant', content: text, citations: null })
    streaming.value = false
    scroll()
    // Persist both messages to backend
    try {
      await addMessage(cid, 'user', q)
      await addMessage(cid, 'assistant', text)
      loadConvs()
    } catch {}
    return
  }

  streaming.value = true; streamText.value = ''; streamThinking.value = ''; retInfo.value = null
  livePhases.value = {}; liveElapsed.value = {}; progressExpanded.value = false
  startTimer(); scroll()

  const myGenId = ++_streamGenId
  abortCtrl.value = new AbortController()
  try {
    let fin = null
    let traceSpans = null   // OTel spans payload from the "trace" SSE event
    for await (const { event, data } of askQueryStream({
      query: q,
      conversationId: convId.value,
      pathFilter: pathFilter.value || null,
      generationOverrides: genTools.value,
      signal: abortCtrl.value.signal,
    })) {
      // If conversation switched away, stop updating UI but don't abort request
      if (myGenId !== _streamGenId) break
      if (event === 'progress') {
        const { phase, status, detail } = data
        const now = Date.now()
        const ex = livePhases.value[phase]
        if (status === 'running') {
          livePhases.value = { ...livePhases.value, [phase]: { status: 'running', detail, t0: now } }
        } else {
          livePhases.value = { ...livePhases.value, [phase]: { status: 'done', detail, t0: ex?.t0 || now, t1: now } }
        }
        scroll()
      } else if (event === 'retrieval') { retInfo.value = data }
      else if (event === 'thinking') { streamThinking.value += data.text; scroll() }
      else if (event === 'delta') { streamText.value += data.text; scroll() }
      else if (event === 'trace') { traceSpans = data }   // OTel {spans: [...]}
      else if (event === 'error') {
        const errMsg = typeof data === 'string' ? data : (data?.error || data?.detail || 'Unknown error')
        fin = { text: `Error: ${errMsg}`, citations_used: null, stats: null, trace_id: null }
      } else if (event === 'done') {
        fin = data
        if (livePhases.value.generation) {
          livePhases.value = { ...livePhases.value, generation: { ...livePhases.value.generation, status: 'done', t1: Date.now() } }
        }
      }
    }
    if (fin && myGenId === _streamGenId) {
      msgs.value.push({
        role: 'assistant',
        content: fin.text,
        thinking: streamThinking.value || (fin.stats?.reasoning_text || ''),
        citations: fin.citations_used,
        stats: fin.stats,
        trace: traceSpans,
        traceId: fin.trace_id || null,
      })
      streamText.value = ''
      streamThinking.value = ''
    }
  } catch (e) {
    // AbortError from user clicking stop — not an error
    if (e.name !== 'AbortError' && myGenId === _streamGenId) {
      msgs.value.push({ role: 'assistant', content: `Error: ${e.message}` })
    }
    streamText.value = ''
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
function fmtSec(ms) { return ms == null ? '' : ms < 1000 ? '<1s' : Math.round(ms / 1000) + 's' }

/* ── PDF ── */
const activeCiteId = ref(null)    // citation_id for legacy compat
const activeChunkId = ref(null)   // chunk_id — the real identity

function onCiteClick(c) {
  if (!c?.file_id) return

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

  // Download URLs
  const dlUrl = fileDownloadUrl(c.file_id)
  const srcUrl = c.source_file_id ? fileDownloadUrl(c.source_file_id) : ''
  const srcLabel = c.source_format ? c.source_format.toUpperCase() : ''

  Object.assign(pdf, {
    show: true,
    url: filePreviewUrl(c.file_id),
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
  const map = buildCiteDisplayMap(m.content || '', cites)
  return [...cites].sort((a, b) => {
    const pa = map[a.citation_id] ?? 999_999
    const pb = map[b.citation_id] ?? 999_999
    return pa - pb
  })
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

      <!-- EMPTY STATE -->
      <div v-if="empty" class="flex-1 flex flex-col">
        <div class="flex-[3]"></div>
        <div class="pl-8 pr-16">
          <div class="max-w-2xl mx-auto text-center">
            <h1 class="wordmark text-[32px] mb-2">ForgeRAG</h1>
            <p class="text-sm text-t3 mb-8">Multi-path fusion · Tree reasoning · Knowledge graph · Pixel-precise citations</p>
            <div class="flex flex-wrap justify-center gap-2 mb-10">
              <button v-for="p in presetQA" :key="p.q" @click="send(p.q)"
                class="px-4 py-2 rounded-full border border-line text-xs text-t2 hover:bg-bg3 hover:border-line2 transition-colors"
              >{{ p.q }}</button>
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
              <ThinkingPicker v-model="thinkingValue" />
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
        <div ref="chatEl" class="flex-1 overflow-y-auto pl-6 pr-14 py-6">
          <div class="max-w-2xl mx-auto space-y-4">
            <div v-for="(m, i) in msgs" :key="i" class="fadein">
              <!-- User -->
              <div v-if="m.role === 'user'" class="flex justify-end mb-2">
                <div class="px-4 py-2.5 rounded-2xl text-sm bg-bg3 text-t1 max-w-[75%]">{{ m.content }}</div>
              </div>
              <!-- Assistant -->
              <div v-else class="group mb-2">
                <!-- Thinking pane (reasoning models like V4-Pro / o1)
                     above the answer — chronological order, reasoning
                     came first. Capped at ~140px with internal scroll. -->
                <div v-if="m.thinking" class="mb-3 border-l-2 border-line pl-3 max-w-[90%]">
                  <button class="text-[11px] text-t3 hover:text-t2 flex items-center gap-1 mb-1.5"
                    @click="thinkingCollapsed[i] = !thinkingCollapsed[i]">
                    <svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor">
                      <path fill-rule="evenodd" clip-rule="evenodd" d="M9.97165 1.29981C11.5853 0.718916 13.271 0.642197 14.3144 1.68555C15.3577 2.72902 15.2811 4.41466 14.7002 6.02833C14.4707 6.66561 14.1504 7.32937 13.75 8.00001C14.1504 8.67062 14.4707 9.33444 14.7002 9.97169C15.2811 11.5854 15.3578 13.271 14.3144 14.3145C13.271 15.3579 11.5854 15.2811 9.97165 14.7002C9.3344 14.4708 8.67059 14.1505 7.99997 13.75C7.32933 14.1505 6.66558 14.4708 6.02829 14.7002C4.41461 15.2811 2.72899 15.3578 1.68552 14.3145C0.642155 13.271 0.71887 11.5854 1.29977 9.97169C1.52915 9.33454 1.84865 8.67049 2.24899 8.00001C1.84866 7.32953 1.52915 6.66544 1.29977 6.02833C0.718852 4.41459 0.64207 2.729 1.68552 1.68555C2.72897 0.642112 4.41456 0.718887 6.02829 1.29981C6.66541 1.52918 7.32949 1.8487 7.99997 2.24903C8.67045 1.84869 9.33451 1.52919 9.97165 1.29981ZM12.9404 9.2129C12.4391 9.893 11.8616 10.5681 11.2148 11.2149C10.568 11.8616 9.89296 12.4391 9.21286 12.9404C9.62532 13.1579 10.0271 13.338 10.4121 13.4766C11.9146 14.0174 12.9172 13.8738 13.3955 13.3955C13.8737 12.9173 14.0174 11.9146 13.4765 10.4121C13.3379 10.0271 13.1578 9.62535 12.9404 9.2129ZM3.05856 9.2129C2.84121 9.62523 2.66197 10.0272 2.52341 10.4121C1.98252 11.9146 2.12627 12.9172 2.60446 13.3955C3.08278 13.8737 4.08544 14.0174 5.58786 13.4766C5.97264 13.338 6.37389 13.1577 6.7861 12.9404C6.10624 12.4393 5.43168 11.8614 4.78513 11.2149C4.13823 10.5679 3.55992 9.89313 3.05856 9.2129ZM7.99899 3.792C7.23179 4.31419 6.45306 4.95512 5.70407 5.70411C4.95509 6.45309 4.31415 7.23184 3.79196 7.99903C4.3143 8.76666 4.95471 9.54653 5.70407 10.2959C6.45309 11.0449 7.23271 11.6848 7.99997 12.207C8.76725 11.6848 9.54683 11.0449 10.2959 10.2959C11.0449 9.54686 11.6848 8.76729 12.207 8.00001C11.6848 7.23275 11.0449 6.45312 10.2959 5.70411C9.5465 4.95475 8.76662 4.31434 7.99899 3.792ZM5.58786 2.52344C4.08533 1.98255 3.08272 2.12625 2.60446 2.6045C2.12621 3.08275 1.98252 4.08536 2.52341 5.5879C2.66189 5.97253 2.8414 6.37409 3.05856 6.78614C3.55983 6.10611 4.1384 5.43189 4.78513 4.78516C5.43186 4.13843 6.10606 3.55987 6.7861 3.0586C6.37405 2.84144 5.97249 2.66192 5.58786 2.52344ZM13.3955 2.6045C12.9172 2.12631 11.9146 1.98257 10.4121 2.52344C10.0272 2.66201 9.62519 2.84125 9.21286 3.0586C9.8931 3.55996 10.5679 4.13827 11.2148 4.78516C11.8614 5.43172 12.4392 6.10627 12.9404 6.78614C13.1577 6.37393 13.338 5.97267 13.4765 5.5879C14.0174 4.08549 13.8736 3.08281 13.3955 2.6045Z"/>
                    </svg>
                    Thinking
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                      class="ml-0.5 transition-transform" :class="thinkingCollapsed[i] ? '-rotate-90' : ''">
                      <path d="M6 9l6 6 6-6"/>
                    </svg>
                  </button>
                  <div v-if="!thinkingCollapsed[i]"
                    class="text-[12px] text-t3 leading-6 whitespace-pre-wrap max-h-[140px] overflow-y-auto pr-2">{{ m.thinking }}</div>
                </div>
                <div class="msg-body text-sm leading-7 text-t1 max-w-[90%]"
                  v-html="renderMsg(m.content, m.citations)"
                  @click="onMsgClick($event, m.citations)">
                </div>
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
                  <button v-for="(c, ci) in orderedCitations(m)" :key="c.citation_id || ci"
                    class="flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[10px] transition-colors"
                    :class="activeChunkId === c.chunk_id
                      ? 'border-brand bg-brand/10 text-brand'
                      : 'border-line text-t2 hover:bg-bg3'"
                    @click="onCiteClick(c)">
                    <span class="font-medium" :class="activeChunkId === c.chunk_id ? '' : 'text-brand'">[{{ ci + 1 }}]</span>
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

            <!-- ═══ Streaming: lightweight progress ═══ -->
            <div v-if="streaming" class="fadein">
              <div v-if="!streamText" class="text-[12px] text-t3 leading-6">
                <!-- No phases yet -->
                <template v-if="!Object.keys(livePhases).length">
                  <Spinner size="sm" />
                </template>

                <template v-else>
                  <!-- Summary line (always visible): "Searching... 3.2s ▾".
                       The old "Thinking:" prefix collided with the actual
                       Thinking pane below — they looked identical despite
                       meaning different things ("model is in Generating
                       phase" vs "the model's reasoning_content"). Drop
                       the prefix, the phase name + spinner are enough. -->
                  <span v-if="progressSummary && !progressSummary.done" class="text-t2">
                    {{ progressSummary.text }}...
                    <span class="text-t3 text-[11px] ml-1.5">{{ fmtSec(progressSummary.elapsed) }}</span>
                  </span>
                  <span v-else-if="progressSummary && progressSummary.done" class="text-t3">
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" class="inline -mt-px mr-0.5 text-t1">
                      <path d="M20 6L9 17l-5-5" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                    {{ progressSummary.text }}
                    <span class="text-[11px] ml-1.5">{{ fmtSec(progressSummary.elapsed) }}</span>
                  </span>

                  <!-- Expand toggle -->
                  <button class="ml-1 text-t3/50 hover:text-t3 align-middle" @click="progressExpanded = !progressExpanded">
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                      class="inline transition-transform" :class="progressExpanded ? 'rotate-180' : ''">
                      <path d="M6 9l6 6 6-6"/>
                    </svg>
                  </button>

                  <!-- Expanded detail (each phase as inline text) -->
                  <div v-if="progressExpanded" class="mt-1 text-[11px] text-t3 leading-5">
                    <div v-for="p in allPhasesSorted" :key="p.name" class="phase-in">
                      <template v-if="p.status === 'done'">
                        <svg width="9" height="9" viewBox="0 0 24 24" fill="none" class="inline -mt-px mr-0.5 text-t1">
                          <path d="M20 6L9 17l-5-5" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                        {{ pLabel[p.name] || p.name }}
                        <span v-if="p.detail" class="text-t3/50 ml-1">{{ p.detail }}</span>
                        <span class="text-t3/40 text-[10px] ml-1.5">{{ fmtSec(liveElapsed[p.name]) }}</span>
                      </template>
                      <template v-else>
                        <Spinner size="xs" class="mr-0.5 -mt-px" />
                        <span class="text-t2">{{ pLabel[p.name] || p.name }}</span>
                        <span class="text-t3/40 text-[10px] ml-1.5">{{ fmtSec(liveElapsed[p.name]) }}</span>
                      </template>
                    </div>
                  </div>
                </template>
              </div>

              <!-- Live thinking pane (reasoning models stream this).
                   Rendered ABOVE the answer to match the persisted layout
                   below — otherwise the pane jumps from below-answer (live)
                   to above-answer (history) the moment the SSE ``done``
                   event flips the message into ``msgs[]``. Chronological
                   order anyway: the model thinks first, then answers. -->
              <div v-if="streamThinking" class="mb-3 border-l-2 border-line pl-3">
                <button class="text-[11px] text-t3 hover:text-t2 flex items-center gap-1 mb-1.5"
                  @click="streamThinkingCollapsed = !streamThinkingCollapsed">
                  <svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor">
                    <path fill-rule="evenodd" clip-rule="evenodd" d="M9.97165 1.29981C11.5853 0.718916 13.271 0.642197 14.3144 1.68555C15.3577 2.72902 15.2811 4.41466 14.7002 6.02833C14.4707 6.66561 14.1504 7.32937 13.75 8.00001C14.1504 8.67062 14.4707 9.33444 14.7002 9.97169C15.2811 11.5854 15.3578 13.271 14.3144 14.3145C13.271 15.3579 11.5854 15.2811 9.97165 14.7002C9.3344 14.4708 8.67059 14.1505 7.99997 13.75C7.32933 14.1505 6.66558 14.4708 6.02829 14.7002C4.41461 15.2811 2.72899 15.3578 1.68552 14.3145C0.642155 13.271 0.71887 11.5854 1.29977 9.97169C1.52915 9.33454 1.84865 8.67049 2.24899 8.00001C1.84866 7.32953 1.52915 6.66544 1.29977 6.02833C0.718852 4.41459 0.64207 2.729 1.68552 1.68555C2.72897 0.642112 4.41456 0.718887 6.02829 1.29981C6.66541 1.52918 7.32949 1.8487 7.99997 2.24903C8.67045 1.84869 9.33451 1.52919 9.97165 1.29981ZM12.9404 9.2129C12.4391 9.893 11.8616 10.5681 11.2148 11.2149C10.568 11.8616 9.89296 12.4391 9.21286 12.9404C9.62532 13.1579 10.0271 13.338 10.4121 13.4766C11.9146 14.0174 12.9172 13.8738 13.3955 13.3955C13.8737 12.9173 14.0174 11.9146 13.4765 10.4121C13.3379 10.0271 13.1578 9.62535 12.9404 9.2129ZM3.05856 9.2129C2.84121 9.62523 2.66197 10.0272 2.52341 10.4121C1.98252 11.9146 2.12627 12.9172 2.60446 13.3955C3.08278 13.8737 4.08544 14.0174 5.58786 13.4766C5.97264 13.338 6.37389 13.1577 6.7861 12.9404C6.10624 12.4393 5.43168 11.8614 4.78513 11.2149C4.13823 10.5679 3.55992 9.89313 3.05856 9.2129ZM7.99899 3.792C7.23179 4.31419 6.45306 4.95512 5.70407 5.70411C4.95509 6.45309 4.31415 7.23184 3.79196 7.99903C4.3143 8.76666 4.95471 9.54653 5.70407 10.2959C6.45309 11.0449 7.23271 11.6848 7.99997 12.207C8.76725 11.6848 9.54683 11.0449 10.2959 10.2959C11.0449 9.54686 11.6848 8.76729 12.207 8.00001C11.6848 7.23275 11.0449 6.45312 10.2959 5.70411C9.5465 4.95475 8.76662 4.31434 7.99899 3.792ZM5.58786 2.52344C4.08533 1.98255 3.08272 2.12625 2.60446 2.6045C2.12621 3.08275 1.98252 4.08536 2.52341 5.5879C2.66189 5.97253 2.8414 6.37409 3.05856 6.78614C3.55983 6.10611 4.1384 5.43189 4.78513 4.78516C5.43186 4.13843 6.10606 3.55987 6.7861 3.0586C6.37405 2.84144 5.97249 2.66192 5.58786 2.52344ZM13.3955 2.6045C12.9172 2.12631 11.9146 1.98257 10.4121 2.52344C10.0272 2.66201 9.62519 2.84125 9.21286 3.0586C9.8931 3.55996 10.5679 4.13827 11.2148 4.78516C11.8614 5.43172 12.4392 6.10627 12.9404 6.78614C13.1577 6.37393 13.338 5.97267 13.4765 5.5879C14.0174 4.08549 13.8736 3.08281 13.3955 2.6045Z"/>
                  </svg>
                  Thinking
                  <span v-if="!streamText" class="text-t3/50 ml-0.5 inline-flex items-center gap-1"><Spinner size="xs" /></span>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                    class="ml-0.5 transition-transform" :class="streamThinkingCollapsed ? '-rotate-90' : ''">
                    <path d="M6 9l6 6 6-6"/>
                  </svg>
                </button>
                <div v-if="!streamThinkingCollapsed"
                  ref="thinkingStreamEl"
                  class="text-[12px] text-t3 leading-6 whitespace-pre-wrap max-h-[140px] overflow-y-auto pr-2">{{ streamThinking }}</div>
              </div>

              <!-- Streaming text (rendered after thinking — same order as
                   the persisted assistant message above). -->
              <div v-if="streamText" class="msg-body text-sm leading-7 text-t1">
                <span v-html="renderStream(streamText)"></span><span class="inline-block w-0.5 h-4 ml-0.5 bg-brand animate-pulse rounded-sm"></span>
              </div>
            </div>
          </div>
        </div>

        <!-- Bottom input -->
        <div class="pl-6 pr-14 pb-4 border-t border-line bg-bg">
          <div class="max-w-2xl mx-auto pt-3">
            <!-- Scope + Tools chips above the input. -->
            <div class="mb-1.5 pl-1 flex items-center gap-1.5">
              <PathScopePicker v-model="pathFilter" />
              <ThinkingPicker v-model="thinkingValue" />
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


.phase-in { animation: phaseIn .15s ease; }
@keyframes phaseIn { from { opacity: 0; } to { opacity: 1; } }

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
</style>
