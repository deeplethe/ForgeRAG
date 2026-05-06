<!--
  Search view — semantic (embedding) search over passages.

  Calls POST /api/v1/search which now runs a dense ANN pass: the
  query gets embedded once, the vector index returns top-K
  passages by cosine similarity, then they're hydrated with
  filename / folder path / page so each row links back to the
  source. Cross-lingual recall comes for free — searching ``蜜蜂``
  surfaces English passages mentioning ``bees`` because the
  multilingual embedder maps both into the same space.

  Distinct from Chat (which calls /agent and gets a streamed
  LLM answer with citations). This page is for navigation /
  discovery — click a row to jump to the source doc + scroll
  the right pane to that passage.

  No more files-vs-passages split: the vector backend returns
  ranked chunks, each row carries enough file context (filename,
  folder, page) inline that a separate file rollup would be
  redundant. Click → opens DocDetail with &chunk=… set so the
  preview lands on the matched passage.

  No more keyword highlighting: there are no "matched tokens"
  with semantic search — the score is similarity, not term
  overlap. The snippet renders plain. Score is shown so users
  who care about "how confident is this match" have the signal.

  Module-level refs persist last query + results across nav.
-->
<script>
import { ref } from 'vue'
export default { name: 'SearchView' }
const _query = ref('')
const _results = ref(null)   // { chunks: [...], stats: {...} } | null
const _loading = ref(false)
const _error = ref('')
</script>

<script setup>
import { computed, onMounted, ref as setupRef } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Search, FileSearch, AlertCircle, Loader2 } from 'lucide-vue-next'

import { search as searchApi } from '@/api'

const { t } = useI18n()
const router = useRouter()

const inputEl = setupRef(null)

onMounted(() => {
  if (inputEl.value) inputEl.value.focus()
})

async function runSearch() {
  const q = _query.value.trim()
  if (!q) return
  _loading.value = true
  _error.value = ''
  try {
    const res = await searchApi({
      query: q,
      include: ['chunks'],
      limit: { chunks: 30 },
    })
    _results.value = res
  } catch (e) {
    _error.value = (e && e.message) || String(e)
    _results.value = null
  } finally {
    _loading.value = false
  }
}

function clearAll() {
  _query.value = ''
  _results.value = null
  _error.value = ''
  if (inputEl.value) inputEl.value.focus()
}

function openDoc(docId, chunkId) {
  const q = { doc: docId }
  if (chunkId) q.chunk = chunkId
  router.push({ path: '/workspace', query: q })
}

const chunks = computed(() => _results.value?.chunks || [])
const stats = computed(() => _results.value?.stats || null)
const hasResults = computed(() => chunks.value.length > 0)
</script>

<template>
  <div class="flex flex-col h-full bg-bg overflow-hidden">
    <!-- ── Header / search bar ──────────────────────────────────── -->
    <header class="shrink-0 px-8 pt-8 pb-5 border-b border-line">
      <h1 class="text-[22px] font-semibold text-t1 m-0">{{ t('search.title') }}</h1>
      <p class="mt-1.5 mb-4 text-[13px] text-t3">{{ t('search.subtitle') }}</p>

      <form class="flex items-center gap-2 max-w-[720px]" @submit.prevent="runSearch">
        <div class="relative flex-1 flex items-center px-4 py-2.5 rounded-xl border border-line shadow-sm bg-bg">
          <Search :size="16" class="text-t3 shrink-0" />
          <input
            ref="inputEl"
            v-model="_query"
            type="text"
            class="flex-1 bg-transparent border-none outline-none text-sm text-t1 leading-relaxed pl-3 pr-2"
            :placeholder="t('search.placeholder')"
            :disabled="_loading"
            autocomplete="off"
            spellcheck="false"
          />
          <button
            v-if="_query"
            type="button"
            class="w-5 h-5 flex items-center justify-center text-t3 hover:text-t1 hover:bg-bg3 rounded transition-colors text-base leading-none shrink-0"
            @click="clearAll"
            :title="t('search.clear')"
          >×</button>
        </div>
        <button
          type="submit"
          class="shrink-0 min-w-[84px] h-[42px] px-4 text-[13px] font-medium rounded-lg flex items-center justify-center gap-1.5 transition-colors"
          :class="(_loading || !_query.trim())
            ? 'bg-bg3 text-t3 cursor-not-allowed'
            : 'bg-brand text-white hover:opacity-90'"
          :disabled="_loading || !_query.trim()"
        >
          <Loader2 v-if="_loading" :size="14" class="animate-spin" />
          <span v-else>{{ t('search.submit') }}</span>
        </button>
      </form>
    </header>

    <!-- ── Body ──────────────────────────────────────────────────── -->
    <main class="flex-1 overflow-y-auto px-8 pt-5 pb-10">
      <div v-if="_error" class="flex items-center gap-2 px-3.5 py-2.5 text-[13px] text-red-600 bg-red-500/[0.08] border border-red-500/20 rounded-md max-w-[720px]">
        <AlertCircle :size="16" />
        <span>{{ _error }}</span>
      </div>

      <div v-else-if="_loading && !_results" class="flex items-center justify-center h-[60vh] text-t3 text-[13px]">
        {{ t('search.empty.searching') }}
      </div>

      <div v-else-if="!_results" class="flex flex-col items-center justify-center h-[60vh] text-t3 text-center text-[13px]">
        <FileSearch :size="36" class="text-t3 opacity-40 mb-3" />
        <p>{{ t('search.empty.idle') }}</p>
        <p class="text-[12px] mt-1.5 opacity-70">{{ t('search.empty.hint') }}</p>
      </div>

      <div v-else-if="!hasResults" class="flex flex-col items-center justify-center h-[60vh] text-t3 text-center text-[13px]">
        <FileSearch :size="36" class="text-t3 opacity-40 mb-3" />
        <p>{{ t('search.empty.none', { query: _query }) }}</p>
      </div>

      <div v-else class="max-w-[880px] flex flex-col gap-2">
        <ul class="list-none p-0 m-0 flex flex-col gap-2">
          <li
            v-for="c in chunks"
            :key="c.chunk_id"
            class="px-3.5 py-3 bg-bg border border-line rounded-md cursor-pointer hover:border-t3 hover:bg-bg2 transition-colors"
            @click="openDoc(c.doc_id, c.chunk_id)"
          >
            <!-- Row header: filename · path · page · score -->
            <div class="flex items-center gap-2 mb-1.5 text-[12px] text-t3 min-w-0">
              <span class="text-t2 font-medium truncate">{{ c.filename || c.doc_id }}</span>
              <span v-if="c.path" class="truncate">· {{ c.path }}</span>
              <span v-if="c.page_no" class="shrink-0">· {{ t('search.page', { n: c.page_no }) }}</span>
              <span class="ml-auto shrink-0 text-[11px] tabular-nums">{{ c.score?.toFixed(3) }}</span>
            </div>
            <div class="text-[13px] text-t1 leading-relaxed">{{ c.snippet }}</div>
          </li>
        </ul>

        <div v-if="stats" class="mt-3 pt-3 border-t border-line text-[12px] text-t3">
          {{ t('search.footer_v2', {
            chunks: stats.chunk_hits ?? 0,
            ms: stats.elapsed_ms ?? 0,
          }) }}
        </div>
      </div>
    </main>
  </div>
</template>
