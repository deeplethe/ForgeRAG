<template>
  <div class="sb" ref="root">
    <svg v-if="W > 0" :width="W" :height="H" class="sb-svg">
      <!-- y-axis -->
      <g v-for="(g, i) in gridY" :key="'g' + i">
        <line :x1="padL" :y1="g.y" :x2="W - padR" :y2="g.y" class="grid" />
        <text :x="padL - 4" :y="g.y + 3" class="axis-label" text-anchor="end">{{ g.label }}</text>
      </g>
      <!-- bars -->
      <g v-for="(b, i) in bars" :key="'b' + i">
        <rect v-for="(seg, j) in b.segs" :key="j"
          :x="b.x" :y="seg.y" :width="b.w" :height="seg.h" :fill="seg.color" class="bar-seg">
          <title>{{ seg.label }}: {{ fmt(seg.value) }}</title>
        </rect>
      </g>
      <!-- x-axis labels (sparse) -->
      <text v-for="(t, i) in xTicks" :key="'x' + i"
        :x="t.x" :y="H - 4" class="axis-label" text-anchor="middle">{{ t.label }}</text>
      <!-- legend -->
      <g class="legend" :transform="`translate(${padL}, ${padT - 4})`">
        <g v-for="(k, i) in keys" :key="'l' + i" :transform="`translate(${i * 90}, 0)`">
          <rect x="0" y="-4" width="8" height="8" :fill="colorFor(k)" />
          <text x="12" y="3" class="legend-label">{{ k }}</text>
        </g>
      </g>
    </svg>
    <div v-if="!buckets.length" class="sb-empty">no data</div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'

const props = defineProps({
  // points: [{ ts: Date|number, model: string, value: number }]
  points: { type: Array, default: () => [] },
  xFormat: { type: Function, default: (v) => '' },
  valueFormat: { type: Function, default: (v) => Math.round(v) },
  /** When true, axis ticks snap to integer values (avoids 0.333/0.666 on small ranges). */
  integer: { type: Boolean, default: false },
})

import { niceTicks } from './niceTicks.js'

const root = ref(null)
const W = ref(0)
const H = ref(150)
const padL = 38, padR = 8, padT = 12, padB = 18

let ro = null
onMounted(() => {
  if (!root.value) return
  const update = () => { W.value = root.value.clientWidth }
  update()
  ro = new ResizeObserver(update)
  ro.observe(root.value)
})
onUnmounted(() => { if (ro) ro.disconnect() })

const innerW = computed(() => Math.max(1, W.value - padL - padR))
const innerH = computed(() => Math.max(1, H.value - padT - padB))

// Collapse into (bucketId → { model → value }) + ordered keys
const grouped = computed(() => {
  const map = new Map()
  const modelSet = new Set()
  for (const p of props.points) {
    const k = p.ts instanceof Date ? p.ts.getTime() : +p.ts
    modelSet.add(p.model)
    const bucket = map.get(k) ?? { ts: p.ts, values: {} }
    bucket.values[p.model] = (bucket.values[p.model] ?? 0) + (p.value || 0)
    map.set(k, bucket)
  }
  const buckets = Array.from(map.values()).sort((a, b) => {
    const ax = a.ts instanceof Date ? a.ts.getTime() : +a.ts
    const bx = b.ts instanceof Date ? b.ts.getTime() : +b.ts
    return ax - bx
  })
  return { buckets, keys: Array.from(modelSet) }
})

const buckets = computed(() => grouped.value.buckets)
const keys = computed(() => grouped.value.keys)

// Vercel-leaning palette: pure neutral ramp (dark → mid → light gray).
// Vercel's own analytics dashboards use a single-tone scale rather than
// branded blue accents — primary metric is darkest, comparisons fade out.
// Hex values are baked in (rather than CSS vars) so dark mode still gets a
// readable spread; the segments stay legible against either bg.
const COLOR_PALETTE = [
  '#0a0a0a',  // near-black (most prominent)
  '#52525b',  // zinc-600
  '#a1a1aa',  // zinc-400
  '#27272a',  // zinc-800
  '#71717a',  // zinc-500
  '#3f3f46',  // zinc-700
  '#d4d4d8',  // zinc-300
]
function colorFor(model) {
  const i = keys.value.indexOf(model)
  return COLOR_PALETTE[i % COLOR_PALETTE.length]
}

const rawMax = computed(() => {
  let mx = 0
  for (const b of buckets.value) {
    let s = 0
    for (const k of keys.value) s += b.values[k] || 0
    if (s > mx) mx = s
  }
  return mx
})

// "Nice" axis: pick a clean step so labels are 0/100/200, not 0.333/0.666.
const scale = computed(() => niceTicks(0, rawMax.value, 4, { integer: props.integer }))
const yMax = computed(() => scale.value.max)

const barW = computed(() => {
  if (!buckets.value.length) return 0
  const gap = 2
  return Math.max(1, innerW.value / buckets.value.length - gap)
})

const bars = computed(() => {
  if (!buckets.value.length) return []
  const step = innerW.value / buckets.value.length
  return buckets.value.map((b, i) => {
    const x = padL + i * step + (step - barW.value) / 2
    let yCursor = padT + innerH.value
    const segs = []
    for (const k of keys.value) {
      const v = b.values[k] || 0
      if (v <= 0) continue
      const h = (v / yMax.value) * innerH.value
      yCursor -= h
      segs.push({ y: yCursor, h, color: colorFor(k), label: k, value: v })
    }
    return { x, w: barW.value, segs }
  })
})

const gridY = computed(() => {
  const ticks = scale.value.ticks
  if (!ticks.length || yMax.value <= 0) return []
  return ticks.map((v) => ({
    y: padT + innerH.value - (v / yMax.value) * innerH.value,
    label: props.valueFormat(v),
  }))
})

const xTicks = computed(() => {
  if (!buckets.value.length) return []
  const n = buckets.value.length
  const want = Math.min(5, n)
  const step = Math.max(1, Math.floor((n - 1) / (want - 1 || 1)))
  const stepPx = innerW.value / n
  const out = []
  for (let i = 0; i < n; i += step) {
    out.push({
      x: padL + i * stepPx + stepPx / 2,
      label: props.xFormat(buckets.value[i].ts),
    })
  }
  return out
})

function fmt(v) { return props.valueFormat(v) }
</script>

<style scoped>
.sb { position: relative; width: 100%; }
.sb-svg { display: block; }
.sb-empty {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; color: var(--color-t3);
}
.grid { stroke: var(--color-line); stroke-width: 0.5; }
.axis-label { font-size: 9px; fill: var(--color-t3); }
.legend-label { font-size: 9px; fill: var(--color-t2); dominant-baseline: middle; }
.bar-seg { shape-rendering: crispEdges; }
</style>
