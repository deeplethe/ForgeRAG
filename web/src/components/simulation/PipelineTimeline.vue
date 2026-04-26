<template>
  <div class="timeline-root">
    <div v-if="!store.lastResult && !store.running" class="empty-state">
      <span class="text-[11px] text-t3">Run a simulation to see the pipeline timeline.</span>
    </div>
    <div v-else-if="store.running" class="empty-state">
      <span class="text-[11px] text-t3">Running…</span>
    </div>
    <div v-else class="timeline-body">
      <div class="flex items-baseline justify-between mb-2">
        <div class="text-[10px] text-t3">
          total {{ fmtMs(totalMs) }}
          <span v-if="stats?.total_ms"> · retrieve {{ fmtMs(stats.total_ms) }}</span>
        </div>
        <div class="text-[10px] text-t3 flex gap-3">
          <span v-if="stats?.bm25_hits != null">BM25 {{ stats.bm25_hits }}</span>
          <span v-if="stats?.vector_hits != null">Vec {{ stats.vector_hits }}</span>
          <span v-if="stats?.tree_hits != null">Tree {{ stats.tree_hits }}</span>
          <span v-if="stats?.kg_hits != null">KG {{ stats.kg_hits }}</span>
          <span v-if="stats?.merged_count != null">merged {{ stats.merged_count }}</span>
        </div>
      </div>

      <div class="stages">
        <div
          v-for="s in stageRows"
          :key="s.key"
          class="stage-row"
          :class="{ selected: store.selectedStageKey === s.spanKey, 'stage-absent': !s.present }"
          @click="s.present && store.selectStage(s.spanKey)"
        >
          <div class="stage-label">{{ s.label }}</div>
          <div class="stage-lane">
            <div
              v-if="s.present"
              class="bar"
              :class="'bar-' + s.kind"
              :style="barStyle(s)"
              :title="`${s.label} · ${fmtMs(s.durationMs)}`"
            >
              <span class="bar-ms">{{ fmtMs(s.durationMs) }}</span>
            </div>
            <span v-else class="bar-absent">skipped</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useSimulationStore } from '@/stores/simulation'

const store = useSimulationStore()

/**
 * Nine canonical stage rows. `spanNames` are candidates to locate the span;
 * first match wins. The `answer` row is synthesized (no single span covers
 * the generation phase directly — it's the gap between the retrieve span end
 * and the root span end).
 */
const STAGE_DEFS = [
  { key: 'qu',     label: 'QU',     spanNames: ['forgerag.query_understanding', 'forgerag.query_expansion'], kind: 'qu' },
  { key: 'bm25',   label: 'BM25',   spanNames: ['forgerag.bm25_path'],   kind: 'bm25' },
  { key: 'vector', label: 'Vector', spanNames: ['forgerag.vector_path'], kind: 'vector' },
  { key: 'kg',     label: 'KG',     spanNames: ['forgerag.kg_path'],     kind: 'kg' },
  { key: 'tree',   label: 'Tree',   spanNames: ['forgerag.tree_path'],   kind: 'tree' },
  { key: 'rrf',    label: 'RRF',    spanNames: ['forgerag.rrf_merge'],   kind: 'merge' },
  { key: 'expand', label: 'Expand', spanNames: ['forgerag.expansion'],   kind: 'merge' },
  { key: 'rerank', label: 'Rerank', spanNames: ['forgerag.rerank'],      kind: 'rerank' },
  { key: 'answer', label: 'Answer', synthetic: true, kind: 'answer' },
]

const result = computed(() => store.lastResult)
const stats = computed(() => result.value?.stats || null)
const trace = computed(() => result.value?.trace || null)

/** All spans, flat list. */
const spans = computed(() => trace.value?.spans || [])

/** Top-level span (the root of the request): pick smallest start + no parent in set. */
const rootSpan = computed(() => {
  if (!spans.value.length) return null
  const ids = new Set(spans.value.map((s) => s.span_id))
  const candidates = spans.value.filter(
    (s) => !s.parent_span_id || !ids.has(s.parent_span_id),
  )
  if (!candidates.length) return spans.value[0]
  // Prefer forgerag.answer, then forgerag.retrieve
  candidates.sort((a, b) => (a.start_time_unix_nano ?? 0) - (b.start_time_unix_nano ?? 0))
  const byName = (n) => candidates.find((s) => s.name === n)
  return byName('forgerag.answer') || byName('forgerag.retrieve') || candidates[0]
})

