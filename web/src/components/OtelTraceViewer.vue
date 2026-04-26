<template>
  <div v-if="!hasSpans" class="px-4 py-6 text-[11px] text-t3">
    No trace data.
  </div>
  <div v-else class="otel-viewer">
    <!-- Summary -->
    <div class="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-t3 px-4 py-3 border-b border-line">
      <span><b class="text-t2">{{ totalMs.toFixed(0) }}</b>ms total</span>
      <span><b class="text-t2">{{ spans.length }}</b> spans</span>
      <span v-if="llmCalls"><b class="text-t2">{{ llmCalls }}</b> LLM calls</span>
      <span v-if="totalTokens"><b class="text-t2">{{ totalTokens }}</b> tokens</span>
      <span v-if="errorCount" class="text-rose-500"><b>{{ errorCount }}</b> error{{ errorCount > 1 ? 's' : '' }}</span>
    </div>

    <!-- Waterfall -->
    <div class="overflow-y-auto flex-1 px-2 py-2">
      <div
        v-for="row in rows"
        :key="row.span_id"
        class="span-row"
        :class="{ 'is-selected': selectedId === row.span_id }"
        @click="selectedId = selectedId === row.span_id ? null : row.span_id"
      >
        <!-- Name + indent -->
        <div class="flex items-center text-[10px]" :style="{ paddingLeft: row.depth * 10 + 'px' }">
          <span class="tree-hinge" v-if="row.depth > 0">└</span>
          <span class="span-tag" :class="`span-tag--${row.category}`">{{ row.shortName }}</span>
          <span class="span-full-name truncate">{{ row.displayName }}</span>
          <span class="ml-auto pl-2 text-t3 font-mono tabular-nums text-[9px]">{{ row.duration_ms.toFixed(1) }}ms</span>
        </div>
        <!-- Waterfall bar -->
        <div class="bar-track">
          <div
            class="bar-fill"
            :class="`bar-fill--${row.category}`"
            :style="{ left: row.offsetPct + '%', width: row.widthPct + '%' }"
          />
        </div>
        <!-- Token / cost inline for LLM spans -->
        <div v-if="row.llmInfo" class="text-[9px] text-t3 mt-0.5" :style="{ paddingLeft: row.depth * 10 + 14 + 'px' }">
          <span class="font-mono">{{ row.llmInfo.model }}</span>
          <span v-if="row.llmInfo.in_tokens" class="ml-2">in <b>{{ row.llmInfo.in_tokens }}</b></span>
          <span v-if="row.llmInfo.out_tokens" class="ml-2">out <b>{{ row.llmInfo.out_tokens }}</b></span>
          <span v-if="row.llmInfo.cost" class="ml-2">${{ row.llmInfo.cost.toFixed(4) }}</span>
        </div>
        <!-- Error indicator -->
        <div v-if="row.isError" class="text-[9px] text-rose-500 mt-0.5" :style="{ paddingLeft: row.depth * 10 + 14 + 'px' }">
          ⚠ {{ row.errorSummary }}
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
 * OtelTraceViewer — renders a standard OpenTelemetry span array as a
 * waterfall trace viewer. Input is the `trace` payload from
 * `POST /api/v1/query` (or the `spans` array from a persisted trace).
 *
 * Each span: { trace_id, span_id, parent_span_id, name,
 *              start_time_unix_nano, end_time_unix_nano, duration_ms,
 *              attributes: {...}, events: [{name, attributes}, ...],
 *              status: {code, description} }
 *
 * Categories drive colour and grouping:
 *   forgerag  — our retrieval / answering pipeline spans
 *   llm       — LiteLLM completion / embedding spans (token + cost)
 *   db        — SQL queries (SQLAlchemy auto-instrumentation)
 *   http      — outbound HTTPX calls
 *   other     — everything else
 */
import { computed, ref } from 'vue'

const props = defineProps({
  trace: { type: Object, default: null },
})

const selectedId = ref(null)

const spans = computed(() => props.trace?.spans || [])
const hasSpans = computed(() => spans.value.length > 0)

/** Find root span (no parent), compute total duration */
const root = computed(() => spans.value.find(s => !s.parent_span_id) || null)
const totalMs = computed(() => {
  if (!root.value) return 0
  return root.value.duration_ms || 0
})
const rootStartNs = computed(() => root.value?.start_time_unix_nano || 0)

/** Categorise a span by name / attributes */
function categorise(span) {
  const n = span.name || ''
  if (n.startsWith('forgerag.')) return 'forgerag'
  // LiteLLM emits span names like "OpenAI.completion" / "litellm.completion"
  // Also detect by presence of gen_ai.* attributes
  if (span.attributes?.['gen_ai.system'] || /litellm|openai|completion|embedding|rerank/i.test(n)) {
    return 'llm'
  }
  if (/^(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|connect)\b/i.test(n)) return 'db'
  if (/^HTTP |^GET |^POST /.test(n) || span.attributes?.['http.method']) return 'http'
  return 'other'
}

/** Short tag shown left-of-name: first 2–3 chars */
function shortTag(category) {
  return {
    forgerag: 'fr',
    llm: 'ai',
    db: 'db',
    http: 'http',
    other: '·',
  }[category]
}

/** Collapse a forgerag span name: "forgerag.bm25_path" → "bm25_path" */
function displayName(name) {
  return name.replace(/^forgerag\./, '')
}

/** Order spans DFS by start time so the waterfall reads top-to-bottom */
const rows = computed(() => {
  if (!hasSpans.value) return []
  const byId = new Map(spans.value.map(s => [s.span_id, s]))
  const childrenOf = new Map()
  for (const s of spans.value) {
    const p = s.parent_span_id
    if (!childrenOf.has(p)) childrenOf.set(p, [])
    childrenOf.get(p).push(s)
  }
  // Sort each child list by start time
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
    const row = {
      span_id: span.span_id,
      parent_span_id: span.parent_span_id,
      name: span.name,
      displayName: displayName(span.name),
      shortName: shortTag(cat),
      category: cat,
      depth,
      duration_ms: span.duration_ms || 0,
      offsetPct: total > 0 ? (startMs / total) * 100 : 0,
      widthPct: total > 0 ? Math.max(0.4, ((span.duration_ms || 0) / total) * 100) : 0,
      attributes: span.attributes || {},
      events: span.events || [],
      llmInfo: extractLLMInfo(span),
      isError: span.status?.code === 'ERROR' || hasErrorEvent(span),
      errorSummary: errorSummary(span),
    }
    out.push(row)
    const children = childrenOf.get(span.span_id) || []
    for (const c of children) walk(c, depth + 1)
  }
  for (const r of roots) walk(r, 0)
  return out
})

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

const llmCalls = computed(() => rows.value.filter(r => r.category === 'llm').length)

const totalTokens = computed(() => {
  let t = 0
  for (const r of rows.value) {
    if (r.llmInfo) t += (r.llmInfo.in_tokens || 0) + (r.llmInfo.out_tokens || 0)
  }
  return t
})

const errorCount = computed(() => rows.value.filter(r => r.isError).length)

const selectedRow = computed(() => rows.value.find(r => r.span_id === selectedId.value) || null)

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

.span-row {
  padding: 3px 8px 4px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.1s;
}
.span-row:hover { background: var(--color-bg2); }
.span-row.is-selected {
  background: color-mix(in srgb, var(--color-brand) 14%, var(--color-bg));
}

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
/* Tag palette — Vercel-style restrained:
   forgerag owns the brand blue; everything else sits on the neutral grays. */
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
</style>
