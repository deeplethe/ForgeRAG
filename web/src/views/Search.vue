<!--
  Search view — BM25 keyword search with cross-lingual query
  expansion, single-column file-as-unit results.

  Calls POST /api/v1/search which:
    1. Detects the query's language and asks a small LLM to
       translate it into the project's other supported languages
       (LRU-cached, thinking-disabled).
    2. Sends original + translations into BM25 as one expanded
       query.
    3. Returns ranked passages + a server-side file rollup, both
       carrying matched_tokens for keyword highlighting.

  Layout: ONE ranked list, one row per matching file. Each row
  shows:
    * filename, with tokens that matched the FILENAME index
      highlighted (filename match is its own kind of hit, on
      par with content match — see f.matched_in).
    * folder path + format chip
    * a match-badge: filename / content / both — so users see
      at a glance whether the file matched on its name vs its
      contents.
    * a one-line snippet from the file's best chunk, with the
      content match tokens highlighted.
    * the file's overall score (rolls up filename + content
      signals server-side; we trust it for ordering rather
      than reconstructing client-side).

  Click a row → workspace DocDetail with the best chunk pre-
  selected so the right pane scrolls to the matched passage.
-->
<script>
import { ref } from 'vue'
export default { name: 'SearchView' }
const _query = ref('')
const _results = ref(null)   // { chunks, files, stats } | null
const _loading = ref(false)
const _error = ref('')
</script>

<script setup>
import { computed, onMounted, ref as setupRef } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Search, FileSearch, AlertCircle, Loader2, FileText } from 'lucide-vue-next'

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
      limit: { chunks: 30, files: 20 },
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

// Click a file row → workspace DocDetail. Pass the best chunk's
// id so the right-pane preview scrolls to the matched passage
// instead of the doc's first page.
function openFile(f) {
  const q = { doc: f.doc_id }
  if (f.best_chunk?.chunk_id) q.chunk = f.best_chunk.chunk_id
  router.push({ path: '/workspace', query: q })
}

const files = computed(() => _results.value?.files || [])
const stats = computed(() => _results.value?.stats || null)
const hasResults = computed(() => files.value.length > 0)

// Translation expansion chip — surfaces the LLM rewrite
// (e.g. "蜜蜂" → "bees") so users see WHY their Chinese query
// just turned up English files. Hidden when no expansion ran.
const translations = computed(() => {
  const arr = stats.value?.translations
  if (!Array.isArray(arr) || arr.length <= 1) return null
  return arr.slice(1)
})

// ── Match badge ────────────────────────────────────────────────
// matched_in is one of:
//   ["filename"]          — file matched on its name only
//   ["content"]           — file matched on its passages only
//   ["filename","content"] — both (best signal; usually highest scored)
function matchBadge(matched) {
  if (!Array.isArray(matched) || matched.length === 0) return ''
  if (matched.length >= 2) return t('search.badge.both')
  return matched[0] === 'filename' ? t('search.badge.filename') : t('search.badge.content')
}

function badgeClass(matched) {
  if (!Array.isArray(matched)) return ''
  if (matched.length >= 2) return 'badge-both'
  return matched[0] === 'filename' ? 'badge-filename' : 'badge-content'
}

// ── Highlight ─────────────────────────────────────────────────
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

      <!-- Translation expansion chip — only when the LLM rewrote
           the query. Lets the user see why a Chinese query
           surfaced English files. -->
      <div v-if="translations" class="mt-3 flex items-center gap-2 max-w-[720px] flex-wrap text-[11px] text-t3">
        <span>{{ t('search.expanded_label') }}</span>
        <span
          v-for="(tx, i) in translations"
          :key="i"
          class="px-1.5 py-0.5 bg-bg2 border border-line rounded text-t2"
        >{{ tx }}</span>
      </div>
    </header>

    <!-- ── Body ─────────────────────────────────────────────────── -->
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
            v-for="f in files"
            :key="f.doc_id"
            class="px-4 py-3 bg-bg border border-line rounded-md cursor-pointer hover:border-t3 hover:bg-bg2 transition-colors"
            @click="openFile(f)"
          >
            <!-- Row 1: filename + match badge + score -->
            <div class="flex items-center gap-2 min-w-0">
              <FileText :size="14" :stroke-width="1.75" class="text-t3 shrink-0" />
              <span
                class="text-[14px] text-t1 font-medium flex-1 truncate hl"
                v-html="highlightTokens(f.filename, f.filename_tokens)"
              />
              <span class="badge shrink-0" :class="badgeClass(f.matched_in)">{{ matchBadge(f.matched_in) }}</span>
              <span class="ml-1 text-[11px] text-t3 shrink-0 tabular-nums">{{ f.score?.toFixed(3) }}</span>
            </div>

            <!-- Row 2: folder path + format -->
            <div v-if="f.path || f.format" class="flex items-center gap-2 mt-1 ml-6 text-[12px] text-t3">
              <span v-if="f.path" class="truncate">{{ f.path }}</span>
              <span v-if="f.format" class="px-1.5 py-px bg-bg2 rounded text-[10px]">{{ f.format.toUpperCase() }}</span>
            </div>

            <!-- Row 3: best chunk snippet, highlighted on content tokens -->
            <div
              v-if="f.best_chunk"
              class="mt-2 ml-6 text-[13px] text-t2 leading-relaxed line-clamp-2 hl"
              v-html="highlightTokens(f.best_chunk.snippet, f.best_chunk.matched_tokens)"
            />
          </li>
        </ul>

        <div v-if="stats" class="mt-3 pt-3 border-t border-line text-[12px] text-t3">
          {{ t('search.footer_v3', {
            files: stats.file_hits ?? files.length,
            chunks: stats.chunk_hits ?? 0,
            ms: stats.elapsed_ms ?? 0,
          }) }}
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
/* Highlight style — soft amber wash that doesn't fight body text. */
.hl :deep(mark) {
  background: rgba(251, 191, 36, 0.25);
  color: inherit;
  padding: 0 1px;
  border-radius: 2px;
}

/* Match badge — three states:
     filename: brand colour (the file's name itself matched)
     content: emerald (a passage matched)
     both: amber (filename AND content — strongest hit)
   Same colour language as the highlight wash. */
.badge {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 6px;
  border-radius: 3px;
  white-space: nowrap;
}
.badge-filename {
  background: var(--color-brand-bg, rgba(99, 102, 241, 0.12));
  color: var(--color-brand, #6366f1);
}
.badge-content {
  background: rgba(16, 185, 129, 0.12);
  color: rgb(5, 150, 105);
}
.badge-both {
  background: rgba(251, 191, 36, 0.18);
  color: rgb(180, 83, 9);
}
</style>
