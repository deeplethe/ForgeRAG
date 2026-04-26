<template>
  <div class="detail-root">
    <div v-if="!store.lastResult" class="empty">
      <span class="text-[11px] text-t3">Nothing to inspect yet.</span>
    </div>

    <div v-else-if="!selectedSpan" class="empty">
      <span class="text-[11px] text-t3">Click a stage above to inspect it.</span>
    </div>

    <div v-else class="detail-body">
      <header class="head">
        <div class="flex items-baseline gap-3">
          <span class="text-[12px] text-t1 font-medium">{{ headerTitle }}</span>
          <span class="text-[10px] text-t3">{{ headerSub }}</span>
        </div>
        <div v-if="selectedSpan.status?.description" class="text-[10px] mt-0.5" style="color: var(--color-err-fg);">
          {{ selectedSpan.status.description }}
        </div>
      </header>

      <!-- Quick stats line for path stages -->
      <section v-if="hitsInfo" class="section">
        <div class="section-head">Hits</div>
        <div class="hits-line">
          <span class="hits-count">{{ hitsInfo.count }} hits</span>
          <span v-if="hitsInfo.topIds?.length" class="text-[10px] text-t3">
            top: {{ hitsInfo.topIds.join(', ') }}
          </span>
        </div>
      </section>

      <!-- Span attributes -->
      <section v-if="attrEntries.length" class="section">
        <div class="section-head">Attributes</div>
        <table class="kv">
          <tr v-for="[k, v] in attrEntries" :key="k">
            <td class="kv-k">{{ k }}</td>
            <td class="kv-v"><code>{{ fmtVal(v) }}</code></td>
          </tr>
        </table>
      </section>

      <!-- LLM child calls -->
      <section v-if="llmChildren.length" class="section">
        <div class="section-head">LLM calls ({{ llmChildren.length }})</div>
        <table class="llm-tbl">
          <tr v-for="c in llmChildren" :key="c.span_id">
            <td class="text-t2">{{ c.attributes?.['gen_ai.request.model'] || c.attributes?.['llm.model'] || c.name }}</td>
            <td class="text-t3 num">{{ fmtMs(c.duration_ms) }}</td>
            <td class="text-t3 num">in {{ c.attributes?.['gen_ai.usage.prompt_tokens'] ?? c.attributes?.['llm.prompt_tokens'] ?? '?' }}</td>
            <td class="text-t3 num">out {{ c.attributes?.['gen_ai.usage.completion_tokens'] ?? c.attributes?.['llm.completion_tokens'] ?? '?' }}</td>
          </tr>
        </table>
      </section>

      <!-- Events -->
      <section v-if="selectedSpan.events?.length" class="section">
        <div class="section-head">Events</div>
        <div v-for="(e, i) in selectedSpan.events" :key="i" class="evt">
          <span class="text-t2">{{ e.name }}</span>
          <span v-if="e.attributes && Object.keys(e.attributes).length" class="text-t3">
            {{ JSON.stringify(e.attributes) }}
          </span>
        </div>
      </section>

      <!-- Synthetic answer: show the final answer text -->
      <section v-if="isAnswerSynthetic" class="section">
        <div class="section-head">Answer preview</div>
        <pre class="answer">{{ store.lastResult.text }}</pre>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useSimulationStore } from '@/stores/simulation'

const store = useSimulationStore()

const spans = computed(() => store.lastResult?.trace?.spans || [])
const stats = computed(() => store.lastResult?.stats || {})

const isAnswerSynthetic = computed(() => store.selectedStageKey === 'synthetic.answer')

const selectedSpan = computed(() => {
  if (!store.lastResult) return null
  if (isAnswerSynthetic.value) {
    // Render a synthesized "span-like" object for display — the answer stage
    // doesn't have a direct span, so we fabricate one from the root.
    const ids = new Set(spans.value.map((s) => s.span_id))
    const roots = spans.value.filter((s) => !s.parent_span_id || !ids.has(s.parent_span_id))
    const root = roots.find((s) => s.name === 'forgerag.answer') || roots[0]
    const retrieve = spans.value.find((s) => s.name === 'forgerag.retrieve')
    if (!root) return null
    return {
      name: 'synthetic.answer',
      displayName: 'Answer generation',
      // Reuse the root span_id so llmChildren lookup finds the generator
      // call (which is parented to the root).
      span_id: root.span_id,
      start_time_unix_nano: retrieve?.end_time_unix_nano ?? root.start_time_unix_nano,
      end_time_unix_nano: root.end_time_unix_nano,
      duration_ms: (Number(root.end_time_unix_nano) - Number(retrieve?.end_time_unix_nano ?? root.start_time_unix_nano)) / 1e6,
      attributes: root.attributes || {},
      events: [],
      status: root.status,
    }
  }
  return spans.value.find((s) => s.name === store.selectedStageKey) || null
})

