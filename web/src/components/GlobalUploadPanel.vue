<template>
  <!-- Hide entirely when nothing has ever been uploaded this session -->
  <div v-if="store.items.length > 0" class="upload-panel-root">
    <!-- ── Expanded drawer (overlay above the bar) ─────────────────────── -->
    <div v-if="store.drawerOpen" class="upload-drawer">
      <div class="drawer-header">
        <div class="flex items-center gap-3 text-2xs">
          <span class="text-t1 font-medium">Uploads</span>
          <span class="text-t3">·</span>
          <span class="text-t2">{{ store.items.length }} total</span>
        </div>
        <div class="flex items-center gap-3">
          <button
            v-if="store.completed.length"
            @click="store.clearCompleted"
            class="text-3xs text-t3 hover:text-t1"
          >clear done ({{ store.completed.length }})</button>
          <button
            v-if="store.failed.length"
            @click="store.clearFailed"
            class="text-3xs text-t3 hover:text-t1"
          >clear failed ({{ store.failed.length }})</button>
          <button
            @click="store.toggleDrawer(false)"
            class="text-t3 hover:text-t1 text-xs leading-none"
            title="Collapse"
          >⌄</button>
        </div>
      </div>

      <div class="drawer-body">
        <div v-if="!pageItems.length" class="text-2xs text-t3 py-6 text-center">
          No items on this page.
        </div>
        <div
          v-for="it in pageItems"
          :key="it.id"
          class="row"
          :class="{ 'row-error': it.state === 'error' }"
        >
          <!-- state icon -->
          <span class="state-icon" :class="'state-' + stateKind(it)">
            <template v-if="stateKind(it) === 'ok'">✓</template>
            <template v-else-if="stateKind(it) === 'err'">✕</template>
            <template v-else-if="stateKind(it) === 'cancel'">—</template>
            <template v-else>⟳</template>
          </span>

          <!-- progress (hair-thin inline bar behind name), or error detail -->
          <div class="row-main">
            <div class="row-line1">
              <span class="row-name" :title="it.name">{{ it.name }}</span>
              <span class="row-folder" :title="it.folderPath">{{ it.folderPath }}</span>
            </div>
            <div v-if="it.state === 'error'" class="row-error-msg" :title="it.error">
              {{ it.error || 'ingestion failed' }}
            </div>
            <div v-else class="progress-track">
              <div class="progress-fill" :style="{ width: it.progress + '%' }"></div>
            </div>
          </div>

          <!-- status text + size -->
          <div class="row-status">
            <span class="text-t2 whitespace-nowrap">{{ shortStatusLabel(it) }}</span>
            <span class="text-t3 whitespace-nowrap">{{ fmtSize(it.size) }}</span>
          </div>

          <!-- actions -->
          <div class="row-actions">
            <button
              v-if="it.state === 'error' && it.file"
              @click="store.retry(it.id)"
              title="Retry"
              class="btn-icon"
            >↻</button>
            <button
              v-if="it.state === 'uploading' || it.state === 'ingesting' || it.state === 'queued'"
              @click="store.cancel(it.id)"
              title="Cancel"
              class="btn-icon"
            >╳</button>
            <button
              v-if="isTerminal(it)"
              @click="store.remove(it.id)"
              title="Remove from list"
              class="btn-icon"
            >🗑</button>
          </div>
        </div>
      </div>

      <!-- pagination -->
      <div v-if="totalPages > 1" class="drawer-pager">
        <button
          :disabled="page === 1"
          @click="page = Math.max(1, page - 1)"
          class="pager-btn"
        >‹</button>
        <span class="text-3xs text-t3">{{ page }} / {{ totalPages }}</span>
        <button
          :disabled="page === totalPages"
          @click="page = Math.min(totalPages, page + 1)"
          class="pager-btn"
        >›</button>
      </div>
    </div>

    <!-- ── Status bar (always visible when there are items) ────────────── -->
    <div
      class="upload-bar"
      @click="store.toggleDrawer()"
      role="button"
    >
      <div class="bar-left">
        <span class="chev">{{ store.drawerOpen ? '⌄' : '⌃' }}</span>
        <span class="text-t1 font-medium">Uploads</span>
        <span v-if="store.active.length" class="chip chip-run">
          ⟳ {{ store.active.length }}
        </span>
        <span v-if="store.completed.length" class="chip chip-ok">
          ✓ {{ store.completed.length }}
        </span>
        <span v-if="store.failed.length" class="chip chip-err">
          ✕ {{ store.failed.length }}
        </span>
      </div>
      <div class="bar-right text-t3">
        <span v-if="totalActiveBytes">{{ fmtSize(totalActiveBytes) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useUploadsStore } from '@/stores/uploads'

const PAGE_SIZE = 8

const store = useUploadsStore()
const page = ref(1)

const sorted = computed(() => {
  // active first, then newest terminal first
  const rank = (it) =>
    it.state === 'uploading' ? 0
    : it.state === 'ingesting' ? 1
    : it.state === 'queued' ? 2
    : 3
  return [...store.items].sort((a, b) => {
    const r = rank(a) - rank(b)
    if (r !== 0) return r
    return (b.created_at || 0) - (a.created_at || 0)
  })
})

const totalPages = computed(() => Math.max(1, Math.ceil(sorted.value.length / PAGE_SIZE)))

const pageItems = computed(() => {
  const start = (page.value - 1) * PAGE_SIZE
  return sorted.value.slice(start, start + PAGE_SIZE)
})

// If current page becomes empty (e.g. after clearCompleted), snap back
watch(totalPages, (n) => { if (page.value > n) page.value = n })

const totalActiveBytes = computed(() =>
  store.active.reduce((sum, it) => sum + (it.size || 0), 0),
)

function stateKind(it) {
  if (it.state === 'ready') return 'ok'
  if (it.state === 'error') return 'err'
  if (it.state === 'cancelled') return 'cancel'
  return 'run'
}

function statusLabel(it) {
  if (it.state === 'ready') return 'ready'
  if (it.state === 'error') return it.error || 'error'
  if (it.state === 'cancelled') return 'cancelled'
  if (it.state === 'queued') return 'queued'
  if (it.state === 'uploading') return 'uploading…'
  if (it.state === 'ingesting') return it.backend_status || 'ingesting…'
  return it.state
}

// Short label for the right column — full error text is rendered separately
// below the filename, so here we just show the state word.
function shortStatusLabel(it) {
  if (it.state === 'error') return 'error'
  return statusLabel(it)
}

function isTerminal(it) {
  return ['ready', 'error', 'cancelled'].includes(it.state)
}

function fmtSize(n) {
  if (!n) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}
</script>

<style scoped>
.upload-panel-root {
  position: fixed;
  left: 240px;          /* past the sidebar (w-60 = 240px) */
  right: 0;
  bottom: 0;
  z-index: 40;
  pointer-events: none; /* only the bar + drawer receive events */
}

/* ── Status bar ───────────────────────────────────────────────────── */
.upload-bar {
  pointer-events: auto;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 14px;
  font-size: 0.6875rem;
  color: var(--color-t2);
  background: var(--color-bg);
  border-top: 1px solid var(--color-line);
  cursor: pointer;
  user-select: none;
  transition: background 0.1s;
}
.upload-bar:hover { background: var(--color-bg2); }

.bar-left { display: flex; align-items: center; gap: 8px; }
.bar-right { display: flex; align-items: center; gap: 8px; font-size: 0.625rem; }
.chev { font-size: 0.625rem; color: var(--color-t3); width: 10px; display: inline-block; }

/* Chip styling is provided globally in style.css (.chip / .chip-ok/err/run).
   Compact bar variant: slightly denser padding + inline icon. */
.chip {
  gap: 3px;
  line-height: 1.4;
}

/* ── Drawer ───────────────────────────────────────────────────────── */
.upload-drawer {
  pointer-events: auto;
  position: absolute;
  left: 0;
  right: 0;
  bottom: 26px;  /* sit above the bar */
  max-height: 400px;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
  border-top: 1px solid var(--color-line);
  /* No box-shadow — Vercel uses a crisp 1px border instead */
}

.drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 14px;
  border-bottom: 1px solid var(--color-line);
}

.drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.row {
  display: grid;
  grid-template-columns: 18px 1fr auto auto;
  align-items: center;
  gap: 10px;
  padding: 5px 14px;
  font-size: 0.6875rem;
  border-top: 1px solid transparent;
}
.row:not(:first-child) { border-top-color: var(--color-line); }
.row:hover { background: var(--color-bg2); }
.row-error .row-name { color: var(--color-err-fg); }

.state-icon {
  font-size: 0.625rem;
  text-align: center;
  line-height: 1rem;
  width: 16px;
  height: 16px;
  border-radius: 3px;
}
.state-run { color: var(--color-run-fg); animation: pulse 1.2s ease-in-out infinite; }
.state-ok  { color: var(--color-ok-fg); }
.state-err { color: var(--color-err-fg); }
.state-cancel { color: var(--color-t3); }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

.row-main { min-width: 0; }
.row-line1 {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.row-name {
  color: var(--color-t1);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex-shrink: 1;
  min-width: 0;
}
.row-folder {
  color: var(--color-t3);
  font-size: 0.625rem;
  flex-shrink: 0;
}

.progress-track {
  height: 1px;
  background: var(--color-line);
  margin-top: 3px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: var(--color-t1);
  transition: width 0.3s ease;
}

/* Error detail shown inline under the filename for terminal=error rows.
   Clamped to 2 lines; full text in title attribute on hover. */
.row-error-msg {
  margin-top: 3px;
  font-size: 0.625rem;
  color: var(--color-err-fg);
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: help;
}

.row-status {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  font-size: 0.625rem;
  gap: 2px;
}

.row-actions {
  display: flex;
  gap: 2px;
}
/* .btn-icon styling lives in style.css (global). Row action buttons render
   a touch smaller than the default to fit the 26px row height. */
.row-actions .btn-icon {
  width: 20px;
  height: 20px;
  font-size: 0.6875rem;
}

/* ── Pager ────────────────────────────────────────────────────────── */
.drawer-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 6px 0 8px;
  border-top: 1px solid var(--color-line);
}
.pager-btn {
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 0.75rem;
  color: var(--color-t2);
  background: transparent;
  border: none;
  cursor: pointer;
  border-radius: 3px;
}
.pager-btn:hover:not(:disabled) { background: var(--color-bg2); color: var(--color-t1); }
.pager-btn:disabled { opacity: 0.3; cursor: not-allowed; }
</style>
