<!--
  Search view — BM25 keyword search with cross-lingual query
  expansion, Onyx-style flat results list.

  Calls POST /api/v1/search:
    1. Detect query language; small LLM translates to the other
       supported language(s) (LRU-cached, thinking-disabled).
    2. Send original + translations into BM25 as one expanded
       query.
    3. Return ranked passages + a server-rolled file view, both
       with matched_tokens for keyword highlighting and
       per-file metadata (created/updated, uploader).

  Layout:
    * Filter bar — three pill-shaped dropdowns (time / uploader
      / format), values sourced from the result set.
    * Flat list — one row per matching file. No card border;
      Onyx-style hover wash + bottom hairline does the
      separation. Each row carries:
        - icon + filename (highlighted on filename_tokens)
        - meta line: avatar + uploader · updated time · format
        - one-line snippet from best chunk (highlighted on
          matched_tokens)
        - match badge (filename / content / both) on the right

  Filtering is client-side: the backend returns the unfiltered
  result set, the dropdowns derive their available options from
  it, and selection just narrows what's shown. Keeps the API
  surface small; if the result set ever exceeds a few hundred
  files we'll switch to server-side filtering.
-->
<script>
import { ref } from 'vue'
export default { name: 'SearchView' }
const _query = ref('')
const _results = ref(null)
const _loading = ref(false)
const _error = ref('')
// Filter state — preserved across nav so coming back to /search
// reuses the same selection alongside the cached results.
const _filterTime = ref('all')      // all | 7d | 30d | 1y
const _filterUploader = ref('all')  // 'all' | uploader_user_id (or 'unknown' for null uploader)
const _filterFormat = ref('all')    // 'all' | format string ('pdf', 'md', ...)
</script>

<script setup>
import { computed, onMounted, ref as setupRef } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Search, AlertCircle, FileText, Clock, User, FileType2, ChevronDown } from 'lucide-vue-next'

import { search as searchApi } from '@/api'
import { avatarUrlFor } from '@/api/admin'
import UserAvatar from '@/components/UserAvatar.vue'
import Spinner from '@/components/Spinner.vue'

const { t } = useI18n()
const router = useRouter()

const inputEl = setupRef(null)

// Per-popover toggles. Only one open at a time — opening one
// closes the others. Click-outside (handler below) closes all.
const openFilter = setupRef(null) // null | 'time' | 'uploader' | 'format'

onMounted(() => {
  if (inputEl.value) inputEl.value.focus()
  document.addEventListener('click', _onDocClick)
})

function _onDocClick(e) {
  if (!e.target.closest('.filter-pill')) openFilter.value = null
}

async function runSearch() {
  const q = _query.value.trim()
  if (!q) return
  _loading.value = true
  _error.value = ''
  try {
    const res = await searchApi({
      query: q,
      include: ['chunks', 'files'],
      limit: { chunks: 30, files: 30 },
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
  _filterTime.value = 'all'
  _filterUploader.value = 'all'
  _filterFormat.value = 'all'
  if (inputEl.value) inputEl.value.focus()
}

function openFile(f) {
  const q = { doc: f.doc_id }
  if (f.best_chunk?.chunk_id) q.chunk = f.best_chunk.chunk_id
  router.push({ path: '/workspace', query: q })
}

const allFiles = computed(() => _results.value?.files || [])
const stats = computed(() => _results.value?.stats || null)

const translations = computed(() => {
  const arr = stats.value?.translations
  if (!Array.isArray(arr) || arr.length <= 1) return null
  return arr.slice(1)
})

// ── Filter option lists ─────────────────────────────────────
// Derived from the unfiltered result set so dropdowns only
// surface values that actually appear. Sorted alphabetically
// (uploaders) / by frequency (formats) for predictability.
const formatOptions = computed(() => {
  const counts = new Map()
  for (const f of allFiles.value) {
    const k = (f.format || '').toLowerCase()
    if (!k) continue
    counts.set(k, (counts.get(k) || 0) + 1)
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([k]) => k)
})

const uploaderOptions = computed(() => {
  // Map uploader_user_id → display name. ``unknown`` bucket for
  // legacy / deleted-user docs (uploader_user_id is null).
  const seen = new Map()
  for (const f of allFiles.value) {
    if (f.uploader_user_id) {
      seen.set(f.uploader_user_id, f.uploader_display_name || f.uploader_user_id)
    } else if (!seen.has('unknown')) {
      seen.set('unknown', t('search.uploader_unknown'))
    }
  }
  return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]))
})

