<script setup>
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { startBenchmark, cancelBenchmark, getBenchmarkStatus, downloadBenchmarkReport, listBenchmarkReports } from '@/api'
import Spinner from '@/components/Spinner.vue'

/* ── state ── */
const status = ref(null)
const numQuestions = ref(30)
const starting = ref(false)
const error = ref('')
let _poll = null
/* Replay mode: pick a saved run to re-run with the exact same questions */
const availableReports = ref([])
const replayFromRunId = ref('')
async function loadAvailableReports() {
  try { const r = await listBenchmarkReports(); availableReports.value = r?.reports || [] } catch {}
}

const st = computed(() => status.value?.status || 'idle')
const isIdle = computed(() => st.value === 'idle')
const isRunning = computed(() => ['generating', 'running', 'scoring'].includes(st.value))
const isDone = computed(() => st.value === 'done')
const isCancelled = computed(() => st.value === 'cancelled')
const isError = computed(() => st.value === 'error')

/* Which phase index is active (0=idle, 1=gen, 2=run, 3=score, 4=results) */
const activeSection = computed(() => {
  if (isDone.value) return 4
  if (st.value === 'scoring') return 3
  if (st.value === 'running') return 2
  if (st.value === 'generating') return 1
  return 0
})

const reachedSection = ref(0)
watch(activeSection, (v) => { if (v > reachedSection.value) reachedSection.value = v })

const progress = computed(() => {
  if (!status.value?.total) return 0
  return Math.round((status.value.completed / status.value.total) * 100)
})
const elapsed = computed(() => fmtDuration(status.value?.elapsed_ms || 0))
const eta = computed(() => {
  const ms = status.value?.estimated_remaining_ms
  return (!ms || ms <= 0) ? '--' : fmtDuration(ms)
})

function fmtDuration(ms) {
  if (ms < 1000) return '<1s'
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}
function fmtScore(v) { return v == null ? '--' : (v * 100).toFixed(1) + '%' }
function scoreColor(v) {
  if (v == null) return 'text-t3'
  if (v >= 0.8) return 'text-brand'
  if (v >= 0.5) return 'text-t1'
  return 'text-t2'
}
function scoreBg(v) {
  if (v == null) return ''
  if (v >= 0.8) return 'border-brand/30 bg-brand/5'
  return ''
}

/* ── actions ── */
async function doStart() {
  error.value = ''
  starting.value = true
  reachedSection.value = 0
  try {
    await startBenchmark({
      numQuestions: numQuestions.value,
      ...(replayFromRunId.value ? { replayFromRunId: replayFromRunId.value } : {}),
    })
    startPolling()
    scrollToPhases()
  } catch (e) { error.value = e.message || 'Failed to start' }
  finally { starting.value = false }
}

async function doCancel() { try { await cancelBenchmark() } catch {} }

function startPolling() { stopPolling(); poll(); _poll = setInterval(poll, 1500) }
function stopPolling() { if (_poll) { clearInterval(_poll); _poll = null } }

async function poll() {
  try {
    status.value = await getBenchmarkStatus()
    if (status.value && !['generating', 'running', 'scoring'].includes(status.value.status))
      stopPolling()
  } catch {}
}

function doDownload() { window.open(downloadBenchmarkReport(), '_blank') }

function doReset() {
  status.value = { status: 'idle' }
  reachedSection.value = 0
  nextTick(() => scrollEl.value?.scrollTo({ top: 0, behavior: 'smooth' }))
}

/* ── auto-scroll ── */
const scrollEl = ref(null)
const phasesEl = ref(null)     // the full-page phases container
const phaseRefs = ref([])      // individual phase cards

function scrollToPhases() {
  nextTick(() => { phasesEl.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }) })
}

let _prevActive = 0
watch(activeSection, (v) => {
  if (v !== _prevActive && v >= 1) { _prevActive = v; scrollToPhases() }
})

onMounted(() => {
  loadAvailableReports()
  poll().then(() => {
    if (isRunning.value) {
      reachedSection.value = activeSection.value
      startPolling()
      scrollToPhases()
    }
    if (isDone.value) {
      reachedSection.value = 4
      scrollToPhases()
    }
  })
})
onUnmounted(() => stopPolling())
</script>

