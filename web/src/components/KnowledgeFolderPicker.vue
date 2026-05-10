<template>
  <!--
    Knowledge picker — modal that lets the user browse the Library
    tree (folders + indexed documents) and pick ONE entry to add as
    a knowledge-scope chip on the chat composer. Sibling of
    ``WorkdirFolderPicker`` (same modal shell, same UX rhythm) but
    sourced from the Library APIs (``getFolderTree`` / ``listDocuments``)
    instead of the per-user workdir tree.

    "Select" emits the entry's path (folder path or full doc path,
    e.g. ``/sales/2025`` or ``/sales/2025/Q3-report.pdf``). Caller
    pushes that into the chip rail; the chat route forwards the
    accumulated set as the agent's preferred search scope hint.

    Single-select per modal session — every "Select" closes the
    dialog. The chip rail is the place that accumulates multiple
    pins; reopening the modal lets the user add another. Selection
    inside this modal is "click to drill in (folder)" or "click to
    pin (file)" — there's no checkbox / multi-select flow because
    the chip rail outside is the truth, not the modal.
  -->
  <Teleport to="body">
    <div
      v-if="open"
      class="dialog-backdrop"
      @click.self="onCancel"
      @keydown.esc="onCancel"
    >
      <div class="picker panel" role="dialog" aria-modal="true" tabindex="-1" ref="dialogEl">
          <div class="picker__header">
            <h2 class="picker__title">{{ title || t('chat.knowledge_picker.title') }}</h2>
            <p class="picker__desc">{{ description || t('chat.knowledge_picker.description') }}</p>
          </div>

          <!-- Search box — sits between header and breadcrumb so the
               user can fall through the tree from the keyboard alone.
               Server-side recursive search rooted at the current dir
               (matches PathScopePicker behaviour). -->
          <div class="picker__search">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              stroke-width="2" class="picker__search-icon">
              <circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>
            </svg>
            <input
              ref="searchEl"
              v-model="search"
              type="text"
              :placeholder="t('chat.knowledge_picker.search_placeholder')"
              class="picker__search-input"
              @keydown.escape.prevent="search ? (search = '') : onCancel()"
            />
            <button
              v-if="search"
              class="picker__search-clear"
              :title="t('common.close')"
              @click="search = ''"
            >✕</button>
          </div>

          <div class="picker__crumb" v-if="!search">
            <button
              v-for="(seg, idx) in breadcrumb"
              :key="seg.path"
              class="picker__crumb-btn"
              :disabled="idx === breadcrumb.length - 1"
              @click="navigateTo(seg.path)"
            >{{ seg.label }}</button>
            <span class="picker__crumb-spacer"></span>
          </div>

          <div class="picker__body">
            <div v-if="loading" class="picker__hint">{{ t('common.loading') }}</div>
            <div v-else-if="error" class="picker__hint picker__hint--error">{{ error }}</div>
            <div v-else-if="!folders.length && !docs.length" class="picker__hint">
              <span v-if="search">{{ t('scope.no_match', { query: search }) }}</span>
              <span v-else>{{ t('scope.empty_dir') }}</span>
            </div>

            <template v-else>
              <!-- Folders: click to drill in. Folder pin is via the
                   "Pin this folder" footer button (or a future
                   right-aligned pin icon on hover) — clicking the
                   row is purely navigational so the user can dig
                   without committing. -->
              <button
                v-for="f in folders"
                :key="'f:' + f.path"
                class="picker__row picker__row--folder"
                :title="t('chat.knowledge_picker.tooltip_drill', { path: f.path })"
                @click="navigateTo(f.path)"
              >
                <FileIcon kind="folder" :size="16" />
                <span class="picker__row-name">{{ f.name || leaf(f.path) }}</span>
                <span class="picker__row-meta" v-if="f.doc_count != null">{{ f.doc_count }}</span>
                <ChevronRight class="picker__row-chev" :size="14" :stroke-width="1.5" />
              </button>

              <!-- Files: click to pin AND close (single-select) -->
              <button
                v-for="d in docs"
                :key="'d:' + d.doc_id"
                class="picker__row picker__row--doc"
                :title="d.path"
                @click="selectFile(d)"
              >
                <FileIcon :kind="docKind(d)" :size="16" />
                <span class="picker__row-name">{{ leaf(d.path) }}</span>
                <span v-if="search" class="picker__row-parent">{{ parentDir(d.path) }}</span>
              </button>
            </template>
          </div>

          <div class="picker__footer">
            <div class="picker__actions">
              <button class="btn-secondary" @click="onCancel">{{ t('common.cancel') }}</button>
              <button
                v-if="!search && currentPath !== '/'"
                class="btn-primary"
                @click="selectCurrentFolder"
              >{{ t('chat.knowledge_picker.pin_folder', { path: currentPath }) }}</button>
              <button
                v-else-if="!search"
                class="btn-primary"
                disabled
                :title="t('chat.knowledge_picker.tooltip_pick_in')"
              >{{ t('chat.knowledge_picker.pin_folder_disabled') }}</button>
            </div>
          </div>
        </div>
    </div>
  </Teleport>
