<!--
  Search view — semantic (embedding) search over passages, with a
  two-pane "file → passages" layout.

  Calls POST /api/v1/search which now runs a dense ANN pass: the
  query gets embedded once, the vector index returns top-K
  passages by cosine similarity, then they're hydrated with
  filename / folder path / page so each row links back to the
  source. Cross-lingual recall comes for free — searching ``蜜蜂``
  surfaces English passages mentioning ``bees`` because the
  multilingual embedder maps both into the same space.

  Layout:
    * LEFT pane — one row per matching file, ranked by the best
      chunk's score within that file. Click a row to select.
    * RIGHT pane — shows all matching passages for the selected
      file, in document order (by page, then chunk_id). Click a
      passage to open the source doc scrolled to that passage.

  The grouping is done client-side from the flat ``chunks`` list
  the backend returns — the API contract stays simple, the UI
  handles the rollup.

  Module-level refs persist last query + results across nav.
-->
<script>
import { ref } from 'vue'
export default { name: 'SearchView' }
const _query = ref('')
const _results = ref(null)   // { chunks: [...], stats: {...} } | null
const _loading = ref(false)
const _error = ref('')
const _selectedDocId = ref(null) // sticks across nav so user comes back to the same file
</script>

<script setup>
import { computed, onMounted, ref as setupRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Search, FileSearch, AlertCircle, Loader2, FileText, ExternalLink } from 'lucide-vue-next'

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
      limit: { chunks: 60 },
    })
    _results.value = res
    // Auto-select the first (highest-ranked) file so the right
    // pane has content to show. The user can click another file
    // to swap; this just avoids a "blank right pane" on land.
    _selectedDocId.value = files.value[0]?.doc_id || null
  } catch (e) {
    _error.value = (e && e.message) || String(e)
    _results.value = null
    _selectedDocId.value = null
  } finally {
    _loading.value = false
  }
}

