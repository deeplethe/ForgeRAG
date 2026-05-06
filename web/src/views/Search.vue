<!--
  Search view — BM25 keyword search with cross-lingual query
  expansion, in a two-pane "file → passages" layout.

  Calls POST /api/v1/search which:
    1. Detects the query's language and asks a small LLM to
       translate it into the project's other supported languages
       (LRU-cached, thinking-disabled).
    2. Sends original + translations into BM25 as a single
       expanded query string. Matched tokens come back per chunk
       so we can highlight them in the snippets.
    3. Returns both a flat chunks list AND a file rollup so the
       UI can drive its master/detail layout off the server's
       authoritative grouping (no client-side dedup needed).

  Layout:
    * LEFT pane — one row per matching file, ranked by the file
      rollup's score. Each row shows filename, folder, format,
      and a one-line preview from the best chunk (highlighted).
    * RIGHT pane — all matching passages of the selected file,
      in document order. Click a passage to open DocDetail
      scrolled to that chunk.

  Why server-rolled files over client grouping: the rollup score
  reflects "how relevant is this file overall" (filename match
  bonus, multi-chunk score combination) which a client-side
  ``max(chunk.score)`` would lose. We rely on the rollup for
  ordering and on the flat chunks list (filtered by doc_id) for
  the right-pane detail view.

  Module-level refs persist last query + results across nav.
-->
<script>
import { ref } from 'vue'
export default { name: 'SearchView' }
const _query = ref('')
const _results = ref(null)   // { chunks, files, stats } | null
const _loading = ref(false)
const _error = ref('')
const _selectedDocId = ref(null)
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
      include: ['chunks', 'files'],
      limit: { chunks: 60, files: 20 },
    })
    _results.value = res
    // Auto-select the first (highest-ranked) file so the right
    // pane has content to show.
    _selectedDocId.value = res.files?.[0]?.doc_id || null
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
const files = computed(() => _results.value?.files || [])
const stats = computed(() => _results.value?.stats || null)

// Translations chip — shows what the query got expanded to so
// users see WHY their Chinese query just turned up English docs.
// Only renders when the translator actually produced something
// new (server returns null otherwise).
const translations = computed(() => {
  const arr = stats.value?.translations
  if (!Array.isArray(arr) || arr.length <= 1) return null
  // [original, ...translated]
  return arr.slice(1)
})

const selectedFile = computed(() =>
  files.value.find((f) => f.doc_id === _selectedDocId.value) || null,
)

// Chunks for the right pane — filter the flat list by selected
// doc, then sort by page asc, score desc. Falls back to the
// rollup's best_chunk when the flat list happens to be empty
// (e.g. tight per-file limits on the backend).
const selectedChunks = computed(() => {
  if (!selectedFile.value) return []
  const sel = chunks.value.filter((c) => c.doc_id === selectedFile.value.doc_id)
  if (!sel.length && selectedFile.value.best_chunk) {
    return [{
      ...selectedFile.value.best_chunk,
      doc_id: selectedFile.value.doc_id,
      filename: selectedFile.value.filename,
      path: selectedFile.value.path,
    }]
  }
  return sel.sort((a, b) => {
    const pa = a.page_no || 0
    const pb = b.page_no || 0
    if (pa !== pb) return pa - pb
    return (b.score || 0) - (a.score || 0)
  })
})

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

// ── Highlight helpers ─────────────────────────────────────────
// Wrap occurrences of any matched token in <mark>. Tokens come
// from the BM25 backend pre-lowercased; we use case-insensitive
// regex so the displayed-case version still highlights.
function highlightTokens(text, tokens) {
  if (!text) return ''
  if (!Array.isArray(tokens) || tokens.length === 0) return escapeHtml(text)
  const escaped = tokens.map(escapeRegExp).filter(Boolean)
  if (escaped.length === 0) return escapeHtml(text)
  const re = new RegExp(`(${escaped.join('|')})`, 'gi')
  return escapeHtml(text).replace(re, '<mark>$1</mark>')
}

function escapeHtml(s) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
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

      <!-- Translation expansion chip — shows what the LLM rewrote
           the query to. Hides itself silently when the translator
           is disabled or returned only the original. -->
      <div v-if="translations" class="mt-3 flex items-center gap-2 max-w-[720px] flex-wrap text-[11px] text-t3">
        <span>{{ t('search.expanded_label') }}</span>
        <span
          v-for="(tx, i) in translations"
          :key="i"
          class="px-1.5 py-0.5 bg-bg2 border border-line rounded text-t2"
        >{{ tx }}</span>
      </div>
    </header>

    <!-- ── Empty / error / loading states (full-width) ────────── -->
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
      <aside class="w-[360px] shrink-0 border-r border-line overflow-y-auto bg-bg2/40">
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
                <div
                  class="text-[13px] text-t1 font-medium truncate hl"
                  v-html="highlightTokens(f.filename, f.filename_tokens)"
                />
                <div v-if="f.path" class="text-[11px] text-t3 truncate mt-0.5">{{ f.path }}</div>
                <!-- One-line snippet preview from the best chunk —
                     gives the user a feel for WHY this file matched
                     before they click into it. -->
                <div
                  v-if="f.best_chunk"
                  class="text-[12px] text-t2 mt-1 line-clamp-2 leading-snug hl"
                  v-html="highlightTokens(f.best_chunk.snippet, f.best_chunk.matched_tokens)"
                />
                <div class="flex items-center gap-2 mt-1.5 text-[11px] text-t3">
                  <span v-if="f.format" class="px-1 bg-bg2 rounded">{{ f.format.toUpperCase() }}</span>
                  <span class="ml-auto tabular-nums">{{ f.score.toFixed(3) }}</span>
                </div>
              </div>
            </div>
          </li>
        </ul>
        <div v-if="stats" class="px-4 py-3 text-[11px] text-t3 border-t border-line/60">
          {{ t('search.footer_v3', {
            files: stats.file_hits ?? files.length,
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
              <div
                class="text-[15px] text-t1 font-semibold hl"
                v-html="highlightTokens(selectedFile.filename, selectedFile.filename_tokens)"
              />
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
              v-for="c in selectedChunks"
              :key="c.chunk_id"
              class="px-3.5 py-3 bg-bg border border-line rounded-md cursor-pointer hover:border-t3 hover:bg-bg2 transition-colors"
              @click="openDoc(c.doc_id || selectedFile.doc_id, c.chunk_id)"
            >
              <div class="flex items-center gap-2 mb-1.5 text-[11px] text-t3">
                <span v-if="c.page_no" class="px-1.5 py-px bg-bg2 rounded">{{ t('search.page', { n: c.page_no }) }}</span>
                <span class="ml-auto shrink-0 tabular-nums">{{ c.score?.toFixed(3) }}</span>
              </div>
              <div
                class="text-[13px] text-t1 leading-relaxed hl"
                v-html="highlightTokens(c.snippet, c.matched_tokens)"
              />
            </li>
          </ul>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
/* Highlight style — soft amber wash, doesn't fight the body text. */
.hl :deep(mark) {
  background: rgba(251, 191, 36, 0.25);
  color: inherit;
  padding: 0 1px;
  border-radius: 2px;
}
</style>
