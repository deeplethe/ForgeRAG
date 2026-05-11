<template>
  <div class="metrics-page">
    <!-- Header: range toggle + refresh -->
    <header class="m-header">
      <div>
        <h1 class="text-[0.8125rem] text-t1 font-medium">Metrics</h1>
        <p class="text-3xs text-t3 mt-0.5">Query &amp; ingestion health. Derived from <code>query_traces</code> + <code>documents</code>.</p>
      </div>
      <div class="flex items-center gap-3">
        <div class="range-toggle">
          <button
            v-for="r in RANGES" :key="r"
            :class="['range-btn', { active: range === r }]"
            @click="selectRange(r)"
          >{{ r }}</button>
        </div>
        <button class="btn-ghost" @click="refresh" :disabled="loading">
          {{ loading ? '…' : 'refresh' }}
        </button>
        <!-- ``min-width`` so the auto-ticking label ("5s ago" → "12s ago"
             → "1m ago") doesn't keep changing the controls-block width;
             space-between would otherwise propagate that into a sideways
             jitter on the range-toggle every second. -->
        <span class="text-3xs text-t3 tabular updated-label">
          <span v-if="lastRefreshedLabel">updated {{ lastRefreshedLabel }}</span>
        </span>
      </div>
    </header>

    <!-- KPI strip -->
    <section class="kpi-strip">
      <span v-if="err" class="err">error: {{ err }}</span>
      <template v-else-if="summary">
        <span class="kpi"><b>{{ summary.queries }}</b> queries</span>
        <span class="kpi"><b>{{ fmtMs(summary.p50_ms) }}</b> p50</span>
        <span class="kpi"><b>{{ fmtMs(summary.p95_ms) }}</b> p95</span>
        <span class="kpi"><b>{{ summary.queries_per_hour.toFixed(1) }}</b>/hr</span>
        <span class="kpi"><b>{{ fmtTokens(summary.tokens_total) }}</b> tokens</span>
        <span class="kpi sep">·</span>
        <span class="kpi"><b :style="{ color: 'var(--color-ok-fg)' }">{{ summary.ingest_ok }}</b> ready</span>
        <span class="kpi"><b :style="{ color: summary.ingest_failed ? 'var(--color-err-fg)' : undefined }">{{ summary.ingest_failed }}</b> failed</span>
        <span v-if="summary.ingest_in_progress" class="kpi"><b>{{ summary.ingest_in_progress }}</b> in progress</span>
      </template>
      <template v-else>
        <span v-for="i in 6" :key="'sk' + i" class="kpi">
          <Skeleton :w="32" :h="14" /> <Skeleton :w="40" :h="11" />
        </span>
      </template>
    </section>

    <!-- Chart grid (2 x 2) -->
    <section class="chart-grid">
      <div class="panel">
        <div class="panel-head">Query latency (p50 / p95)</div>
        <LineChart
          :points="latencyPoints"
          :series="[
            { key: 'p50_ms', label: 'p50', color: 'var(--color-t1)' },
            { key: 'p95_ms', label: 'p95', color: 'var(--color-t3)' },
          ]"
          :y-format="v => fmtMs(v)"
          :x-format="v => fmtTick(v)"
        />
      </div>

      <div class="panel">
        <div class="panel-head">Tokens by model</div>
        <StackedBars
          :points="tokensPoints"
          :x-format="v => fmtTick(v)"
          :value-format="v => fmtTokens(v)"
          integer
        />
      </div>

      <div class="panel">
        <div class="panel-head">Per-path avg / p95</div>
        <HBars :items="pathTiming" />
      </div>

      <div class="panel">
        <div class="panel-head">Queries per {{ bucketLabel }}</div>
        <LineChart
          :points="latencyPoints"
          :series="[{ key: 'count', label: 'count', color: 'var(--color-t1)' }]"
          :y-format="v => Math.round(v).toString()"
          :x-format="v => fmtTick(v)"
          integer
        />
      </div>
    </section>

    <!-- Per-user token + cost table -->
    <section v-if="userUsage.length" class="panel panel-table">
      <div class="panel-head">
        Tokens by user <span class="text-t3">· lifetime</span>
        <span v-if="costConfigured" class="text-t3"> · cost @ ${{ costInPer1M }}/M in · ${{ costOutPer1M }}/M out</span>
        <span v-else class="text-t3"> · cost not configured (set <code>answering.generator.input_cost_per_1m_usd</code>)</span>
      </div>
      <table class="t">
        <thead>
          <tr>
            <th>user</th>
            <th class="w-[80px] tabular">input</th>
            <th class="w-[80px] tabular">output</th>
            <th class="w-[90px] tabular">total</th>
            <th class="w-[70px] tabular">answers</th>
            <th v-if="costConfigured" class="w-[80px] tabular">cost</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in userUsage" :key="u.user_id">
            <td class="truncate">{{ u.display_name || u.email || u.username || u.user_id }}</td>
            <td class="tabular text-t3">{{ (u.input_tokens || 0).toLocaleString() }}</td>
            <td class="tabular text-t3">{{ (u.output_tokens || 0).toLocaleString() }}</td>
            <td class="tabular">{{ (u.total_tokens || 0).toLocaleString() }}</td>
            <td class="tabular text-t3">{{ (u.message_count || 0).toLocaleString() }}</td>
            <td v-if="costConfigured" class="tabular">{{ fmtCost(u.total_cost_usd) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- Slow queries table -->
    <section class="panel panel-table">
      <div class="panel-head">Slow queries <span class="text-t3">· top 10</span></div>
      <table v-if="slow.length" class="t">
        <thead>
          <tr>
            <th class="w-[90px]">when</th>
            <th class="w-[72px] tabular">ms</th>
            <th>query</th>
            <th class="w-[160px]">model</th>
            <th class="w-[52px] tabular">cites</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in slow" :key="r.trace_id" class="row-link" @click="openTrace(r.trace_id)">
            <td class="text-t3">{{ fmtAgo(r.ts) }}</td>
            <td class="tabular" :class="r.total_ms > 5000 ? '' : 'text-t2'"
                :style="r.total_ms > 5000 ? { color: 'var(--color-err-fg)' } : undefined">{{ r.total_ms }}</td>
            <td class="truncate" :title="r.query">{{ r.query }}</td>
            <td class="text-t3 truncate">{{ r.answer_model || '—' }}</td>
            <td class="tabular text-t3">{{ r.citations }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">no queries in range</div>
    </section>

    <!-- Ingestion failures table -->
    <section class="panel panel-table">
      <div class="panel-head">Recent ingestion failures <span class="text-t3">· top 10</span></div>
      <table v-if="failures.length" class="t">
        <thead>
          <tr>
            <th class="w-[90px]">when</th>
            <th>file</th>
            <th class="w-[160px]">folder</th>
            <th class="w-[56px]">type</th>
            <th>error</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in failures" :key="r.doc_id" class="row-link" @click="openDoc(r.doc_id)">
            <td class="text-t3">{{ fmtAgo(r.ts) }}</td>
            <td class="truncate text-t2" :title="r.file_name">{{ r.file_name || r.doc_id }}</td>
            <td class="text-t3 truncate">{{ r.folder_path || '/' }}</td>
            <td class="text-t3">{{ r.format || '?' }}</td>
            <td class="truncate" :title="r.error_message" style="color: var(--color-err-fg);">{{ r.error_message || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">no failures — clean slate</div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { get } from '@/api/client'
import { listUserUsage } from '@/api/admin'
import LineChart from '@/components/metrics/LineChart.vue'
import StackedBars from '@/components/metrics/StackedBars.vue'
import HBars from '@/components/metrics/HBars.vue'
import Skeleton from '@/components/Skeleton.vue'

const RANGES = ['24h', '7d', '30d']

const router = useRouter()
const range = ref('24h')
const loading = ref(false)
const err = ref('')
const lastRefreshedAt = ref(null)

const summary = ref(null)
const latency = ref([])
const tokens = ref([])
const pathTiming = ref([])
const slow = ref([])
const failures = ref([])
// Admin-only; a 403 just leaves it empty so the panel disappears.
const userUsage = ref([])
// Cost rates from /health — drives whether the cost column shows
// and the header note labels (e.g. "$3/M in · $15/M out").
const costInPer1M = ref(0)
const costOutPer1M = ref(0)
const costConfigured = computed(() => costInPer1M.value > 0 || costOutPer1M.value > 0)
// USD formatter — sub-cent values would otherwise render as "$0.00"
// which reads as "free", misleading users who have real usage.
// Showing "<$0.01" preserves the "tiny but nonzero" signal.
function fmtCost(v) {
  const n = Number(v) || 0
  if (n <= 0) return '$0.00'
  if (n < 0.01) return '<$0.01'
  return `$${n.toFixed(2)}`
}

const bucketLabel = computed(() => ({ '24h': '15 min', '7d': 'hour', '30d': '6 hrs' }[range.value]))

const latencyPoints = computed(() =>
  latency.value.map(p => ({
    x: new Date(p.ts),
    p50_ms: p.p50_ms,
    p95_ms: p.p95_ms,
    count: p.count,
  })),
)

const tokensPoints = computed(() =>
  tokens.value.flatMap(p => [
    { ts: new Date(p.ts), model: p.model, value: (p.prompt_tokens || 0) + (p.completion_tokens || 0) },
  ]),
)

async function refresh() {
  loading.value = true
  err.value = ''
  try {
    const r = range.value
    const [sum, lat, tok, pth, slw, fl] = await Promise.all([
      get(`/api/v1/metrics/summary`, { range: r }),
      get(`/api/v1/metrics/query/latency`, { range: r }),
      get(`/api/v1/metrics/query/tokens`, { range: r }),
      get(`/api/v1/metrics/query/path-timing`, { range: r }),
      get(`/api/v1/metrics/query/slow`, { range: r, limit: 10 }),
      get(`/api/v1/metrics/ingestion/recent-failures`, { limit: 10 }),
    ])
    summary.value = sum
    latency.value = lat || []
    tokens.value = tok || []
    pathTiming.value = pth || []
    slow.value = slw || []
    failures.value = fl || []

    // Per-user usage runs in its own try so a non-admin's 403 here
    // doesn't kill the rest of the metrics page.
    try {
      userUsage.value = (await listUserUsage()) || []
    } catch {
      userUsage.value = []
    }
    // Health snapshot for cost-rate display ("cost @ $3/M in").
    // Same endpoint that powers the context-window ring — cached
    // per refresh, so the rate stays consistent across the page.
    try {
      const h = await fetch('/api/v1/health', { credentials: 'include' }).then(r => r.json())
      costInPer1M.value = h?.features?.input_cost_per_1m_usd || 0
      costOutPer1M.value = h?.features?.output_cost_per_1m_usd || 0
    } catch { /* non-fatal */ }
    lastRefreshedAt.value = Date.now()
  } catch (e) {
    err.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

function selectRange(r) {
  if (r === range.value) return
  range.value = r
  // Clear time-series data so charts don't visibly "snap" from the old
  // time range to the new one when data arrives. KPI summary + tables
  // stay (they're general-purpose, not range-axis-bound) for continuity.
  latency.value = []
  tokens.value = []
  pathTiming.value = []
  refresh()
}

// Auto-refresh every 30s while on this page; clears on unmount
let timer = null
onMounted(() => {
  refresh()
  timer = setInterval(refresh, 30_000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })

// Ticker: updates the "updated N seconds ago" label once a second
const tick = ref(0)
let tickTimer = null
onMounted(() => { tickTimer = setInterval(() => { tick.value++ }, 1000) })
onUnmounted(() => { if (tickTimer) clearInterval(tickTimer) })

const lastRefreshedLabel = computed(() => {
  void tick.value
  if (!lastRefreshedAt.value) return ''
  const s = Math.max(0, Math.floor((Date.now() - lastRefreshedAt.value) / 1000))
  return s < 5 ? 'just now' : `${s}s ago`
})

function openTrace(tid) {
  // For MVP just copy the id. Could deep-link to a trace viewer later.
  navigator.clipboard?.writeText(tid)
}
function openDoc(docId) {
  router.push({ path: '/library', query: { doc: docId } })
}

function fmtMs(ms) {
  if (ms == null) return '—'
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}
function fmtTokens(n) {
  if (n == null) return '0'
  // Defensive: integer flag on charts now snaps ticks to whole numbers, but
  // round here too in case fractional values slip through (e.g., bucketed
  // averages elsewhere).
  const r = Math.round(n)
  if (r < 1000) return `${r}`
  if (r < 1_000_000) return `${(r / 1000).toFixed(1)}k`
  return `${(r / 1_000_000).toFixed(2)}M`
}
function fmtTick(v) {
  const d = v instanceof Date ? v : new Date(v)
  if (range.value === '24h') {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleDateString([], { month: 'numeric', day: 'numeric' })
}
function fmtAgo(iso) {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}
</script>

<style scoped>
.metrics-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  padding: 0 0 24px;
  background: var(--color-bg2);   /* canvas */
}

/* Page header — Vercel uses spacing alone, no border-bottom */
.m-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 24px 14px;
  flex-shrink: 0;
}

.range-toggle {
  display: inline-flex;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  overflow: hidden;
  background: var(--color-bg);
}
.range-btn {
  padding: 4px 12px;
  font-size: 0.6875rem;
  color: var(--color-t3);
  background: var(--color-bg);
  border: none;
  border-right: 1px solid var(--color-line);
  cursor: pointer;
  font-variant-numeric: tabular-nums;
}
.range-btn:last-child { border-right: none; }
.range-btn:hover { background: var(--color-bg2); color: var(--color-t1); }
.range-btn.active { background: var(--color-t1); color: var(--color-bg); }

/* Width floor for the auto-ticking "updated Ns ago" label — see the
   comment next to its element in the template. */
.updated-label {
  display: inline-block;
  min-width: 80px;
  text-align: right;
}

.btn-ghost {
  padding: 4px 10px;
  font-size: 0.6875rem;
  color: var(--color-t3);
  background: transparent;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  cursor: pointer;
  /* Lock width so loading-state ``…`` and ``refresh`` produce the same
     button bounding box. Without this the header is right-anchored
     (justify-content: space-between) so any width swap on the right
     ripples back into the range-toggle and the whole bar jitters. */
  min-width: 64px;
  text-align: center;
}
.btn-ghost:hover:not(:disabled) { color: var(--color-t1); background: var(--color-bg2); }
.btn-ghost:disabled { opacity: 0.4; cursor: wait; }

.tabular { font-variant-numeric: tabular-nums; }

/* KPI strip — its own card, no border-bottom slicing the page */
.kpi-strip {
  margin: 0 24px 14px;
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 20px;
  padding: 16px 20px;
  font-size: 0.75rem;
  color: var(--color-t2);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  flex-shrink: 0;
  min-height: 56px;          /* lock height across loading / empty states */
}
.kpi b { font-variant-numeric: tabular-nums; }   /* fixed-width digits → no width jitter when numbers change */
.kpi { font-variant-numeric: tabular-nums; }
.kpi b { font-size: 0.875rem; color: var(--color-t1); font-weight: 600; margin-right: 3px; }
.kpi.sep { color: var(--color-line); margin: 0 -10px; }
.err { color: var(--color-err-fg); font-size: 0.6875rem; }

.chart-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  padding: 0 24px 14px;
  flex-shrink: 0;
}

/* Panels reuse global .panel; only add internal padding here.
   min-height locks vertical layout across loading / empty / populated
   states so range-toggle clicks don't make the page jolt. */
.panel { padding: 18px 20px; min-width: 0; min-height: 220px; }
.panel-head {
  font-size: 0.6875rem;
  color: var(--color-t2);
  font-weight: 500;
  margin-bottom: 12px;
}
.panel-table { margin: 0 24px 14px; padding: 16px 0 0; }
.panel-table .panel-head { padding: 0 20px; }

.t { width: 100%; border-collapse: collapse; font-size: 0.6875rem; }
.t thead tr { background: var(--color-bg2); }
.t th {
  padding: 9px 16px;
  font-weight: 500;
  font-size: 0.5625rem;
  color: var(--color-t3);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  text-align: left;
  border-top: 1px solid var(--color-line);
  border-bottom: 1px solid var(--color-line);
  white-space: nowrap;
}
.t th:first-child { padding-left: 20px; }
.t th:last-child { padding-right: 20px; }
.t td {
  padding: 10px 16px;
  border-top: 1px solid var(--color-line);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 0;
}
.t td:first-child { padding-left: 20px; }
.t td:last-child { padding-right: 20px; }
.t tbody tr:first-child td { border-top: none; }
.row-link { cursor: pointer; }
.row-link:hover td { background: var(--color-bg2); }
.truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty { padding: 24px 16px; text-align: center; color: var(--color-t3); font-size: 0.6875rem; }
</style>
