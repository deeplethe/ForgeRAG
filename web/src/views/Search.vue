<!--
  Search view — workspace-level "find me things" page.

  Calls POST /api/v1/search (the retrieval primitive, no LLM answer)
  with include=["chunks","files"] so users see both:

    * file-level rollups at the top  (which document matches?)
    * chunk-level snippets below      (what's the actual passage?)

  Distinct from the Chat view (which calls /query and gets a streamed
  LLM answer + citations). This page is for navigation / discovery —
  click a chunk row to jump straight into the source doc.

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
  if (matched.length === 2) return 'badge-both'
  return matched[0] === 'filename' ? 'badge-filename' : 'badge-content'
}

// Bold the parts of a filename that matched the query — uses the
// per-row ``filename_tokens`` array returned by the backend so we
// don't have to re-tokenize client-side.
function highlightFilename(filename, tokens) {
  if (!filename) return ''
  if (!Array.isArray(tokens) || tokens.length === 0) return escapeHtml(filename)
  // Build a single regex from the token list. Tokens are already
  // lowercased server-side; case-insensitive flag handles the
  // displayed casing.
  const escaped = tokens.map(escapeRegExp).filter(Boolean)
  if (escaped.length === 0) return escapeHtml(filename)
  const re = new RegExp(`(${escaped.join('|')})`, 'gi')
  return escapeHtml(filename).replace(re, '<mark>$1</mark>')
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
  <div class="search-page">
    <!-- ── Search bar ────────────────────────────────────────────── -->
    <header class="search-header">
      <h1 class="search-title">{{ t('search.title') }}</h1>
      <p class="search-subtitle">{{ t('search.subtitle') }}</p>
      <form class="search-form" @submit.prevent="runSearch">
        <div class="search-input-wrap">
          <Search :size="18" class="search-input-icon" />
          <input
            ref="inputEl"
            v-model="_query"
            type="text"
            class="search-input"
            :placeholder="t('search.placeholder')"
            :disabled="_loading"
            autocomplete="off"
            spellcheck="false"
          />
          <button
            v-if="_query"
            type="button"
            class="search-clear"
            @click="clearAll"
            :title="t('search.clear')"
          >×</button>
        </div>
        <button type="submit" class="search-submit" :disabled="_loading || !_query.trim()">
          <Loader2 v-if="_loading" :size="16" class="animate-spin" />
          <span v-else>{{ t('search.submit') }}</span>
        </button>
      </form>
    </header>

    <!-- ── Body ──────────────────────────────────────────────────── -->
    <main class="search-body">
      <div v-if="_error" class="search-error">
        <AlertCircle :size="16" />
        <span>{{ _error }}</span>
      </div>

      <div v-else-if="_loading && !_results" class="search-empty">
        {{ t('search.empty.searching') }}
      </div>

      <div v-else-if="!_results" class="search-empty">
        <FileSearch :size="36" class="search-empty-icon" />
        <p>{{ t('search.empty.idle') }}</p>
        <p class="search-hint">{{ t('search.empty.hint') }}</p>
      </div>

      <div v-else-if="!hasResults" class="search-empty">
        <FileSearch :size="36" class="search-empty-icon" />
        <p>{{ t('search.empty.none', { query: _query }) }}</p>
      </div>

      <div v-else class="search-results">
        <!-- ── Files section ───────────────────────────────────── -->
        <section v-if="files.length" class="result-section">
          <div class="result-section-hdr">
            <FileText :size="14" />
            <span class="result-section-title">{{ t('search.section.files') }}</span>
            <span class="result-section-count">{{ files.length }}</span>
          </div>
          <ul class="result-list">
            <li
              v-for="f in files"
              :key="f.doc_id"
              class="file-row"
              @click="openDoc(f.doc_id, f.best_chunk?.chunk_id)"
            >
              <div class="file-row-head">
                <span class="file-name" v-html="highlightFilename(f.filename, f.filename_tokens)" />
                <span class="file-badge" :class="badgeClass(f.matched_in)">
                  {{ fmtBadge(f.matched_in) }}
                </span>
              </div>
              <div class="file-row-meta">
                <span class="file-path">{{ f.path || '/' }}</span>
                <span v-if="f.format" class="file-format">{{ f.format.toUpperCase() }}</span>
              </div>
              <div v-if="f.best_chunk" class="file-row-snippet">
                {{ f.best_chunk.snippet }}
              </div>
            </li>
          </ul>
        </section>

        <!-- ── Chunks section ──────────────────────────────────── -->
        <section v-if="chunks.length" class="result-section">
          <div class="result-section-hdr">
            <FileSearch :size="14" />
            <span class="result-section-title">{{ t('search.section.chunks') }}</span>
            <span class="result-section-count">{{ chunks.length }}</span>
          </div>
          <ul class="result-list">
            <li
              v-for="c in chunks"
              :key="c.chunk_id"
              class="chunk-row"
              @click="openDoc(c.doc_id, c.chunk_id)"
            >
              <div class="chunk-row-head">
                <span class="chunk-filename">{{ c.filename || c.doc_id }}</span>
                <span v-if="c.boosted_by_filename" class="chunk-boost"
                      :title="t('search.tooltip.boosted')">↑</span>
                <span v-if="c.page_no" class="chunk-page">p.{{ c.page_no }}</span>
              </div>
              <div class="chunk-row-snippet">{{ c.snippet }}</div>
            </li>
          </ul>
        </section>

        <!-- ── Footer stats ────────────────────────────────────── -->
        <div v-if="stats" class="search-footer">
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
.search-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--color-bg);
  overflow: hidden;
}

