<template>
  <div v-if="!hasSpans" class="px-4 py-6 text-[11px] text-t3">
    No trace data.
  </div>
  <div v-else class="otel-viewer">
    <!-- Summary header -->
    <div class="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-t3 px-4 py-3 border-b border-line">
      <span><b class="text-t2">{{ totalMs.toFixed(0) }}</b>ms total</span>
      <span><b class="text-t2">{{ spans.length }}</b> spans</span>
      <span v-if="llmCalls"><b class="text-t2">{{ llmCalls }}</b> LLM calls</span>
      <span v-if="totalTokens"><b class="text-t2">{{ totalTokens }}</b> tokens</span>
      <span v-if="errorCount" class="text-rose-500"><b>{{ errorCount }}</b> error{{ errorCount > 1 ? 's' : '' }}</span>
      <span v-if="untracedMs >= 50" class="text-t3">
        <b class="text-t2">{{ untracedMs.toFixed(0) }}</b>ms untraced
      </span>
    </div>

    <!-- Body: phase groups -->
    <div
      ref="bodyEl"
      class="overflow-y-auto flex-1 px-2 py-2 relative"
      @mousemove="onMouseMove"
      @mouseleave="cursorX = null"
    >
      <!-- Hover guide line + time tooltip.
           ``top``/``height`` are bound to the body's CURRENT scroll
           viewport (not its content extent), so the line spans only
           the visible area regardless of scroll position. The tooltip
           follows the cursor's Y inside the line — Chrome DevTools
           Performance pattern, "look right where my eye is". -->
      <div
        v-if="cursorX != null"
        class="cursor-guide"
        :style="{
          left: cursorX + 'px',
          top: scrollTop + 'px',
          height: visibleHeight + 'px',
        }"
      >
        <div
          class="cursor-tooltip"
          :style="{ top: tooltipOffsetY + 'px' }"
        >{{ cursorMs.toFixed(0) }}ms</div>
      </div>

      <!-- Phase groups (collapsed by default) -->
      <div
        v-for="phase in phaseGroups"
        :key="phase.name"
        class="phase-group"
      >
        <button
          type="button"
          class="phase-header"
          :class="{ 'is-empty': !phase.spans.length, 'is-untraced': phase.untraced }"
          @click="togglePhase(phase.name)"
        >
          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2.5" class="phase-chevron"
            :class="{ 'is-open': !collapsed[phase.name] }">
            <path d="M9 6l6 6-6 6"/>
          </svg>
          <span class="phase-label">{{ phase.label }}</span>
          <span class="phase-count" v-if="phase.spans.length">{{ phase.spans.length }}</span>
          <span class="phase-duration tabular-nums">{{ phase.duration_ms.toFixed(0) }}ms</span>
          <div class="bar-track phase-bar-track">
            <div
              class="bar-fill"
              :class="phase.untraced ? 'bar-fill--untraced' : 'bar-fill--forgerag'"
              :style="{ left: phase.offsetPct + '%', width: phase.widthPct + '%' }"
            />
          </div>
        </button>
        <div v-if="!collapsed[phase.name] && phase.spans.length" class="phase-children">
          <div
            v-for="row in phase.spans"
            :key="row.span_id"
            class="span-row"
            :class="{
              'is-selected': selectedId === row.span_id,
              'is-ancestor': ancestorIds.has(row.span_id) && selectedId !== row.span_id,
              'is-dimmed': selectedId && !ancestorIds.has(row.span_id) && selectedId !== row.span_id,
            }"
            @click="onRowClick(row)"
          >
            <div class="flex items-center text-[10px]" :style="{ paddingLeft: row.depth * 10 + 'px' }">
              <span class="tree-hinge" v-if="row.depth > 0">└</span>
              <span class="span-tag" :class="`span-tag--${row.category}`">{{ row.shortName }}</span>
              <span class="span-full-name truncate">{{ row.displayName }}</span>
              <span class="ml-auto pl-2 text-t3 font-mono tabular-nums text-[9px]">{{ row.duration_ms.toFixed(1) }}ms</span>
            </div>
            <div class="bar-track">
              <div
                class="bar-fill"
                :class="`bar-fill--${row.category}`"
                :style="{ left: row.offsetPct + '%', width: row.widthPct + '%' }"
              />
            </div>
            <div v-if="row.llmInfo" class="text-[9px] text-t3 mt-0.5" :style="{ paddingLeft: row.depth * 10 + 14 + 'px' }">
              <span class="font-mono">{{ row.llmInfo.model }}</span>
              <span v-if="row.llmInfo.in_tokens" class="ml-2">in <b>{{ row.llmInfo.in_tokens }}</b></span>
              <span v-if="row.llmInfo.out_tokens" class="ml-2">out <b>{{ row.llmInfo.out_tokens }}</b></span>
              <span v-if="row.llmInfo.cost" class="ml-2">${{ row.llmInfo.cost.toFixed(4) }}</span>
            </div>
            <div v-if="row.isError" class="text-[9px] text-rose-500 mt-0.5" :style="{ paddingLeft: row.depth * 10 + 14 + 'px' }">
              ⚠ {{ row.errorSummary }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Detail pane (attributes) -->
    <div v-if="selectedRow" class="flex-none border-t border-line px-4 py-2 max-h-60 overflow-y-auto">
      <div class="text-[9px] text-t3 uppercase tracking-wider mb-1">{{ selectedRow.name }} · attributes</div>
      <div class="space-y-px text-[10px]">
        <div v-for="(v, k) in selectedRow.attributes" :key="k" class="flex gap-2 leading-tight">
          <span class="text-t3 font-mono shrink-0">{{ k }}</span>
          <span class="text-t2 font-mono break-all">{{ formatValue(v) }}</span>
        </div>
        <div v-if="selectedRow.events?.length" class="mt-2 pt-2 border-t border-line/50">
          <div class="text-[9px] text-t3 uppercase tracking-wider mb-1">events</div>
          <div v-for="(ev, i) in selectedRow.events" :key="i" class="text-[9px] mb-1">
            <span class="text-t2 font-medium">{{ ev.name }}</span>
            <span v-for="(v, k) in ev.attributes" :key="k" class="ml-2 text-t3">
              {{ k }}=<span class="text-t2">{{ formatValue(v) }}</span>
            </span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * OtelTraceViewer — renders OTel spans as a phase-grouped waterfall.
 *
 * Top-level layout: 4 (or 5) collapsible phase groups based on the
 * direct children of ``forgerag.answer``:
 *
 *   ▶ Setup           (forgerag.setup + any pre-pipeline DB/HTTP spans)
 *   ▶ Understanding   (forgerag.query_understanding + descendants)
 *   ▶ Retrieval       (forgerag.retrieve + all retriever subspans)
 *   ▶ Generation      (forgerag.prompt_build + forgerag.generation + ...)
 *   ▶ Untraced        synthetic — only shown when root.duration ≠ Σ children
 *
 * Each phase header is its own waterfall bar (start/end derived from the
 * union of its member spans). Click to expand → individual spans render
 * with the same waterfall-row layout as before. Default state is all
 * phases collapsed; the user's choices persist in localStorage.
 *
 * Hover anywhere in the body → a vertical guide line + ms readout
 * floats at the cursor (Chrome DevTools-style). Beats a fixed top
 * ruler since it surfaces only when needed and works at every Y.
 *
 * Click a span row → its parent chain is highlighted (``.is-ancestor``)
 * and unrelated rows dim (``.is-dimmed``), so the user can see "this
 * leaf belongs to that branch" without re-reading indentation.
 */
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'

const props = defineProps({
  trace: { type: Object, default: null },
})

const spans = computed(() => props.trace?.spans || [])
const hasSpans = computed(() => spans.value.length > 0)

/** Find root span (no parent), compute total duration */
const root = computed(() => spans.value.find(s => !s.parent_span_id) || null)
const totalMs = computed(() => root.value?.duration_ms || 0)
const rootStartNs = computed(() => root.value?.start_time_unix_nano || 0)

// ── Categorisation / display helpers (unchanged from prior version) ──

function categorise(span) {
  const n = span.name || ''
  if (n.startsWith('forgerag.')) return 'forgerag'
  if (span.attributes?.['gen_ai.system'] || /litellm|openai|completion|embedding|rerank/i.test(n)) {
    return 'llm'
  }
  if (/^(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|connect)\b/i.test(n)) return 'db'
  if (/^HTTP |^GET |^POST /.test(n) || span.attributes?.['http.method']) return 'http'
  return 'other'
}
function shortTag(category) {
  return { forgerag: 'fr', llm: 'ai', db: 'db', http: 'http', other: '·' }[category]
}
function displayName(name) { return name.replace(/^forgerag\./, '') }

// ── Phase boundaries — name-based with start-time fallback ──

const PHASES = [
  { name: 'setup',         label: 'Setup',         match: /^forgerag\.setup$/ },
  { name: 'understanding', label: 'Understanding', match: /^forgerag\.query_understanding$/ },
  { name: 'retrieval',     label: 'Retrieval',     match: /^forgerag\.retrieve$/ },
  { name: 'generation',    label: 'Generation',    match: /^forgerag\.(prompt_build|generation)$/ },
]

/** All rows DFS-ordered with depth + waterfall offsets. */
const allRows = computed(() => {
  if (!hasSpans.value) return []
  const byId = new Map(spans.value.map(s => [s.span_id, s]))
  const childrenOf = new Map()
  for (const s of spans.value) {
    const p = s.parent_span_id
    if (!childrenOf.has(p)) childrenOf.set(p, [])
    childrenOf.get(p).push(s)
  }
  for (const [, list] of childrenOf) {
    list.sort((a, b) => (a.start_time_unix_nano || 0) - (b.start_time_unix_nano || 0))
  }
  const total = totalMs.value || 1
  const baseNs = rootStartNs.value
  const out = []
  const roots = spans.value.filter(s => !s.parent_span_id || !byId.has(s.parent_span_id))

  function walk(span, depth) {
    const cat = categorise(span)
    const startMs = Math.max(0, ((span.start_time_unix_nano || 0) - baseNs) / 1e6)
    out.push({
      span_id: span.span_id,
      parent_span_id: span.parent_span_id,
      name: span.name,
      displayName: displayName(span.name),
      shortName: shortTag(cat),
      category: cat,
      depth,
      duration_ms: span.duration_ms || 0,
      startMs,
      offsetPct: total > 0 ? (startMs / total) * 100 : 0,
      widthPct: total > 0 ? Math.max(0.4, ((span.duration_ms || 0) / total) * 100) : 0,
      attributes: span.attributes || {},
      events: span.events || [],
      llmInfo: extractLLMInfo(span),
      isError: span.status?.code === 'ERROR' || hasErrorEvent(span),
      errorSummary: errorSummary(span),
    })
    for (const c of childrenOf.get(span.span_id) || []) walk(c, depth + 1)
  }
  for (const r of roots) walk(r, 0)
  return out
})

/** Group rows into phases. Children of forgerag.answer get bucketed by
 * name match; their descendants follow the same bucket. Top-level
 * non-forgerag spans (auto-instrumented DB/HTTP that the runtime hung
 * directly off root) attach to whichever phase is currently "open"
 * by start-time order — i.e. they belong to the phase they ran during. */
const phaseGroups = computed(() => {
  if (!allRows.value.length) return []

  // Index: span_id → which top-level phase it belongs to.
  const phaseOf = new Map()    // span_id → phase.name
  const rootSpan = root.value
  if (!rootSpan) return []

  // Step 1: top-level forgerag spans match a phase regex; everything
  // descended from them inherits the phase.
  const byParent = new Map()
  for (const s of spans.value) {
    const p = s.parent_span_id || ''
    if (!byParent.has(p)) byParent.set(p, [])
    byParent.get(p).push(s)
  }

  function inheritPhase(spanId, phaseName) {
    phaseOf.set(spanId, phaseName)
    for (const c of byParent.get(spanId) || []) inheritPhase(c.span_id, phaseName)
  }

  // Track top-level forgerag phases ordered by start time so we can
  // bucket loose top-level DB/HTTP spans by which phase was active.
  const topLevel = (byParent.get(rootSpan.span_id) || [])
    .slice().sort((a, b) => (a.start_time_unix_nano || 0) - (b.start_time_unix_nano || 0))
  const phaseStarts = []     // [{name, startNs, endNs}]
  for (const s of topLevel) {
    const def = PHASES.find(p => p.match.test(s.name))
    if (def) {
      inheritPhase(s.span_id, def.name)
      phaseStarts.push({
        name: def.name,
        startNs: s.start_time_unix_nano || 0,
        endNs: s.end_time_unix_nano || 0,
      })
    }
  }

  // Step 2: top-level non-forgerag spans (DB / HTTP / connect) — bucket
  // by which phase they ran during. If they ran before any phase
  // started, use the first phase ("setup" usually). After the last
  // phase ended, attach to the last phase.
  for (const s of topLevel) {
    if (phaseOf.has(s.span_id)) continue
    const start = s.start_time_unix_nano || 0
    let chosen = null
    for (const ph of phaseStarts) {
      if (start >= ph.startNs && start <= ph.endNs) { chosen = ph.name; break }
    }
    if (!chosen) {
      // pick closest phase by start time
      if (!phaseStarts.length) continue
      chosen = phaseStarts.reduce((best, ph) =>
        Math.abs(ph.startNs - start) < Math.abs(best.startNs - start) ? ph : best,
        phaseStarts[0]).name
    }
    inheritPhase(s.span_id, chosen)
  }

  // Step 3: build phase groups
  const groups = PHASES.map(p => ({
    name: p.name, label: p.label, untraced: false,
    spans: [], duration_ms: 0, offsetPct: 0, widthPct: 0,
    earliestNs: Infinity, latestNs: 0,
  }))
  const groupBy = new Map(groups.map(g => [g.name, g]))

  for (const row of allRows.value) {
    if (row.span_id === rootSpan.span_id) continue   // skip the root itself
    const ph = phaseOf.get(row.span_id)
    if (!ph) continue
    const g = groupBy.get(ph)
    if (!g) continue
    g.spans.push(row)
    const span = spans.value.find(s => s.span_id === row.span_id)
    if (!span) continue
    g.earliestNs = Math.min(g.earliestNs, span.start_time_unix_nano || Infinity)
    g.latestNs   = Math.max(g.latestNs,   span.end_time_unix_nano   || 0)
  }

  // Compute waterfall bars per phase (wall-clock span of its members)
  const total = totalMs.value || 1
  const baseNs = rootStartNs.value
  for (const g of groups) {
    if (!g.spans.length) continue
    g.duration_ms = (g.latestNs - g.earliestNs) / 1e6
    const startMs = Math.max(0, (g.earliestNs - baseNs) / 1e6)
    g.offsetPct = total > 0 ? (startMs / total) * 100 : 0
    g.widthPct  = total > 0 ? Math.max(0.4, (g.duration_ms / total) * 100) : 0
  }

  // Step 4: untraced synthetic group — only shown if non-trivial gap
  const sumChildrenMs = groups.reduce((sum, g) => sum + g.duration_ms, 0)
  const gap = (totalMs.value || 0) - sumChildrenMs
  if (gap >= 50) {
    groups.push({
      name: 'untraced', label: 'Untraced', untraced: true,
      spans: [], duration_ms: gap,
      offsetPct: 0, widthPct: total > 0 ? (gap / total) * 100 : 0,
      earliestNs: 0, latestNs: 0,
    })
  }

  return groups.filter(g => g.spans.length || g.untraced)
})

const untracedMs = computed(() => {
  const g = phaseGroups.value.find(p => p.untraced)
  return g ? g.duration_ms : 0
})

// ── Phase collapse state — persisted in localStorage ──

const LS_KEY = 'forgerag.trace.collapsedPhases.v1'
function readInitialCollapsed() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (raw) {
      const arr = JSON.parse(raw)
      if (Array.isArray(arr)) {
        const out = {}
        for (const n of arr) out[n] = true
        return out
      }
    }
  } catch {}
  // Default: all phases collapsed (per design — show summary first,
  // user expands what they care about).
  return { setup: true, understanding: true, retrieval: true, generation: true, untraced: true }
}
const collapsed = ref(readInitialCollapsed())
watch(collapsed, (v) => {
  try {
    const arr = Object.keys(v).filter(k => v[k])
    localStorage.setItem(LS_KEY, JSON.stringify(arr))
  } catch {}
}, { deep: true })