<template>
  <div ref="scrollEl" class="h-full overflow-y-auto scroll-smooth bg-bg2">

    <!-- ═══ Intro section — fills first screen ═══ -->
    <div class="min-h-full flex flex-col justify-center px-10 py-16">
      <div class="max-w-4xl mx-auto w-full">
        <h1 class="text-3xl font-bold text-t1 tracking-tight">Benchmark</h1>
        <p class="text-sm text-t2 mt-3 leading-relaxed max-w-2xl">
          Automated end-to-end RAG evaluation. Generates test questions from your ingested documents,
          runs the full retrieval + generation pipeline, and scores every answer with an LLM judge.
        </p>

        <!-- Method: 3 phase cards -->
        <div class="grid grid-cols-3 gap-5 mt-10">
          <div class="rounded-xl border border-line p-6">
            <div class="flex items-center gap-2.5 mb-3">
              <div class="w-7 h-7 rounded-full bg-bg3 text-t1 text-xs font-bold flex items-center justify-center">1</div>
              <span class="text-sm font-semibold text-t1">Test Generation</span>
            </div>
            <p class="text-xs text-t2 leading-relaxed">
              An LLM reads sampled chunks from your documents and generates diverse question-answer pairs
              — factual, comparative, numerical, and definitional — to stress-test retrieval breadth.
            </p>
          </div>
          <div class="rounded-xl border border-line p-6">
            <div class="flex items-center gap-2.5 mb-3">
              <div class="w-7 h-7 rounded-full bg-bg3 text-t1 text-xs font-bold flex items-center justify-center">2</div>
              <span class="text-sm font-semibold text-t1">Pipeline Execution</span>
            </div>
            <p class="text-xs text-t2 leading-relaxed">
              Each question runs through the exact same pipeline as Chat:
              query understanding, BM25 + vector + tree retrieval, RRF merge, optional rerank, and LLM generation. Latency is recorded per item.
            </p>
          </div>
          <div class="rounded-xl border border-line p-6">
            <div class="flex items-center gap-2.5 mb-3">
              <div class="w-7 h-7 rounded-full bg-bg3 text-t1 text-xs font-bold flex items-center justify-center">3</div>
              <span class="text-sm font-semibold text-t1">LLM-as-Judge</span>
            </div>
            <p class="text-xs text-t2 leading-relaxed">
              An LLM judge scores each answer on three dimensions (0-100%).
              Aggregate averages are computed across the full set to produce the final benchmark scores.
            </p>
          </div>
        </div>

        <!-- Metric definitions -->
        <div class="grid grid-cols-3 gap-5 mt-5">
          <div class="px-5 py-4 rounded-lg bg-bg3/40">
            <div class="text-xs font-semibold text-t1 mb-1">Faithfulness</div>
            <div class="text-[11px] text-t2 leading-relaxed">
              Does the answer only use information from the retrieved context?
              100% = zero hallucination, every claim is grounded in source passages.
            </div>
          </div>
          <div class="px-5 py-4 rounded-lg bg-bg3/40">
            <div class="text-xs font-semibold text-t1 mb-1">Answer Relevancy</div>
            <div class="text-[11px] text-t2 leading-relaxed">
              Does the answer address the question asked?
              Measures whether the response is on-topic and complete, not just factually correct.
            </div>
          </div>
          <div class="px-5 py-4 rounded-lg bg-bg3/40">
            <div class="text-xs font-semibold text-t1 mb-1">Context Precision</div>
            <div class="text-[11px] text-t2 leading-relaxed">
              Are the retrieved chunks relevant?
              High precision means the retrieval pipeline surfaces the right passages, not noise.
            </div>
          </div>
        </div>

        <!-- Start controls -->
        <div class="mt-10 flex items-end gap-5 flex-wrap">
          <div>
            <label class="text-[10px] text-t3 uppercase tracking-wider block mb-1.5">Questions</label>
            <input v-model.number="numQuestions" type="number" min="5" max="200" step="5"
              :disabled="!!replayFromRunId"
              class="w-24 px-3 py-2 rounded-lg border border-line bg-bg text-sm text-t1 outline-none focus:border-brand disabled:opacity-40" />
          </div>
          <!-- Replay mode: optionally reuse questions from a prior run for strict A/B comparison -->
          <div v-if="availableReports.length">
            <label class="text-[10px] text-t3 uppercase tracking-wider block mb-1.5">Replay from</label>
            <select v-model="replayFromRunId"
              class="px-3 py-2 rounded-lg border border-line bg-bg text-sm text-t1 outline-none focus:border-brand">
              <option value="">— new questions —</option>
              <option v-for="r in availableReports" :key="r.run_id" :value="r.run_id">
                {{ r.run_id }} · {{ r.num_items }}q · CP {{ r.context_precision ?? '?' }}
              </option>
            </select>
          </div>
          <button @click="doStart" :disabled="starting || isRunning"
            class="px-7 py-2 rounded-lg text-sm font-semibold bg-brand text-white hover:opacity-90 disabled:opacity-40 transition-opacity">
            {{ starting ? 'Starting...' : (replayFromRunId ? 'Replay' : 'Start Benchmark') }}
          </button>
          <button v-if="isRunning" @click="doCancel"
            class="px-5 py-2 rounded-lg text-sm border border-line text-t2 hover:bg-bg3 transition-colors">
            Cancel
          </button>
        </div>
        <div v-if="error" class="mt-3 text-xs text-red-500">{{ error }}</div>
        <div v-if="isCancelled" class="mt-4 text-xs text-t3">Benchmark was cancelled after {{ elapsed }}.</div>
        <div v-if="isError" class="mt-4">
          <div class="text-xs text-red-500 font-medium">Benchmark failed</div>
          <pre class="mt-1 text-[10px] text-red-400 whitespace-pre-wrap max-h-28 overflow-y-auto">{{ status.error }}</pre>
        </div>
      </div>
    </div>

    <!-- ═══ Pipeline phases — full page below intro ═══ -->
    <div ref="phasesEl" class="min-h-full flex flex-col justify-center max-w-4xl mx-auto w-full px-10 py-16 space-y-6"
      :class="reachedSection >= 1 ? '' : 'opacity-0 pointer-events-none'">

      <!-- Phase 1: Test generation -->
      <div :ref="el => phaseRefs[0] = el"
        class="rounded-xl border p-6 transition-all duration-500"
        :class="reachedSection >= 1 ? 'border-line' : 'border-line/30 opacity-20'">
        <div class="flex items-center justify-between mb-4">
          <div class="flex items-center gap-3">
            <div class="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold"
              :class="activeSection > 1 ? 'bg-t1 text-white' : activeSection === 1 ? 'bg-brand text-white' : 'bg-bg3 text-t3'">
              <svg v-if="activeSection > 1" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg>
              <span v-else>1</span>
            </div>
            <div>
              <div class="text-sm font-semibold text-t1">Test Generation</div>
              <div class="text-[11px] text-t2">Generating question-answer pairs from document chunks</div>
            </div>
          </div>
          <div v-if="activeSection === 1" class="flex items-center gap-2 text-[11px] text-t3">
            <Spinner size="md" />
            <span>{{ elapsed }}</span>
          </div>
          <div v-else-if="activeSection > 1" class="text-[11px] text-t3">Done</div>
        </div>
        <div class="h-1.5 rounded-full bg-bg3 overflow-hidden">
          <div class="h-full rounded-full transition-all duration-500"
            :class="activeSection > 1 ? 'bg-t1' : 'bg-brand'"
            :style="{ width: (activeSection === 1 ? progress : activeSection > 1 ? 100 : 0) + '%' }"></div>
        </div>
        <div v-if="activeSection === 1" class="flex justify-between mt-2 text-[10px] text-t3">
          <span>{{ status?.completed || 0 }} / {{ status?.total || '?' }} questions</span>
          <span>ETA {{ eta }}</span>
        </div>
      </div>

      <!-- Phase 2: Pipeline execution -->
      <div :ref="el => phaseRefs[1] = el"
        class="rounded-xl border p-6 transition-all duration-500"
        :class="reachedSection >= 2 ? 'border-line' : 'border-line/30 opacity-20'">
        <div class="flex items-center justify-between mb-4">
          <div class="flex items-center gap-3">
            <div class="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold"
              :class="activeSection > 2 ? 'bg-t1 text-white' : activeSection === 2 ? 'bg-brand text-white' : 'bg-bg3 text-t3'">
              <svg v-if="activeSection > 2" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg>
              <span v-else>2</span>
            </div>
            <div>
              <div class="text-sm font-semibold text-t1">Retrieval + Generation</div>
              <div class="text-[11px] text-t2">Running each question through the full RAG pipeline</div>
            </div>
          </div>
          <div v-if="activeSection === 2" class="flex items-center gap-2 text-[11px] text-t3">
            <Spinner size="md" />
            <span>{{ elapsed }}</span>
          </div>
          <div v-else-if="activeSection > 2" class="text-[11px] text-t3">Done</div>
        </div>
        <div class="h-1.5 rounded-full bg-bg3 overflow-hidden">
          <div class="h-full rounded-full transition-all duration-500"
            :class="activeSection > 2 ? 'bg-t1' : 'bg-brand'"
            :style="{ width: (activeSection === 2 ? progress : activeSection > 2 ? 100 : 0) + '%' }"></div>
        </div>
        <div v-if="activeSection === 2" class="flex justify-between mt-2 text-[10px] text-t3">
          <span>{{ status?.completed || 0 }} / {{ status?.total || '?' }} queries</span>
          <span>ETA {{ eta }}</span>
        </div>
      </div>

      <!-- Phase 3: Scoring -->
      <div :ref="el => phaseRefs[2] = el"
        class="rounded-xl border p-6 transition-all duration-500"
        :class="reachedSection >= 3 ? 'border-line' : 'border-line/30 opacity-20'">
        <div class="flex items-center justify-between mb-4">
          <div class="flex items-center gap-3">
            <div class="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold"
              :class="activeSection > 3 ? 'bg-t1 text-white' : activeSection === 3 ? 'bg-brand text-white' : 'bg-bg3 text-t3'">
              <svg v-if="activeSection > 3" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg>
              <span v-else>3</span>
            </div>
            <div>
              <div class="text-sm font-semibold text-t1">LLM-as-Judge Scoring</div>
              <div class="text-[11px] text-t2">Evaluating faithfulness, relevancy, and context precision</div>
            </div>
          </div>
          <div v-if="activeSection === 3" class="flex items-center gap-2 text-[11px] text-t3">
            <Spinner size="md" />
            <span>{{ elapsed }}</span>
          </div>
          <div v-else-if="activeSection > 3" class="text-[11px] text-t3">Done</div>
        </div>
        <div class="h-1.5 rounded-full bg-bg3 overflow-hidden">
          <div class="h-full rounded-full transition-all duration-500"
            :class="activeSection > 3 ? 'bg-t1' : 'bg-brand'"
            :style="{ width: (activeSection === 3 ? progress : activeSection > 3 ? 100 : 0) + '%' }"></div>
        </div>
        <div v-if="activeSection === 3" class="flex justify-between mt-2 text-[10px] text-t3">
          <span>{{ status?.completed || 0 }} / {{ status?.total || '?' }} scored</span>
          <span>ETA {{ eta }}</span>
        </div>
      </div>

      <!-- ═══ Results ═══ -->
      <div v-if="reachedSection >= 4 && isDone && status.scores" class="pt-6">
        <h2 class="text-xl font-bold text-t1 mb-6">Results</h2>

        <!-- Score cards -->
        <div class="grid grid-cols-3 gap-5">
          <div class="rounded-xl border p-6 text-center" :class="scoreBg(status.scores.faithfulness) || 'border-line'">
            <div class="text-[10px] text-t3 uppercase tracking-widest mb-2">Faithfulness</div>
            <div class="text-3xl font-bold" :class="scoreColor(status.scores.faithfulness)">
              {{ fmtScore(status.scores.faithfulness) }}
            </div>
            <div class="text-[10px] text-t3 mt-2">Grounded in context</div>
          </div>
          <div class="rounded-xl border p-6 text-center" :class="scoreBg(status.scores.answer_relevancy) || 'border-line'">
            <div class="text-[10px] text-t3 uppercase tracking-widest mb-2">Answer Relevancy</div>
            <div class="text-3xl font-bold" :class="scoreColor(status.scores.answer_relevancy)">
              {{ fmtScore(status.scores.answer_relevancy) }}
            </div>
            <div class="text-[10px] text-t3 mt-2">Addresses the question</div>
          </div>
          <div class="rounded-xl border p-6 text-center" :class="scoreBg(status.scores.context_precision) || 'border-line'">
            <div class="text-[10px] text-t3 uppercase tracking-widest mb-2">Context Precision</div>
            <div class="text-3xl font-bold" :class="scoreColor(status.scores.context_precision)">
              {{ fmtScore(status.scores.context_precision) }}
            </div>
            <div class="text-[10px] text-t3 mt-2">Relevant chunks retrieved</div>
          </div>
        </div>

        <!-- Summary -->
        <div class="flex gap-8 mt-5 text-xs text-t3">
          <span>Items: <span class="text-t1 font-medium">{{ status.scores.total_items }}</span></span>
          <span>Scored: <span class="text-t1 font-medium">{{ status.scores.scored_items }}</span></span>
          <span>Failed: <span class="text-t1 font-medium">{{ status.scores.failed_items }}</span></span>
          <span>Avg latency: <span class="text-t1 font-medium">{{ status.scores.avg_latency_ms }}ms</span></span>
          <span>Total: <span class="text-t1 font-medium">{{ elapsed }}</span></span>
        </div>

        <!-- Actions -->
        <div class="flex gap-3 mt-6">
          <button @click="doDownload"
            class="flex items-center gap-2 px-5 py-2 rounded-lg text-xs font-semibold bg-brand text-white hover:opacity-90 transition-opacity">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/>
            </svg>
            Download Report
          </button>
          <button @click="doReset"
            class="px-5 py-2 rounded-lg text-xs border border-line text-t2 hover:bg-bg3 transition-colors">
            New Benchmark
          </button>
        </div>

        <!-- Detail table -->
        <div class="mt-8 mb-10">
          <h3 class="text-sm font-semibold text-t1 mb-3">Per-Question Detail</h3>
          <div class="rounded-xl border border-line overflow-hidden">
            <table class="w-full text-[11px]">
              <thead>
                <tr class="bg-bg3 text-t3 text-left">
                  <th class="px-3 py-2 font-medium w-8">#</th>
                  <th class="px-3 py-2 font-medium">Question</th>
                  <th class="px-3 py-2 font-medium w-20 text-center">Faith.</th>
                  <th class="px-3 py-2 font-medium w-20 text-center">Relev.</th>
                  <th class="px-3 py-2 font-medium w-20 text-center">Prec.</th>
                  <th class="px-3 py-2 font-medium w-16 text-right">Latency</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(item, i) in status.items" :key="i"
                  class="border-t border-line hover:bg-bg3/40 transition-colors">
                  <td class="px-3 py-2 text-t3 tabular-nums">{{ i + 1 }}</td>
                  <td class="px-3 py-2 text-t1">
                    <div class="line-clamp-2 leading-relaxed">{{ item.question }}</div>
                    <div v-if="item.doc_title" class="text-[10px] text-t3 mt-0.5 truncate">{{ item.doc_title }}</div>
                    <div v-if="item.error" class="text-[10px] text-red-400 mt-0.5">{{ item.error }}</div>
                  </td>
                  <td class="px-3 py-2 text-center font-medium tabular-nums" :class="scoreColor(item.faithfulness)">
                    {{ fmtScore(item.faithfulness) }}
                  </td>
                  <td class="px-3 py-2 text-center font-medium tabular-nums" :class="scoreColor(item.relevancy)">
                    {{ fmtScore(item.relevancy) }}
                  </td>
                  <td class="px-3 py-2 text-center font-medium tabular-nums" :class="scoreColor(item.context_precision)">
                    {{ fmtScore(item.context_precision) }}
                  </td>
                  <td class="px-3 py-2 text-right text-t3 tabular-nums">{{ item.latency_ms }}ms</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

    </div>

  </div>
</template>