// ── Filter predicate ────────────────────────────────────────
const _NOW = Date.now()
function _withinTimeWindow(f) {
  if (_filterTime.value === 'all') return true
  const dStr = f.updated_at || f.created_at
  if (!dStr) return false
  const d = new Date(dStr).getTime()
  if (isNaN(d)) return false
  const days = ({ '7d': 7, '30d': 30, '1y': 365 })[_filterTime.value]
  return _NOW - d <= days * 86400000
}

function _matchesUploader(f) {
  if (_filterUploader.value === 'all') return true
  if (_filterUploader.value === 'unknown') return !f.uploader_user_id
  return f.uploader_user_id === _filterUploader.value
}

function _matchesFormat(f) {
  if (_filterFormat.value === 'all') return true
  return (f.format || '').toLowerCase() === _filterFormat.value
}

const files = computed(() =>
  allFiles.value.filter((f) =>
    _withinTimeWindow(f) && _matchesUploader(f) && _matchesFormat(f),
  ),
)

const hasResults = computed(() => allFiles.value.length > 0)
const hasFiltered = computed(() => files.value.length > 0)
const filtersActive = computed(() =>
  _filterTime.value !== 'all'
  || _filterUploader.value !== 'all'
  || _filterFormat.value !== 'all',
)

// ── Filter pill labels ──────────────────────────────────────
const timeLabel = computed(() => ({
  all: t('search.filter.time_all'),
  '7d': t('search.filter.time_7d'),
  '30d': t('search.filter.time_30d'),
  '1y': t('search.filter.time_1y'),
})[_filterTime.value])

const uploaderLabel = computed(() => {
  if (_filterUploader.value === 'all') return t('search.filter.uploader_all')
  const opt = uploaderOptions.value.find(([k]) => k === _filterUploader.value)
  return opt ? opt[1] : t('search.filter.uploader_all')
})

const formatLabel = computed(() => {
  if (_filterFormat.value === 'all') return t('search.filter.format_all')
  return _filterFormat.value.toUpperCase()
})

function pickTime(v) { _filterTime.value = v; openFilter.value = null }
function pickUploader(v) { _filterUploader.value = v; openFilter.value = null }
function pickFormat(v) { _filterFormat.value = v; openFilter.value = null }

// ── Match badge ────────────────────────────────────────────
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

// ── Highlight + relative time ──────────────────────────────
function highlightTokens(text, tokens) {
  if (!text) return ''
  if (!Array.isArray(tokens) || tokens.length === 0) return escapeHtml(text)
  const escaped = tokens.map(escapeRegExp).filter(Boolean)
  if (escaped.length === 0) return escapeHtml(text)
  const re = new RegExp(`(${escaped.join('|')})`, 'gi')
  return escapeHtml(text).replace(re, '<mark>$1</mark>')
}
function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function fmtRelativeTime(d) {
  if (!d) return ''
  const ts = new Date(d).getTime()
  if (isNaN(ts)) return ''
  const diff = Date.now() - ts
  const day = 86400000
  if (diff < day) return t('search.time.today')
  if (diff < 2 * day) return t('search.time.yesterday')
  if (diff < 7 * day) return t('search.time.days_ago', { n: Math.floor(diff / day) })
  if (diff < 30 * day) return t('search.time.weeks_ago', { n: Math.floor(diff / (7 * day)) })
  if (diff < 365 * day) return t('search.time.months_ago', { n: Math.floor(diff / (30 * day)) })
  return t('search.time.years_ago', { n: Math.floor(diff / (365 * day)) })
}
</script>