function togglePhase(name) {
  collapsed.value = { ...collapsed.value, [name]: !collapsed.value[name] }
}

// ── Selection + ancestor highlight ──

const selectedId = ref(null)
function onRowClick(row) {
  selectedId.value = selectedId.value === row.span_id ? null : row.span_id
}
const ancestorIds = computed(() => {
  const set = new Set()
  if (!selectedId.value) return set
  const byId = new Map(spans.value.map(s => [s.span_id, s]))
  let cur = byId.get(selectedId.value)
  while (cur) {
    set.add(cur.span_id)
    cur = byId.get(cur.parent_span_id)
  }
  return set
})
const selectedRow = computed(() => allRows.value.find(r => r.span_id === selectedId.value) || null)

// ── Hover guide line ──
//
// Coordinates live in the body's CONTENT space (so they survive scroll
// without us having to listen to mouse events from the window). The
// guide line's ``top`` / ``height`` track the body's scroll viewport
// (scrollTop + clientHeight) so the line covers exactly the visible
// area; the tooltip follows the cursor's Y inside the line.

const bodyEl = ref(null)
const cursorX = ref(null)
const cursorY = ref(0)         // y in body content coords
const cursorMs = ref(0)
const scrollTop = ref(0)
const visibleHeight = ref(0)