</template>

<script setup>
/**
 * Knowledge picker — modal sibling of WorkdirFolderPicker, sources
 * from the Library tree. See template comment for UX rationale.
 */
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronRight } from 'lucide-vue-next'
import { listDocuments, getFolderTree } from '@/api'
import FileIcon from '@/components/workspace/FileIcon.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: '' },
  description: { type: String, default: '' },
  initialPath: { type: String, default: '/' },
  // Paths already in the chip rail — used to dim / mark already-pinned
  // entries so the user doesn't double-pin. Compared by exact match.
  alreadyPinned: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:open', 'select', 'cancel'])

const { t } = useI18n()

const dialogEl = ref(null)
const searchEl = ref(null)
const currentPath = ref('/')
const folders = ref([])
const docs = ref([])
const loading = ref(false)
const error = ref('')
const search = ref('')

// On open: reset state, focus the search box, fetch the initial dir.
// Closing leaves currentPath alone — reopening picks up where you
// left off, which is what users expect when they ✕ a chip and want
// to repin from the same context.
watch(() => props.open, async (now) => {
  if (now) {
    error.value = ''
    search.value = ''
    currentPath.value = props.initialPath || '/'
    await loadCurrent()
    await nextTick()
    searchEl.value?.focus()
  }
})

// Debounce search a bit so we don't hammer the API on every keystroke.
let _searchTimer = null
watch(search, () => {
  if (!props.open) return
  if (_searchTimer) clearTimeout(_searchTimer)
  _searchTimer = setTimeout(() => loadCurrent(), 200)
})
watch(currentPath, () => { if (props.open) loadCurrent() })

async function loadCurrent() {
  loading.value = true
  error.value = ''
  try {
    const dir = currentPath.value
    if (search.value.trim()) {
      // Server-side search across subtree (rooted at currentDir).
      // Folder name matches aren't indexed, so during search we hide
      // folders and rely on doc name + content hits. Cheap to add
      // folder-name search later if it shows up as a UX gap.
      const res = await listDocuments({
        search: search.value.trim(),
        path_filter: dir === '/' ? undefined : dir,
        recursive: true,
        limit: 50,
      })
      docs.value = (res.items || []).filter(d => !d.path?.startsWith('/__trash__'))
      folders.value = []
    } else {
      const [tree, docList] = await Promise.all([
        getFolderTree(dir, 1, false).catch(() => ({ children: [] })),
        listDocuments({
          path_filter: dir === '/' ? undefined : dir,
          recursive: false,
          limit: 100,
        }).catch(() => ({ items: [] })),
      ])
      folders.value = (tree.children || []).filter(f => !f.path?.startsWith('/__trash__'))
      docs.value = (docList.items || []).filter(d => !d.path?.startsWith('/__trash__'))
    }
  } catch (e) {
    error.value = e?.message || String(e)
    folders.value = []
    docs.value = []
  } finally {
    loading.value = false
  }
}

const breadcrumb = computed(() => {
  if (currentPath.value === '/') return [{ path: '/', label: t('chat.knowledge_picker.root') }]
  const parts = currentPath.value.split('/').filter(Boolean)
  const segs = [{ path: '/', label: t('chat.knowledge_picker.root') }]
  let acc = ''
  for (const p of parts) {
    acc += '/' + p
    segs.push({ path: acc, label: p })
  }
  return segs
})

function leaf(path) { return (path || '').split('/').pop() || path || '/' }
function parentDir(path) {
  const parts = (path || '').split('/').filter(Boolean)
  parts.pop()
  return '/' + parts.join('/')
}
function docKind(d) {
  // FileIcon dispatches off ``kind`` (folder / pdf / image / text /
  // other). For library docs we only have the path; pull off the
  // extension to pick a glyph. Plain ``.md`` / ``.txt`` map to text;
  // PDFs to pdf; everything else falls back to ``other``.
  const ext = (d.path || '').toLowerCase().split('.').pop()
  if (ext === 'pdf') return 'pdf'
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'].includes(ext)) return 'image'
  if (['md', 'txt', 'html', 'csv', 'json'].includes(ext)) return 'text'
  return 'other'
}