<template>
  <div class="flex flex-col h-full bg-bg overflow-hidden">
    <!-- ── Header / search bar ────────────────────────────────────
         Title + subtitle were dropped — the sidebar tab and the
         input's own placeholder already say what this page is.
         Left-aligned with a generous left gutter (px-12 = 48px)
         so the bar reads as part of the content column rather
         than floating in the middle of the page. -->
    <header class="shrink-0 px-12 pt-10 pb-5">
      <form class="flex items-center gap-2 max-w-[720px]" @submit.prevent="runSearch">
        <div class="search-input-wrap">
          <Search :size="14" :stroke-width="1.75" class="text-t3 shrink-0" />
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
        <button
          type="submit"
          class="search-submit"
          :disabled="_loading || !_query.trim()"
        >
          <Spinner v-if="_loading" size="md" />
          <span v-else>{{ t('search.submit') }}</span>
        </button>
      </form>

      <!-- Translation chip — left-aligned under the bar so the
           header stack shares one vertical axis. -->
      <div v-if="translations" class="mt-3 flex items-center gap-2 max-w-[720px] flex-wrap text-[11px] text-t3">
        <span>{{ t('search.expanded_label') }}</span>
        <span
          v-for="(tx, i) in translations"
          :key="i"
          class="px-1.5 py-0.5 bg-bg2 border border-line rounded text-t2"
        >{{ tx }}</span>
      </div>

      <!-- Filter pills — only when we have results to filter.
           Same 720px / left-aligned slot as the bar above. -->
      <div v-if="hasResults" class="mt-4 flex items-center gap-2 flex-wrap max-w-[720px]">
        <!-- Time -->
        <div class="filter-pill relative">
          <button class="pill-btn" :class="{ 'pill-btn-active': _filterTime !== 'all' }"
            @click.stop="openFilter = openFilter === 'time' ? null : 'time'">
            <Clock :size="13" :stroke-width="1.75" />
            {{ timeLabel }}
            <ChevronDown :size="12" :stroke-width="1.75" />
          </button>
          <div v-if="openFilter === 'time'" class="pill-popover">
            <button class="pop-row" :class="{ 'pop-row-active': _filterTime === 'all' }" @click="pickTime('all')">{{ t('search.filter.time_all') }}</button>
            <button class="pop-row" :class="{ 'pop-row-active': _filterTime === '7d' }" @click="pickTime('7d')">{{ t('search.filter.time_7d') }}</button>
            <button class="pop-row" :class="{ 'pop-row-active': _filterTime === '30d' }" @click="pickTime('30d')">{{ t('search.filter.time_30d') }}</button>
            <button class="pop-row" :class="{ 'pop-row-active': _filterTime === '1y' }" @click="pickTime('1y')">{{ t('search.filter.time_1y') }}</button>
          </div>
        </div>
        <!-- Uploader -->
        <div class="filter-pill relative">
          <button class="pill-btn" :class="{ 'pill-btn-active': _filterUploader !== 'all' }"
            @click.stop="openFilter = openFilter === 'uploader' ? null : 'uploader'">
            <User :size="13" :stroke-width="1.75" />
            {{ uploaderLabel }}
            <ChevronDown :size="12" :stroke-width="1.75" />
          </button>
          <div v-if="openFilter === 'uploader'" class="pill-popover">
            <button class="pop-row" :class="{ 'pop-row-active': _filterUploader === 'all' }" @click="pickUploader('all')">{{ t('search.filter.uploader_all') }}</button>
            <button v-for="(opt, i) in uploaderOptions" :key="i"
              class="pop-row" :class="{ 'pop-row-active': _filterUploader === opt[0] }"
              @click="pickUploader(opt[0])">
              <UserAvatar :name="opt[1]" :size="16" />
              <span>{{ opt[1] }}</span>
            </button>
          </div>
        </div>
        <!-- Format -->
        <div class="filter-pill relative">
          <button class="pill-btn" :class="{ 'pill-btn-active': _filterFormat !== 'all' }"
            @click.stop="openFilter = openFilter === 'format' ? null : 'format'">
            <FileType2 :size="13" :stroke-width="1.75" />
            {{ formatLabel }}
            <ChevronDown :size="12" :stroke-width="1.75" />
          </button>
          <div v-if="openFilter === 'format'" class="pill-popover">
            <button class="pop-row" :class="{ 'pop-row-active': _filterFormat === 'all' }" @click="pickFormat('all')">{{ t('search.filter.format_all') }}</button>
            <button v-for="fmt in formatOptions" :key="fmt"
              class="pop-row" :class="{ 'pop-row-active': _filterFormat === fmt }"
              @click="pickFormat(fmt)">{{ fmt.toUpperCase() }}</button>
          </div>
        </div>
      </div>
    </header>

    <!-- ── Body ─────────────────────────────────────────────────── -->
    <main class="flex-1 flex flex-col overflow-y-auto px-12 pt-2 pb-10">
      <div v-if="_error" class="mt-2 flex items-center gap-2 px-3.5 py-2.5 text-[13px] text-red-600 bg-red-500/[0.08] border border-red-500/20 rounded-md max-w-[720px]">
        <AlertCircle :size="16" />
        <span>{{ _error }}</span>
      </div>

      <!-- ``flex-1`` empty states centre within the full main
           area instead of a fixed 60vh box. Earlier 60vh meant
           short viewports squeezed the icon up while wide
           viewports left it slumped at the top — neither read
           as "centred". -->
      <div v-else-if="_loading && !_results" class="flex-1 flex items-center justify-center text-t3 text-[13px]">
        {{ t('search.empty.searching') }}
      </div>

      <div v-else-if="!_results" class="flex-1 flex flex-col items-center justify-center text-t3 text-center text-[13px]">
        <Search :size="32" :stroke-width="1.5" class="text-t3 opacity-40 mb-3" />
        <p>{{ t('search.empty.idle') }}</p>
        <p class="text-[12px] mt-1.5 opacity-70">{{ t('search.empty.hint') }}</p>
      </div>

      <div v-else-if="!hasResults" class="flex-1 flex flex-col items-center justify-center text-t3 text-center text-[13px]">
        <Search :size="32" :stroke-width="1.5" class="text-t3 opacity-40 mb-3" />
        <p>{{ t('search.empty.none', { query: _query }) }}</p>
      </div>

      <div v-else-if="!hasFiltered" class="flex-1 flex flex-col items-center justify-center text-t3 text-center text-[13px]">
        <Search :size="32" :stroke-width="1.5" class="text-t3 opacity-40 mb-3" />
        <p>{{ t('search.empty.filtered') }}</p>
      </div>

      <div v-else class="max-w-[920px]">
        <ul class="list-none p-0 m-0">
          <li
            v-for="f in files"
            :key="f.doc_id"
            class="row"
            @click="openFile(f)"
          >
            <!-- Row 1: filename + match badge -->
            <div class="row-title">
              <FileText :size="14" :stroke-width="1.75" class="text-t3 shrink-0" />
              <span
                class="filename hl"
                v-html="highlightTokens(f.filename, f.filename_tokens)"
              />
              <span class="badge shrink-0" :class="badgeClass(f.matched_in)">{{ matchBadge(f.matched_in) }}</span>
            </div>

            <!-- Row 2: uploader avatar + name + updated time + format chip + folder path.
                 Each non-chip segment is its own ``meta-item`` and the parent
                 ``row-meta`` injects a separator dot BETWEEN visible items
                 via CSS ``::before`` on n+1, so we never get an orphan dot
                 leading the line when uploader/etc are null. -->
            <div class="row-meta">
              <span
                v-if="f.uploader_display_name || f.uploader_user_id"
                class="meta-item meta-uploader"
              >
                <UserAvatar
                  :name="f.uploader_display_name || f.uploader_user_id"
                  :img-url="avatarUrlFor(f.uploader_user_id, f.uploader_has_avatar)"
                  :size="16"
                />
                <span v-if="f.uploader_display_name" class="text-t2">{{ f.uploader_display_name }}</span>
              </span>
              <span v-if="f.updated_at" class="meta-item">{{ fmtRelativeTime(f.updated_at) }}</span>
              <span v-if="f.format" class="format-chip">{{ f.format.toUpperCase() }}</span>
              <span v-if="f.path" class="meta-item path truncate">{{ f.path }}</span>
            </div>

            <!-- Row 3: snippet -->
            <div
              v-if="f.best_chunk"
              class="row-snippet hl"
              v-html="highlightTokens(f.best_chunk.snippet, f.best_chunk.matched_tokens)"
            />
          </li>
        </ul>

        <div v-if="stats" class="mt-4 pt-3 text-[12px] text-t3">
          {{ t('search.footer_v3', {
            files: filtersActive ? files.length : (stats.file_hits ?? files.length),
            chunks: stats.chunk_hits ?? 0,
            ms: stats.elapsed_ms ?? 0,
          }) }}
          <span v-if="filtersActive" class="ml-1">· {{ t('search.filter.filtered_of', { total: allFiles.length }) }}</span>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