// Tooltip sits at the TOP of the visible viewport (Chrome DevTools
// Performance pattern) — never overlaps the row the cursor's on. Just
// 2px in from the top so it doesn't kiss the panel border. The X
// follows the cursor; only Y is fixed.
//
// (We tried "follow cursor Y" — turned out to actively block whatever
// row the user was reading. Top-pin is calmer and more standard.)
const tooltipOffsetY = computed(() => 2)

function syncScroll() {
  if (!bodyEl.value) return
  scrollTop.value = bodyEl.value.scrollTop
  visibleHeight.value = bodyEl.value.clientHeight
}

let _resizeObs = null
onMounted(() => {
  if (!bodyEl.value) return
  bodyEl.value.addEventListener('scroll', syncScroll, { passive: true })
  _resizeObs = new ResizeObserver(syncScroll)
  _resizeObs.observe(bodyEl.value)
  syncScroll()
})
onBeforeUnmount(() => {
  if (bodyEl.value) bodyEl.value.removeEventListener('scroll', syncScroll)
  if (_resizeObs) _resizeObs.disconnect()
})

function onMouseMove(e) {
  if (!bodyEl.value || !totalMs.value) return
  // Use any phase bar-track as the timeline reference; they all share
  // the same width since they're rendered with identical layout rules.
  const trackEls = bodyEl.value.querySelectorAll('.phase-bar-track')
  if (!trackEls.length) return
  const rect = trackEls[0].getBoundingClientRect()
  const x = e.clientX - rect.left
  if (x < 0 || x > rect.width) { cursorX.value = null; return }
  const bodyRect = bodyEl.value.getBoundingClientRect()
  cursorX.value = e.clientX - bodyRect.left
  // y in content coords = client-y offset + body's scroll
  cursorY.value = e.clientY - bodyRect.top + bodyEl.value.scrollTop
  cursorMs.value = (x / rect.width) * totalMs.value
}