/* ── Header ─────────────────────────────────────────────────── */
.search-header {
  flex: 0 0 auto;
  padding: 32px 32px 20px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg);
}

.search-title {
  font-size: 22px;
  font-weight: 600;
  color: var(--color-t1);
  margin: 0;
}

.search-subtitle {
  margin: 6px 0 18px;
  font-size: 13px;
  color: var(--color-t3);
}

.search-form {
  display: flex;
  gap: 8px;
  align-items: center;
  max-width: 720px;
}

.search-input-wrap {
  position: relative;
  flex: 1 1 auto;
}

.search-input-icon {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--color-t3);
  pointer-events: none;
}

.search-input {
  width: 100%;
  padding: 10px 36px 10px 38px;
  font-size: 14px;
  color: var(--color-t1);
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
  border-radius: 6px;
  outline: none;
  transition: border-color 0.1s;
}

.search-input:focus {
  border-color: var(--color-t2);
}

.search-input:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.search-clear {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  width: 20px;
  height: 20px;
  border: none;
  background: transparent;
  color: var(--color-t3);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  border-radius: 4px;
}

.search-clear:hover {
  color: var(--color-t1);
  background: var(--color-bg3);
}

.search-submit {
  flex: 0 0 auto;
  min-width: 84px;
  padding: 10px 16px;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-bg);
  background: var(--color-t1);
  border: none;
  border-radius: 6px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: opacity 0.1s;
}

.search-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.search-submit:hover:not(:disabled) {
  opacity: 0.85;
}

/* ── Body ───────────────────────────────────────────────────── */
.search-body {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 20px 32px 40px;
}

.search-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  font-size: 13px;
  color: #c33;
  background: rgba(204, 51, 51, 0.08);
  border: 1px solid rgba(204, 51, 51, 0.2);
  border-radius: 6px;
  max-width: 720px;
}

.search-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 60vh;
  color: var(--color-t3);
  text-align: center;
  font-size: 13px;
}

.search-empty-icon {
  color: var(--color-t3);
  opacity: 0.4;
  margin-bottom: 12px;
}

.search-hint {
  font-size: 12px;
  margin-top: 6px;
  opacity: 0.7;
}

/* ── Results ────────────────────────────────────────────────── */
.search-results {
  max-width: 880px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.result-section-hdr {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0 10px;
  border-bottom: 1px solid var(--color-line);
  margin-bottom: 8px;
  color: var(--color-t2);
}

.result-section-title {
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-weight: 600;
}

.result-section-count {
  font-size: 11px;
  color: var(--color-t3);
  font-family: var(--font-mono, ui-monospace, monospace);
}

.result-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* File row */
.file-row,
.chunk-row {
  padding: 12px 14px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 6px;
  cursor: pointer;
  transition: border-color 0.1s, background 0.1s;
}

.file-row:hover,
.chunk-row:hover {
  border-color: var(--color-t3);
  background: var(--color-bg2);
}

.file-row-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.file-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-t1);
  flex: 1 1 auto;
  word-break: break-all;
}

.file-name :deep(mark) {
  background: rgba(251, 191, 36, 0.25);
  color: inherit;
  padding: 0 1px;
  border-radius: 2px;
}

.file-badge {
  font-size: 10px;
  font-family: var(--font-mono, ui-monospace, monospace);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 2px 6px;
  border-radius: 3px;
  flex: 0 0 auto;
}

.badge-filename { background: rgba(50, 145, 255, 0.12); color: #3291ff; }
.badge-content  { background: rgba(16, 185, 129, 0.12); color: #10b981; }
.badge-both     { background: rgba(251, 191, 36, 0.12); color: #fbbf24; }

.file-row-meta {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 4px;
  font-size: 11px;
  color: var(--color-t3);
  font-family: var(--font-mono, ui-monospace, monospace);
}

.file-format {
  padding: 1px 5px;
  background: var(--color-bg2);
  border-radius: 2px;
  font-size: 10px;
}

.file-row-snippet {
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-t2);
  line-height: 1.5;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

/* Chunk row */
.chunk-row-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--color-t3);
  font-family: var(--font-mono, ui-monospace, monospace);
}

.chunk-filename {
  color: var(--color-t2);
  font-size: 12px;
  font-family: inherit;
}

.chunk-page {
  color: var(--color-t3);
}

.chunk-boost {
  color: #3291ff;
  font-weight: bold;
  cursor: help;
}

.chunk-row-snippet {
  font-size: 13px;
  color: var(--color-t1);
  line-height: 1.55;
}

.search-footer {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--color-line);
  font-size: 11px;
  color: var(--color-t3);
  font-family: var(--font-mono, ui-monospace, monospace);
}
</style>