/* Highlight wash — soft amber, never the focus. */
.hl :deep(mark) {
  background: rgba(251, 191, 36, 0.25);
  color: inherit;
  padding: 0 1px;
  border-radius: 2px;
}

/* ── Search bar ────────────────────────────────────────────
   Matches the rest of the app's primary form pattern:
   ``var(--r-md)`` rounding + thin border, no shadow, the
   submit button uses the standard t1/bg invert ("Save"-style
   buttons in Profile / Users / Settings sub-pages). The old
   ``rounded-xl`` + ``bg-brand`` blue stuck out as a one-off. */
.search-input-wrap {
  position: relative;
  flex: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 36px;
  padding: 0 10px;
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  background: var(--color-bg);
  transition: border-color 0.12s, box-shadow 0.12s;
}
.search-input-wrap:focus-within {
  border-color: var(--color-line2);
  box-shadow: var(--ring-focus);
}
.search-input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  outline: none;
  font-size: 13px;
  color: var(--color-t1);
  line-height: 1.5;
}
.search-input::placeholder {
  color: var(--color-t3);
}
.search-clear {
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  background: transparent;
  border: none;
  border-radius: var(--r-sm);
  color: var(--color-t3);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  transition: color 0.12s, background-color 0.12s;
}
.search-clear:hover {
  color: var(--color-t1);
  background: var(--color-bg3);
}