// ── LLM info / error / formatting (unchanged) ──

function extractLLMInfo(span) {
  const a = span.attributes || {}
  const model = a['gen_ai.request.model'] || a['gen_ai.response.model'] || a['llm.model'] || null
  const in_tokens = a['gen_ai.usage.input_tokens'] || a['gen_ai.usage.prompt_tokens'] || a['llm.token_count.prompt']
  const out_tokens = a['gen_ai.usage.output_tokens'] || a['gen_ai.usage.completion_tokens'] || a['llm.token_count.completion']
  const cost = a['gen_ai.usage.cost'] || a['llm.response.cost']
  if (!model && !in_tokens && !out_tokens) return null
  return { model, in_tokens, out_tokens, cost: cost ? Number(cost) : null }
}
function hasErrorEvent(span) {
  return (span.events || []).some(ev => ev.name === 'exception')
}
function errorSummary(span) {
  if (span.status?.description) return span.status.description
  const ev = (span.events || []).find(e => e.name === 'exception')
  if (ev) {
    return `${ev.attributes?.['exception.type'] || 'exception'}: ${
      (ev.attributes?.['exception.message'] || '').slice(0, 120)
    }`
  }
  return ''
}
const llmCalls = computed(() => allRows.value.filter(r => r.category === 'llm').length)
const totalTokens = computed(() => {
  let t = 0
  for (const r of allRows.value) {
    if (r.llmInfo) t += (r.llmInfo.in_tokens || 0) + (r.llmInfo.out_tokens || 0)
  }
  return t
})
const errorCount = computed(() => allRows.value.filter(r => r.isError).length)

