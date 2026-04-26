<template>
  <div class="hb">
    <div v-if="!items.length" class="hb-empty">no data</div>
    <div v-for="r in rows" :key="r.key" class="row">
      <div class="row-label">{{ r.label }}</div>
      <div class="row-track">
        <div class="row-bar row-bar-avg" :style="{ width: r.avgPct + '%', background: colorFor(r.key) }"></div>
        <div class="row-bar row-bar-p95" :style="{ left: r.avgPct + '%', width: (r.p95Pct - r.avgPct) + '%', background: colorFor(r.key) }"></div>
      </div>
      <div class="row-val">
        <span class="text-t1">{{ fmt(r.avg_ms) }}</span>
        <span class="text-t3"> / {{ fmt(r.p95_ms) }}</span>
      </div>
    </div>
    <div v-if="items.length" class="hb-footnote">
      <span>avg</span>
      <span class="mute">· p95</span>
      <span class="mute">· {{ totalSamples }} samples</span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  // items: [{ key, label, avg_ms, p95_ms, samples }]
  items: { type: Array, default: () => [] },
})

// Vercel-style monochrome ramp. Retrieval paths get the strongest tone
// (this is the user's primary "where's time spent" question); fusion +
// rerank fade out as supporting detail.
const COLOR_MAP = {
  qu:     'var(--color-t1)',
  bm25:   'var(--color-t1)',
  vector: 'var(--color-t1)',
  kg:     'var(--color-t1)',
  tree:   'var(--color-t1)',
  rrf:    'var(--color-t3)',
  expand: 'var(--color-t3)',
  rerank: 'var(--color-t2)',
}
function colorFor(k) { return COLOR_MAP[k] || 'var(--color-t3)' }

const maxVal = computed(() => {
  if (!props.items.length) return 1
  return Math.max(...props.items.map(r => r.p95_ms || r.avg_ms || 0)) || 1
})

const rows = computed(() => props.items.map(r => ({
  ...r,
  avgPct: Math.min(100, (r.avg_ms / maxVal.value) * 100),
  p95Pct: Math.min(100, (r.p95_ms / maxVal.value) * 100),
})))

const totalSamples = computed(() => props.items.reduce((s, r) => s + (r.samples || 0), 0))

function fmt(ms) {
  if (ms == null) return '—'
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}
</script>

<style scoped>
.hb { font-size: 11px; position: relative; }
.hb-empty {
  display: flex; align-items: center; justify-content: center;
  padding: 20px;
  font-size: 11px;
  color: var(--color-t3);
}

.row {
  display: grid;
  grid-template-columns: 54px 1fr 96px;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
}
.row-label {
  font-size: 10px;
  color: var(--color-t2);
  text-align: right;
}
.row-track {
  position: relative;
  height: 12px;
  background: var(--color-bg2);
  border-radius: 2px;
  overflow: hidden;
}
.row-bar {
  position: absolute;
  top: 0;
  bottom: 0;
  height: 100%;
}
.row-bar-avg { left: 0; }
.row-bar-p95 { opacity: 0.4; }
.row-val {
  font-size: 10px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.hb-footnote {
  margin-top: 6px;
  padding-top: 4px;
  border-top: 1px solid var(--color-line);
  font-size: 9px;
  color: var(--color-t3);
  display: flex;
  gap: 6px;
}
.mute { color: var(--color-t3); }
</style>