.search-submit {
  flex-shrink: 0;
  min-width: 84px;
  height: 36px;
  padding: 0 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 500;
  border: none;
  border-radius: var(--r-md);
  background: var(--color-t1);
  color: var(--color-bg);
  cursor: pointer;
  transition: opacity 0.15s, background-color 0.15s;
}
.search-submit:hover:not(:disabled) {
  background: var(--color-t1-hover);
}
.search-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ── Result rows ──────────────────────────────────────────────
   Onyx-style: no per-row card border. Hover wash + bottom
   hairline does the separation. Click target is the whole row. */
.row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--color-line);
  transition: background-color 0.12s;
}
.row:hover { background: var(--color-bg2); }
.row:last-child { border-bottom: none; }

.row-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.filename {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-t1);
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.row-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: 22px;
  font-size: 11px;
  color: var(--color-t3);
  min-width: 0;
}
.row-meta .text-t2 { color: var(--color-t2); }
/* Each .meta-item gets a leading "·" except the first visible
   one. format-chip and the avatar wrapper are excluded — the
   chip carries its own background as a visual separator, and
   ``meta-uploader`` always renders first when present. */
.meta-item { display: inline-flex; align-items: center; gap: 4px; }
.meta-item + .meta-item::before,
.format-chip + .meta-item::before {
  content: "·";
  margin-right: 2px;
  color: var(--color-t3);
}
.format-chip {
  padding: 0 5px;
  background: var(--color-bg2);
  border-radius: 3px;
  font-size: 10px;
  letter-spacing: 0.04em;
}
.row-meta .path {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.row-snippet {
  margin-left: 22px;
  font-size: 13px;
  color: var(--color-t2);
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ── Match badge ────────────────────────────────────────────── */
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

/* ── Filter pills + popovers ──────────────────────────────── */
.pill-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
  color: var(--color-t2);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 999px;
  cursor: pointer;
  transition: background-color 0.12s, color 0.12s, border-color 0.12s;
}
.pill-btn:hover {
  background: var(--color-bg2);
  color: var(--color-t1);
}
.pill-btn-active {
  background: var(--color-bg-selected, var(--color-bg2));
  color: var(--color-t1);
  border-color: var(--color-line2, var(--color-line));
  font-weight: 500;
}

.pill-popover {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  min-width: 180px;
  max-height: 280px;
  overflow-y: auto;
  padding: 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.14);
  z-index: 20;
}
.pop-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 8px;
  font-size: 12px;
  color: var(--color-t1);
  background: transparent;
  border: none;
  border-radius: 6px;
  text-align: left;
  cursor: pointer;
  transition: background-color 0.1s;
}
.pop-row:hover { background: var(--color-bg2); }
.pop-row-active { background: var(--color-bg-selected, var(--color-bg2)); font-weight: 500; }
</style>