function formatValue(v) {
  if (v == null) return 'null'
  if (typeof v === 'string' && v.length > 120) return v.slice(0, 117) + '…'
  if (Array.isArray(v)) return `[${v.length}] ${JSON.stringify(v).slice(0, 100)}`
  return String(v)
}
</script>

<style scoped>
.otel-viewer {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

/* ── Phase group header ─────────────────────────────────────── */
.phase-group + .phase-group { margin-top: 2px; }
.phase-header {
  display: grid;
  grid-template-columns: 14px 1fr auto auto;
  grid-template-areas: "chev label count duration"
                       "bar  bar   bar   bar";
  column-gap: 6px;
  row-gap: 3px;
  width: 100%;
  align-items: center;
  padding: 5px 8px 6px;
  border-radius: 4px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  transition: background 0.1s;
}
.phase-header:hover { background: var(--color-bg2); }
.phase-header.is-empty { opacity: 0.55; cursor: default; pointer-events: none; }
.phase-header.is-untraced { opacity: 0.7; }

.phase-chevron {
  grid-area: chev;
  color: var(--color-t3);
  transition: transform 0.15s;
}
.phase-chevron.is-open { transform: rotate(90deg); }

.phase-label {
  grid-area: label;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-t1);
}
.phase-count {
  grid-area: count;
  font-size: 9px;
  color: var(--color-t3);
  background: var(--color-bg3);
  padding: 0 5px;
  border-radius: 8px;
  line-height: 14px;
}
.phase-duration {
  grid-area: duration;
  font-size: 10px;
  color: var(--color-t2);
  font-family: var(--font-mono, monospace);
}
.phase-bar-track {
  grid-area: bar;
  margin: 0;
  height: 4px;
}