const rootStart = computed(() => Number(rootSpan.value?.start_time_unix_nano ?? 0))
const rootEnd = computed(() => Number(rootSpan.value?.end_time_unix_nano ?? 0))
const totalNs = computed(() => Math.max(1, rootEnd.value - rootStart.value))
const totalMs = computed(() => totalNs.value / 1e6)

function findSpan(names) {
  for (const n of names) {
    const hit = spans.value.find((s) => s.name === n)
    if (hit) return hit
  }
  return null
}

const retrieveSpan = computed(() => spans.value.find((s) => s.name === 'forgerag.retrieve') || null)

const stageRows = computed(() => {
  if (!rootSpan.value) return STAGE_DEFS.map((d) => ({ ...d, present: false }))
  return STAGE_DEFS.map((d) => {
    if (d.synthetic && d.key === 'answer') {
      const rEnd = retrieveSpan.value ? Number(retrieveSpan.value.end_time_unix_nano) : rootEnd.value
      const durNs = rootEnd.value - rEnd
      if (durNs <= 0) return { ...d, present: false }
      return {
        ...d,
        present: true,
        spanKey: 'synthetic.answer',
        startNs: rEnd,
        endNs: rootEnd.value,
        durationMs: durNs / 1e6,
      }
    }
    const sp = findSpan(d.spanNames)
    if (!sp) return { ...d, present: false }
    return {
      ...d,
      present: true,
      spanKey: sp.name,
      startNs: Number(sp.start_time_unix_nano),
      endNs: Number(sp.end_time_unix_nano),
      durationMs: sp.duration_ms ?? (Number(sp.end_time_unix_nano) - Number(sp.start_time_unix_nano)) / 1e6,
    }
  })
})

function barStyle(s) {
  const offset = ((s.startNs - rootStart.value) / totalNs.value) * 100
  const width = Math.max(0.5, ((s.endNs - s.startNs) / totalNs.value) * 100)
  return {
    left: `${offset}%`,
    width: `${width}%`,
  }
}

function fmtMs(ms) {
  if (ms == null) return ''
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}
</script>

<style scoped>
.timeline-root {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
}

.timeline-body {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.stages {
  display: flex;
  flex-direction: column;
}

.stage-row {
  display: grid;
  grid-template-columns: 60px 1fr;
  align-items: center;
  gap: 8px;
  padding: 2px 0;
  cursor: pointer;
  border-radius: 3px;
}
.stage-row:hover:not(.stage-absent) { background: var(--color-bg2); }
.stage-row.selected { background: var(--color-bg3); }
.stage-row.selected .stage-label { color: var(--color-t1); font-weight: 500; }
/* Selected row: strong neutral outline so the choice is obvious without
   reaching for branded blue (matches the rest of the app's selection style). */
.stage-row.selected .bar { box-shadow: 0 0 0 1.5px var(--color-t1); opacity: 1; }
.stage-row.stage-absent { cursor: default; }
.stage-row.stage-absent .stage-label { color: var(--color-t3); }

.stage-label {
  font-size: 10px;
  color: var(--color-t2);
  text-align: right;
  padding-right: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stage-lane {
  position: relative;
  height: 14px;
  background: var(--color-bg);
  border-radius: 2px;
}

.bar {
  position: absolute;
  top: 1px; bottom: 1px;
  min-width: 2px;
  border-radius: 2px;
  display: flex;
  align-items: center;
  padding: 0 4px;
  font-size: 8.5px;
  color: white;
  overflow: hidden;
  box-sizing: border-box;
  font-variant-numeric: tabular-nums;
}
.bar-ms {
  opacity: 0.85;
  white-space: nowrap;
  text-shadow: 0 0 2px rgba(0,0,0,0.35);
}

/* Unified monochrome ramp (Vercel pattern: no branded blue accents).
   Retrieval paths are darkest with subtle opacity tiers to differentiate
   them inside the same band. Fusion / rerank step down to mid-gray;
   final answer lands on the same darkest tone as the paths to read as
   "the output of all that". */
.bar-qu     { background: var(--color-t1); }
.bar-bm25   { background: var(--color-t1); opacity: 0.85; }
.bar-vector { background: var(--color-t1); opacity: 0.85; }
.bar-kg     { background: var(--color-t1); opacity: 0.7; }
.bar-tree   { background: var(--color-t1); opacity: 0.7; }
.bar-merge  { background: var(--color-t3); }
.bar-rerank { background: var(--color-t2); }
.bar-answer { background: var(--color-t1); }

.bar-absent {
  display: inline-block;
  padding-left: 4px;
  font-size: 9px;
  color: var(--color-t3);
  font-style: italic;
  line-height: 14px;
}
</style>