function clearAll() {
  _query.value = ''
  _results.value = null
  _selectedDocId.value = null
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

// ── File rollup ───────────────────────────────────────────────────
// Group chunks by doc_id. Each file carries:
//   - top score (max chunk score) — drives the left-pane ranking
//   - hit count (how many of its chunks matched)
//   - all chunks for the right-pane detail view
// Sort: by top score DESC. Within a file, chunks are ordered by
// page, then by descending score (so the strongest match in a
// given page rank wins ties).
const files = computed(() => {
  const map = new Map()
  for (const c of chunks.value) {
    let f = map.get(c.doc_id)
    if (!f) {
      f = {
        doc_id: c.doc_id,
        filename: c.filename || c.doc_id,
        path: c.path || '',
        topScore: c.score || 0,
        chunks: [],
      }
      map.set(c.doc_id, f)
    }
    f.chunks.push(c)
    if ((c.score || 0) > f.topScore) f.topScore = c.score
  }
  for (const f of map.values()) {
    f.chunks.sort((a, b) => {
      const pa = a.page_no || 0
      const pb = b.page_no || 0
      if (pa !== pb) return pa - pb
      return (b.score || 0) - (a.score || 0)
    })
  }
  return Array.from(map.values()).sort((a, b) => b.topScore - a.topScore)
})

const selectedFile = computed(() =>
  files.value.find((f) => f.doc_id === _selectedDocId.value) || null,
)

// If the selected doc disappears from a fresh result set (e.g.
// user re-runs the same query and the backend returns a different
// top-K), fall back to whatever's first.
watch(files, (list) => {
  if (!list.length) {
    _selectedDocId.value = null
    return
  }
  if (!list.some((f) => f.doc_id === _selectedDocId.value)) {
    _selectedDocId.value = list[0].doc_id
  }
})

const hasResults = computed(() => files.value.length > 0)
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

    <!-- ── Empty / error / loading states (full-width, no panes) ── -->
    <div v-if="_error" class="px-8 pt-5">
      <div class="flex items-center gap-2 px-3.5 py-2.5 text-[13px] text-red-600 bg-red-500/[0.08] border border-red-500/20 rounded-md max-w-[720px]">
        <AlertCircle :size="16" />
        <span>{{ _error }}</span>
      </div>
    </div>

    <div v-else-if="_loading && !_results" class="flex-1 flex items-center justify-center text-t3 text-[13px]">
      {{ t('search.empty.searching') }}
    </div>

    <div v-else-if="!_results" class="flex-1 flex flex-col items-center justify-center text-t3 text-center text-[13px]">
      <FileSearch :size="36" class="text-t3 opacity-40 mb-3" />
      <p>{{ t('search.empty.idle') }}</p>
      <p class="text-[12px] mt-1.5 opacity-70">{{ t('search.empty.hint') }}</p>
    </div>

    <div v-else-if="!hasResults" class="flex-1 flex flex-col items-center justify-center text-t3 text-center text-[13px]">
      <FileSearch :size="36" class="text-t3 opacity-40 mb-3" />
      <p>{{ t('search.empty.none', { query: _query }) }}</p>
    </div>

    <!-- ── Two-pane results body ────────────────────────────────── -->
    <main v-else class="flex-1 flex min-h-0">
      <!-- Left: file list -->
      <aside class="w-[340px] shrink-0 border-r border-line overflow-y-auto bg-bg2/40">
        <div class="px-4 py-3 border-b border-line text-[11px] uppercase tracking-wider text-t3 sticky top-0 bg-bg2/95 backdrop-blur z-10">
          {{ t('search.section_files', { n: files.length }) }}
        </div>
        <ul class="list-none p-0 m-0">
          <li
            v-for="f in files"
            :key="f.doc_id"
            class="px-4 py-3 cursor-pointer border-b border-line/60 transition-colors"
            :class="_selectedDocId === f.doc_id
              ? 'bg-bg-selected'
              : 'hover:bg-bg3/50'"
            @click="_selectedDocId = f.doc_id"
          >
            <div class="flex items-start gap-2 min-w-0">
              <FileText :size="13" :stroke-width="1.75" class="text-t3 shrink-0 mt-0.5" />
              <div class="min-w-0 flex-1">
                <div class="text-[13px] text-t1 font-medium truncate">{{ f.filename }}</div>
                <div v-if="f.path" class="text-[11px] text-t3 truncate mt-0.5">{{ f.path }}</div>
                <div class="flex items-center gap-2 mt-1.5 text-[11px] text-t3">
                  <span>{{ t('search.hit_count', { n: f.chunks.length }) }}</span>
                  <span class="ml-auto tabular-nums">{{ f.topScore.toFixed(3) }}</span>
                </div>
              </div>
            </div>
          </li>
        </ul>
        <div v-if="stats" class="px-4 py-3 text-[11px] text-t3 border-t border-line/60">
          {{ t('search.footer_v3', {
            files: files.length,
            chunks: stats.chunk_hits ?? chunks.length,
            ms: stats.elapsed_ms ?? 0,
          }) }}
        </div>
      </aside>

      <!-- Right: passages of the selected file -->
      <section class="flex-1 min-w-0 overflow-y-auto">
        <div v-if="!selectedFile" class="h-full flex items-center justify-center text-t3 text-[13px]">
          {{ t('search.pick_file') }}
        </div>
        <div v-else class="px-8 py-6 max-w-[880px]">
          <!-- File header -->
          <div class="flex items-start gap-3 pb-4 mb-4 border-b border-line">
            <FileText :size="16" :stroke-width="1.75" class="text-t3 shrink-0 mt-0.5" />
            <div class="min-w-0 flex-1">
              <div class="text-[15px] text-t1 font-semibold">{{ selectedFile.filename }}</div>
              <div v-if="selectedFile.path" class="text-[12px] text-t3 mt-0.5">{{ selectedFile.path }}</div>
            </div>
            <button
              class="shrink-0 inline-flex items-center gap-1.5 px-2.5 h-7 text-[11px] text-t2 hover:text-t1 hover:bg-bg3 rounded transition-colors"
              :title="t('search.open_doc')"
              @click="openDoc(selectedFile.doc_id, null)"
            >
              <ExternalLink :size="12" :stroke-width="1.75" />
              {{ t('search.open_doc') }}
            </button>
          </div>

          <!-- Passages list -->
          <ul class="list-none p-0 m-0 flex flex-col gap-2">
            <li
              v-for="c in selectedFile.chunks"
              :key="c.chunk_id"
              class="px-3.5 py-3 bg-bg border border-line rounded-md cursor-pointer hover:border-t3 hover:bg-bg2 transition-colors"
              @click="openDoc(c.doc_id, c.chunk_id)"
            >
              <div class="flex items-center gap-2 mb-1.5 text-[11px] text-t3">
                <span v-if="c.page_no" class="px-1.5 py-px bg-bg2 rounded">{{ t('search.page', { n: c.page_no }) }}</span>
                <span class="ml-auto shrink-0 tabular-nums">{{ c.score?.toFixed(3) }}</span>
              </div>
              <div class="text-[13px] text-t1 leading-relaxed">{{ c.snippet }}</div>
            </li>
          </ul>
        </div>
      </section>
    </main>
  </div>
</template>