/* ── Span row (inside expanded phase) ──────────────────────── */
.phase-children {
  padding-left: 14px;
  padding-bottom: 4px;
}
.span-row {
  padding: 3px 8px 4px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.1s, opacity 0.1s;
}
.span-row:hover { background: var(--color-bg2); }
.span-row.is-selected {
  background: color-mix(in srgb, var(--color-brand) 14%, var(--color-bg));
}
/* Ancestor chain — slight outline + brand-tinted background, but
   NOT as loud as the selected row itself. */
.span-row.is-ancestor {
  background: color-mix(in srgb, var(--color-brand) 6%, var(--color-bg));
  outline: 1px solid color-mix(in srgb, var(--color-brand) 30%, transparent);
  outline-offset: -1px;
}
/* Non-related rows fade so the parent chain visually pops. */
.span-row.is-dimmed { opacity: 0.45; }

.tree-hinge {
  color: var(--color-t3);
  font-size: 8px;
  padding-right: 3px;
  opacity: 0.6;
}

.span-tag {
  display: inline-block;
  min-width: 22px;
  padding: 0 4px;
  margin-right: 5px;
  font-size: 8px;
  line-height: 14px;
  text-align: center;
  border-radius: 2px;
  background: var(--color-bg3);
  color: var(--color-t3);
  font-family: var(--font-mono, monospace);
}
.span-tag--forgerag { background: var(--color-brand-bg); color: var(--color-brand); }
.span-tag--llm      { background: var(--color-warn-bg);  color: var(--color-warn-fg); }
.span-tag--db       { background: var(--color-bg3);      color: var(--color-t2); }
.span-tag--http     { background: var(--color-run-bg);   color: var(--color-run-fg); }