async function navigateTo(path) {
  currentPath.value = path
}

function selectCurrentFolder() {
  // Pin the breadcrumb's terminal folder. Useful when the user has
  // drilled into ``/sales/2025`` and wants the WHOLE folder, not a
  // specific file — they hit the footer button.
  const path = currentPath.value
  if (!path || path === '/') return
  emit('select', path)
  emit('update:open', false)
}

function selectFile(doc) {
  emit('select', doc.path)
  emit('update:open', false)
}

function onCancel() {
  emit('cancel')
  emit('update:open', false)
}
</script>

<style scoped>
/* Same shell as WorkdirFolderPicker so the two pickers feel like one
   component. Duplicated rather than imported because scoped styles
   can't share via @import; if we end up with a third sibling we'll
   factor the shell out into a base. */
.dialog-backdrop {
  position: fixed;
  inset: 0;
  background: color-mix(in srgb, #000 45%, transparent);
  backdrop-filter: blur(2px);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.picker {
  width: 100%;
  max-width: 520px;
  max-height: 70vh;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
  outline: none;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
  border: 1px solid var(--color-line);
  border-radius: 8px;
  overflow: hidden;
}
.picker__header { padding: 16px 18px 8px; }
.picker__title {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-t1);
  margin: 0;
  letter-spacing: -0.01em;
}
.picker__desc {
  margin: 6px 0 0;
  font-size: 0.75rem;
  color: var(--color-t2);
  line-height: 1.55;
}
.picker__search {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 14px 8px;
  padding: 6px 10px;
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
  border-radius: 6px;
}
.picker__search-icon { color: var(--color-t3); flex-shrink: 0; }
.picker__search-input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  outline: none;
  font-size: 0.75rem;
  color: var(--color-t1);
}
.picker__search-input::placeholder { color: var(--color-t3); }
.picker__search-clear {
  background: transparent;
  border: none;
  color: var(--color-t3);
  font-size: 0.625rem;
  cursor: pointer;
}
.picker__search-clear:hover { color: var(--color-t1); }
.picker__crumb {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  border-top: 1px solid var(--color-line);
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
  overflow-x: auto;
  white-space: nowrap;
}
.picker__crumb-btn {
  padding: 3px 7px;
  font-size: 0.6875rem;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
}
.picker__crumb-btn:hover:not(:disabled) {
  background: var(--color-bg);
  color: var(--color-t1);
}
.picker__crumb-btn:disabled {
  color: var(--color-t1);
  font-weight: 500;
  cursor: default;
}
.picker__crumb-btn:not(:last-of-type)::after {
  content: '/';
  margin-left: 8px;
  color: var(--color-t3);
}
.picker__crumb-spacer { flex: 1; }
.picker__body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
  min-height: 200px;
}
.picker__hint {
  padding: 18px 18px;
  font-size: 0.75rem;
  color: var(--color-t3);
  text-align: center;
}
.picker__hint--error { color: var(--color-err-fg); }
.picker__row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 16px;
  font-size: 0.75rem;
  color: var(--color-t1);
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
}
.picker__row:hover { background: var(--color-bg2); }
.picker__row-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.picker__row-meta {
  font-size: 0.625rem;
  color: var(--color-t3);
  font-feature-settings: "tnum";
}
.picker__row-parent {
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 0.625rem;
  color: var(--color-t3);
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.picker__row-chev { color: var(--color-t3); }
.picker__footer {
  padding: 10px 16px;
  border-top: 1px solid var(--color-line);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  background: var(--color-bg2);
}
.picker__actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.btn-primary, .btn-secondary {
  padding: 5px 11px;
  font-size: 0.6875rem;
  border-radius: 5px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.btn-secondary {
  color: var(--color-t2);
  background: transparent;
  border: 1px solid var(--color-line);
}
.btn-secondary:hover {
  color: var(--color-t1);
  background: var(--color-bg);
}
.btn-primary {
  color: white;
  background: var(--color-brand);
  border: 1px solid var(--color-brand);
  /* Long path text in the button label can overflow on narrow modals;
     truncate to keep the footer one row. */
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
