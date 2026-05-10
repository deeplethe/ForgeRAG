<template>
  <div class="lc" ref="root">
    <svg v-if="W > 0 && H > 0" :width="W" :height="H" class="lc-svg">
      <!-- y-axis grid + labels -->
      <g v-for="(g, i) in gridY" :key="'g' + i">
        <line :x1="padL" :y1="g.y" :x2="W - padR" :y2="g.y" class="grid" />
        <text :x="padL - 4" :y="g.y + 3" class="axis-label" text-anchor="end">{{ g.label }}</text>
      </g>
      <!-- x-axis labels (sparse) -->
      <text v-for="(t, i) in xTicks" :key="'x' + i"
        :x="t.x" :y="H - 4" class="axis-label" text-anchor="middle">{{ t.label }}</text>

      <!-- series -->
      <path v-for="s in seriesPaths" :key="s.key"
        :d="s.d" :stroke="s.color" fill="none" stroke-width="1.5" class="line" />

      <!-- dots (optional, only if few points) -->
      <g v-if="points.length <= 40">
        <circle v-for="(p, i) in dots" :key="'d' + i"
          :cx="p.x" :cy="p.y" :r="2" :fill="p.color" class="dot" />
      </g>

      <!-- legend -->
      <g class="legend" :transform="`translate(${padL}, ${padT - 4})`">
        <g v-for="(s, i) in series" :key="'l' + i" :transform="`translate(${i * 60}, 0)`">
          <line x1="0" y1="0" x2="12" y2="0" :stroke="s.color" stroke-width="1.5" />
          <text x="16" y="3" class="legend-label">{{ s.label }}</text>
        </g>
      </g>
    </svg>
    <div v-if="!points.length" class="lc-empty">no data</div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { niceTicks } from './niceTicks.js'

const props = defineProps({
  // points: [{ x (number|Date), [key1]: value, [key2]: value }]
  points: { type: Array, default: () => [] },
  // series: [{ key: 'p50_ms', label: 'p50', color: '#xxx' }]
  series: { type: Array, default: () => [] },
  yFormat: { type: Function, default: (v) => Math.round(v) },
  xFormat: { type: Function, default: (v) => '' },
  /** When true, axis ticks snap to integer values. */
  integer: { type: Boolean, default: false },
})

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

const xValues = computed(() => props.points.map(p => p.x instanceof Date ? p.x.getTime() : +p.x))
const xMin = computed(() => Math.min(...xValues.value))
const xMax = computed(() => Math.max(...xValues.value))

const allValues = computed(() => {
  const out = []
  for (const p of props.points) {
    for (const s of props.series) {
      const v = p[s.key]
      if (v != null && !Number.isNaN(v)) out.push(v)
    }
  }
  return out
})
const yMin = computed(() => 0)
const rawMax = computed(() => allValues.value.length ? Math.max(...allValues.value) : 0)
const scale = computed(() => niceTicks(0, rawMax.value, 4, { integer: props.integer }))
const yMax = computed(() => scale.value.max)

function sx(v) {
  const span = xMax.value - xMin.value || 1
  return padL + ((v - xMin.value) / span) * innerW.value
}
function sy(v) {
  const span = yMax.value - yMin.value || 1
  return padT + innerH.value - ((v - yMin.value) / span) * innerH.value
}

const seriesPaths = computed(() => {
  return props.series.map((s) => {
    const parts = []
    let drawing = false
    for (const p of props.points) {
      const xv = p.x instanceof Date ? p.x.getTime() : +p.x
      const v = p[s.key]
      if (v == null || Number.isNaN(v)) { drawing = false; continue }
      const cmd = drawing ? 'L' : 'M'
      parts.push(`${cmd}${sx(xv).toFixed(1)},${sy(v).toFixed(1)}`)
      drawing = true
    }
    return { key: s.key, color: s.color, d: parts.join(' ') }
  })
})

const dots = computed(() => {
  const out = []
  for (const p of props.points) {
    const xv = p.x instanceof Date ? p.x.getTime() : +p.x
    for (const s of props.series) {
      const v = p[s.key]
      if (v == null) continue
      out.push({ x: sx(xv), y: sy(v), color: s.color })
    }
  }
  return out
})

const gridY = computed(() => {
  const ticks = scale.value.ticks
  if (!ticks.length) return []
  return ticks.map((v) => ({ y: sy(v), label: props.yFormat(v) }))
})

const xTicks = computed(() => {
  if (!props.points.length) return []
  const n = props.points.length
  // Show about 5 evenly spaced ticks
  const want = Math.min(5, n)
  const step = Math.max(1, Math.floor((n - 1) / (want - 1 || 1)))
  const out = []
  for (let i = 0; i < n; i += step) {
    const p = props.points[i]
    const xv = p.x instanceof Date ? p.x.getTime() : +p.x
    out.push({ x: sx(xv), label: props.xFormat(p.x) })
  }
  return out
})
</script>

<style scoped>
.lc { position: relative; width: 100%; }
.lc-svg { display: block; }
.lc-empty {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 0.6875rem; color: var(--color-t3);
}
.grid { stroke: var(--color-line); stroke-width: 0.5; }
.axis-label { font-size: 0.5625rem; fill: var(--color-t3); }
.legend-label { font-size: 0.5625rem; fill: var(--color-t2); dominant-baseline: middle; }
.dot { opacity: 0.9; }
</style>
