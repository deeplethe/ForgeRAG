<!--
  Search view — workspace-level "find me things" page.

  Calls POST /api/v1/search (BM25-only keyword search, no LLM) with
  include=["chunks","files"] so users see both:

    * file-level rollups at the top  (which document matches?)
    * chunk-level snippets below      (what's the actual passage?)

  Distinct from the Chat view (which calls /query and gets a streamed
  LLM answer + citations). This page is for navigation / discovery —
  click a chunk row to jump straight into the source doc.

  Styling matches Chat.vue: Tailwind utilities + design-token colors
  (--color-bg, --color-line, --color-t1/t2/t3, --color-brand). No
  mono fonts — search results are prose, not code.

  Keeps state at module level so navigating away and back preserves
  the user's last query + results. Same idiom as Chat.vue.
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
import { Search, FileText, FileSearch, AlertCircle, Loader2 } from 'lucide-vue-next'

import { search as searchApi } from '@/api'

const { t } = useI18n()
const router = useRouter()

const inputEl = setupRef(null)

// Auto-focus the search input on mount so the page is keyboard-first.
onMounted(() => {
  if (inputEl.value) inputEl.value.focus()
})

// ── Submit handler ────────────────────────────────────────────────
async function runSearch() {
  const q = _query.value.trim()
  if (!q) return
  _loading.value = true
  _error.value = ''
  try {
    const res = await searchApi({
      query: q,
      include: ['chunks', 'files'],
      limit: { chunks: 30, files: 10 },
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

// ── Result navigation ─────────────────────────────────────────────
// Clicking a file or chunk row opens the doc in the workspace's
// embedded DocDetail (same URL convention the workspace uses).
// Chunks pass an additional &chunk=<id> hint so DocDetail can scroll
// the right pane straight to the matched chunk on mount.
function openDoc(docId, chunkId) {
  const q = { doc: docId }
  if (chunkId) q.chunk = chunkId
  router.push({ path: '/workspace', query: q })
}

// ── Computed views over the loaded result ─────────────────────────
const files = computed(() => _results.value?.files || [])
const chunks = computed(() => _results.value?.chunks || [])
const stats = computed(() => _results.value?.stats || null)

const hasResults = computed(() => files.value.length > 0 || chunks.value.length > 0)

function fmtBadge(matched) {
  // matched = ["filename"] | ["content"] | ["filename", "content"]
  if (!Array.isArray(matched) || matched.length === 0) return ''
  if (matched.length === 2) return t('search.badge.both')
  return matched[0] === 'filename' ? t('search.badge.filename') : t('search.badge.content')
}

function badgeClass(matched) {
  if (!Array.isArray(matched)) return ''
  if (matched.length === 2) return 'bg-amber-500/10 text-amber-600'
  if (matched[0] === 'filename') return 'bg-brand-bg text-brand'
  return 'bg-emerald-500/10 text-emerald-600'
}

// ── Highlight ─────────────────────────────────────────────────────
// Wraps occurrences of any matched token in <mark> tags. Used for
// both filenames (with f.filename_tokens) and chunk snippets (with
// c.matched_tokens). Tokens are server-tokenised + lowercased so the
// case-insensitive flag handles whatever casing the displayed text has.
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
        <!-- Input wrapper: matches Chat.vue's textarea wrapper for visual consistency. -->
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

      <div v-else class="max-w-[880px] flex flex-col gap-6">
        <!-- ── Files section ─────────────────────────────────────── -->
        <section v-if="files.length">
          <div class="flex items-center gap-2 pb-2.5 mb-2 border-b border-line text-t2">
            <FileText :size="14" />
            <span class="text-[12px] font-semibold uppercase tracking-wider">
              {{ t('search.section.files') }}
            </span>
            <span class="text-[12px] text-t3">{{ files.length }}</span>
          </div>
          <ul class="list-none p-0 m-0 flex flex-col gap-2">
            <li
              v-for="f in files"
              :key="f.doc_id"
              class="px-3.5 py-3 bg-bg border border-line rounded-md cursor-pointer hover:border-t3 hover:bg-bg2 transition-colors"
              @click="openDoc(f.doc_id, f.best_chunk?.chunk_id)"
            >
              <div class="flex items-center gap-2.5">
                <span
                  class="text-sm font-medium text-t1 flex-1 break-all hl"
                  v-html="highlightTokens(f.filename, f.filename_tokens)"
                />
                <span class="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded shrink-0" :class="badgeClass(f.matched_in)">
                  {{ fmtBadge(f.matched_in) }}
                </span>
              </div>
              <div class="flex items-center gap-3 mt-1 text-[12px] text-t3">
                <span>{{ f.path || '/' }}</span>
                <span v-if="f.format" class="px-1.5 py-px bg-bg2 rounded text-[10px]">{{ f.format.toUpperCase() }}</span>
              </div>
              <div
                v-if="f.best_chunk"
                class="mt-2 text-[13px] text-t2 leading-relaxed line-clamp-2 hl"
                v-html="highlightTokens(f.best_chunk.snippet, f.best_chunk.matched_tokens)"
              />
            </li>
          </ul>
        </section>

        <!-- ── Chunks section ────────────────────────────────────── -->
        <section v-if="chunks.length">
          <div class="flex items-center gap-2 pb-2.5 mb-2 border-b border-line text-t2">
            <FileSearch :size="14" />
            <span class="text-[12px] font-semibold uppercase tracking-wider">
              {{ t('search.section.chunks') }}
            </span>
            <span class="text-[12px] text-t3">{{ chunks.length }}</span>
          </div>
          <ul class="list-none p-0 m-0 flex flex-col gap-2">
            <li
              v-for="c in chunks"
              :key="c.chunk_id"
              class="px-3.5 py-3 bg-bg border border-line rounded-md cursor-pointer hover:border-t3 hover:bg-bg2 transition-colors"
              @click="openDoc(c.doc_id, c.chunk_id)"
            >
              <div class="flex items-center gap-2 mb-1.5 text-[12px] text-t3">
                <span class="text-t2">{{ c.filename || c.doc_id }}</span>
                <span v-if="c.boosted_by_filename" class="text-brand font-bold cursor-help" :title="t('search.tooltip.boosted')">↑</span>
                <span v-if="c.page_no">p.{{ c.page_no }}</span>
              </div>
              <div
                class="text-[13px] text-t1 leading-relaxed hl"
                v-html="highlightTokens(c.snippet, c.matched_tokens)"
              />
            </li>
          </ul>
        </section>

        <!-- ── Footer stats ──────────────────────────────────────── -->
        <div v-if="stats" class="mt-4 pt-3 border-t border-line text-[12px] text-t3">
          {{ t('search.footer', {
            chunks: stats.chunk_hits ?? 0,
            files: stats.file_hits ?? 0,
            ms: stats.elapsed_ms ?? 0,
          }) }}
        </div>
      </div>
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