.span-full-name {
  color: var(--color-t1);
  font-size: 10px;
  font-weight: 500;
}

.bar-track {
  position: relative;
  height: 3px;
  margin: 2px 0 0 22px;
  background: var(--color-bg3);
  border-radius: 2px;
  overflow: visible;
}
.bar-fill {
  position: absolute;
  top: 0;
  height: 100%;
  border-radius: 2px;
  min-width: 2px;
}
.bar-fill--forgerag { background: var(--color-brand); }
.bar-fill--llm      { background: var(--color-warn-fg); }
.bar-fill--db       { background: var(--color-t3); }
.bar-fill--http     { background: var(--color-run-fg); }
.bar-fill--other    { background: var(--color-t3); }
/* Untraced gap rendered as a hatched grey to read as "missing", not
   as a real span. */
.bar-fill--untraced {
  background: repeating-linear-gradient(
    -45deg,
    var(--color-bg3) 0,
    var(--color-bg3) 3px,
    var(--color-line2) 3px,
    var(--color-line2) 6px
  );
}

/* ── Hover guide line + time tooltip ────────────────────────
   Inverted (Vercel-style): the line + tooltip fill use ``--color-t1``
   which is near-black in light mode and near-white in dark mode; the
   tooltip's text uses ``--color-bg`` for inverted contrast. Brand
   blue is reserved for selected state / accents — the cursor ruler
   is a neutral measurement tool, not a CTA. */
.cursor-guide {
  position: absolute;
  /* ``top`` and ``height`` are bound from JS to bodyEl.scrollTop
     and bodyEl.clientHeight so the line spans the visible viewport
     (not the body's content extent) regardless of scroll. */
  width: 1px;
  background: var(--color-t1);
  opacity: 0.6;
  pointer-events: none;
  z-index: 5;
}
.cursor-tooltip {
  position: absolute;
  /* ``top`` is bound from JS so the tooltip follows the cursor Y. */
  left: 4px;
  padding: 1px 5px;
  background: var(--color-t1);
  color: var(--color-bg);
  font-size: 9px;
  font-family: var(--font-mono, monospace);
  border-radius: 3px;
  white-space: nowrap;
}
</style>