const headerTitle = computed(() => selectedSpan.value?.displayName || selectedSpan.value?.name || '')
const headerSub = computed(() => {
  const s = selectedSpan.value
  if (!s) return ''
  return fmtMs(s.duration_ms)
})

const attrEntries = computed(() => {
  const a = selectedSpan.value?.attributes || {}
  // Prune internal bookkeeping that adds noise
  const skip = new Set(['telemetry.sdk.name', 'telemetry.sdk.version', 'telemetry.sdk.language'])
  return Object.entries(a).filter(([k]) => !skip.has(k))
})

const llmChildren = computed(() => {
  const sel = selectedSpan.value
  if (!sel || !sel.span_id) return []
  // Synthetic spans don't have an ID — fall back to direct children of root
  return spans.value.filter(
    (s) =>
      s.parent_span_id === sel.span_id &&
      (s.name?.startsWith('litellm') ||
       s.name?.toLowerCase().includes('completion') ||
       s.attributes?.['gen_ai.request.model'] ||
       s.attributes?.['llm.model']),
  )
})

const HITS_MAP = {
  'forgerag.bm25_path':   { count: 'bm25_hits',   top: 'bm25_top_ids' },
  'forgerag.vector_path': { count: 'vector_hits', top: 'vector_top_ids' },
  'forgerag.tree_path':   { count: 'tree_hits',   top: 'tree_top_ids' },
  'forgerag.kg_path':     { count: 'kg_hits',     top: 'kg_top_ids' },
  'forgerag.rrf_merge':   { count: 'merged_count', top: null },
  'forgerag.rerank':      { count: 'reranked_count', top: null },
}

const hitsInfo = computed(() => {
  const key = store.selectedStageKey
  const m = HITS_MAP[key]
  if (!m) return null
  const count = stats.value[m.count]
  if (count == null) return null
  return {
    count,
    topIds: m.top ? stats.value[m.top] : null,
  }
})

function fmtMs(ms) {
  if (ms == null) return ''
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function fmtVal(v) {
  if (v == null) return 'null'
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  try { return JSON.stringify(v) } catch { return String(v) }
}
</script>

<style scoped>
.detail-root {
  padding: 14px 20px 24px;
  font-size: 11px;
  color: var(--color-t2);
  height: 100%;
}

.empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 120px;
}

.detail-body {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

/* No bottom border slicing the panel — Vercel uses spacing alone */
.head { padding-bottom: 4px; }

.section { padding-bottom: 0; }

.section-head {
  font-size: 10px;
  color: var(--color-t3);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 6px;
}

.hits-line {
  display: flex;
  align-items: baseline;
  gap: 12px;
  font-size: 11px;
}
.hits-count {
  color: var(--color-t1);
  font-weight: 500;
}

.kv {
  width: 100%;
  border-collapse: collapse;
  font-size: 10px;
  font-family: ui-monospace, 'Cascadia Code', Menlo, monospace;
}
.kv tr { border-top: 1px solid var(--color-line); }
.kv tr:first-child { border-top: none; }
.kv-k {
  padding: 3px 8px 3px 0;
  color: var(--color-t3);
  white-space: nowrap;
  vertical-align: top;
  width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kv-v {
  padding: 3px 0;
  color: var(--color-t1);
  word-break: break-all;
}
.kv-v code {
  background: transparent;
  font-family: inherit;
}

.llm-tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 10px;
}
.llm-tbl td {
  padding: 3px 8px;
  border-top: 1px solid var(--color-line);
}
.llm-tbl tr:first-child td { border-top: none; }
.num { text-align: right; font-variant-numeric: tabular-nums; }

.evt {
  display: flex;
  gap: 8px;
  padding: 2px 0;
  font-size: 10px;
}

.answer {
  padding: 8px 10px;
  font-size: 11px;
  color: var(--color-t1);
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
  border-radius: 4px;
  max-height: 300px;
  overflow-y: auto;
  white-space: pre-wrap;
  font-family: inherit;
}
</style>
