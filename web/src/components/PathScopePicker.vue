<!--
  PathScopePicker — sets ``path_filter`` for the chat by browsing folders
  and files. Backend already supports both via prefix matching on
  ``Document.path`` (which stores the full virtual path including the
  filename), so a folder pick → subtree scope, a file pick → that
  single document. See retrieval/components/path_scope.py.

  Trigger button shows current scope ("全部" / "📁 /agriculture" /
  "📄 /agriculture/00_b_eekeeping.md"). Clicking opens a popup above
  the trigger with a search box, breadcrumb-back, and a list of the
  current directory's subfolders + direct files. Behavior:
    • Click a folder row → scope on that folder AND navigate into it.
      So as you drill the scope follows you to the deepest folder
      you've clicked. The panel stays open so you can keep diving
      (or pick a file inside).
    • Click a file row → scope on that file and close (terminal).
    • The breadcrumb's "选中此目录" button is a sugar shortcut:
      "I'm done picking, close the panel." It also re-asserts the
      current dir as scope (handy if you went UP via the arrow,
      since the arrow only navigates and leaves scope alone).
    • Up arrow → navigate up only; doesn't change scope.
    • Click "全部文档" at the root list → clear scope and close.

  Search box is server-side: when non-empty we call ``listDocuments``
  with ``recursive=true`` rooted at the current directory and ``search``
  matched on doc name. Folders are hidden during search (BE doesn't
  index folders by name; cheap to add later if needed).
-->
<template>
  <div ref="rootEl" class="relative inline-block">
    <!-- Badge-style trigger: ``bg-bg3`` fill (no border) so it reads
         as a distinct chip without colliding with the input card's
         border below. The fill differentiates it from the surrounding
         transparent area; hover deepens. -->
    <button
      type="button"
      class="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-bg3/70 text-2xs text-t2 hover:bg-bg3 transition-colors"
      :class="{ 'text-brand': scoped, '!bg-bg3': open }"
      :title="scoped ? t('scope.tooltip_scoped_to', { path: modelValue }) : t('scope.tooltip_idle')"
      @click="toggle"
    >
      <component :is="scopeIcon" class="w-3.5 h-3.5" />
      <span class="truncate max-w-[240px]">{{ displayLabel }}</span>
      <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
        class="ml-0.5 transition-transform" :class="open ? 'rotate-180' : ''">
        <path d="M6 9l6 6 6-6" />
      </svg>
    </button>

    <Transition name="popup">
      <div
        v-if="open"
        class="absolute bottom-full left-0 mb-1.5 w-[340px] rounded-xl border border-line bg-bg shadow-lg overflow-hidden z-20"
      >
        <!-- Search -->
        <div class="px-3 pt-2.5 pb-1.5 border-b border-line">
          <div class="flex items-center gap-2 text-xs text-t2">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
              class="text-t3 shrink-0">
              <circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>
            </svg>
            <input
              ref="searchEl"
              v-model="search"
              type="text"
              :placeholder="t('scope.search_placeholder')"
              class="flex-1 bg-transparent border-none outline-none text-t1 placeholder:text-t3"
              @keydown.escape="close"
            />
            <button
              v-if="search"
              class="text-t3 hover:text-t1 text-3xs"
              :title="t('common.close')"
              @click="search = ''"
            >✕</button>
          </div>
        </div>

        <!-- Breadcrumb / current location (hidden during search) -->
        <div v-if="!search" class="flex items-center gap-1 px-3 py-1.5 border-b border-line text-2xs text-t3">
          <button
            class="p-0.5 rounded hover:bg-bg3 disabled:opacity-30 disabled:cursor-default"
            :disabled="currentDir === '/'"
            :title="t('scope.tooltip_up')"
            @click="navigateUp"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
              <path d="M15 18l-6-6 6-6"/>
            </svg>
          </button>
          <span class="font-mono truncate flex-1">{{ currentDir }}</span>
          <button
            v-if="currentDir !== '/'"
            class="px-1.5 py-0.5 rounded text-brand hover:bg-brand/10 text-3xs font-medium"
            :title="t('scope.tooltip_confirm_close')"
            @click="confirmCurrent"
          >{{ t('scope.select_this_folder') }}</button>
        </div>

        <!-- List -->
        <div class="max-h-[280px] overflow-y-auto">
          <!-- "All documents" option (only at root, no search) -->
          <button
            v-if="!search && currentDir === '/'"
            type="button"
            class="w-full flex items-center gap-2 px-3 py-2 text-xs text-t2 hover:bg-bg3 border-b border-line/50"
            :class="{ 'text-brand': !modelValue || modelValue === '/' }"
            @click="selectRoot"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 010 20M12 2a15 15 0 000 20"/>
            </svg>
            <span class="flex-1 text-left">{{ t('scope.all_documents') }}</span>
            <svg v-if="!modelValue || modelValue === '/'" width="10" height="10" viewBox="0 0 24 24"
              fill="none" stroke="currentColor" stroke-width="3">
              <path d="M20 6L9 17l-5-5"/>
            </svg>
          </button>

          <!-- Loading state -->
          <div v-if="loading" class="flex items-center justify-center py-6 text-2xs text-t3 gap-2">
            <Spinner size="sm" /> {{ t('common.loading') }}
          </div>

          <!-- Empty state -->
          <div v-else-if="!folders.length && !docs.length" class="py-6 text-center text-2xs text-t3">
            <span v-if="search">{{ t('scope.no_match', { query: search }) }}</span>
            <span v-else>{{ t('scope.empty_dir') }}</span>
          </div>

          <template v-else>
            <!-- Folders: single click = scope + drill in (don't close
                 the panel — user can keep diving or pick a file inside). -->
            <button
              v-for="f in folders"
              :key="'f:' + f.path"
              type="button"
              class="w-full flex items-center gap-2 px-3 py-2 text-xs text-t1 hover:bg-bg3"
              :class="{ 'text-brand bg-brand/5': modelValue === f.path }"
              :title="t('scope.tooltip_pick_and_dive', { path: f.path })"
              @click="pickFolder(f)"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                class="shrink-0" :class="modelValue === f.path ? 'text-brand' : 'text-t2'">
                <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>
              </svg>
              <span class="flex-1 text-left truncate">{{ f.name || leaf(f.path) }}</span>
              <svg v-if="modelValue === f.path" width="10" height="10" viewBox="0 0 24 24"
                fill="none" stroke="currentColor" stroke-width="3" class="text-brand shrink-0">
                <path d="M20 6L9 17l-5-5"/>
              </svg>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                class="text-t3 shrink-0">
                <path d="M9 6l6 6-6 6"/>
              </svg>
            </button>

            <!-- Files -->
            <button
              v-for="d in docs"
              :key="'d:' + d.doc_id"
              type="button"
              class="w-full flex items-center gap-2 px-3 py-2 text-xs text-t1 hover:bg-bg3"
              :class="{ 'text-brand bg-brand/5': modelValue === d.path }"
              :title="d.path"
              @click="selectFile(d)"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                class="text-t2 shrink-0">
                <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/>
                <path d="M14 2v6h6"/>
              </svg>
              <span class="flex-1 text-left truncate">{{ leaf(d.path) }}</span>
              <span v-if="search" class="text-t3 text-3xs font-mono truncate max-w-[140px]">{{ parentDir(d.path) }}</span>
              <svg v-if="modelValue === d.path" width="10" height="10" viewBox="0 0 24 24"
                fill="none" stroke="currentColor" stroke-width="3">
                <path d="M20 6L9 17l-5-5"/>
              </svg>
            </button>
          </template>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, h } from 'vue'
import { useI18n } from 'vue-i18n'
import { listDocuments, getFolderTree } from '@/api'
import Spinner from './Spinner.vue'

const { t } = useI18n()

const props = defineProps({
  modelValue: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const open = ref(false)
const currentDir = ref('/')
const search = ref('')
const folders = ref([])
const docs = ref([])
const loading = ref(false)
const rootEl = ref(null)
const searchEl = ref(null)

// ── Display ──────────────────────────────────────────────────────────
const scoped = computed(() => !!props.modelValue && props.modelValue !== '/')
const isFile = computed(() => /\.[a-z0-9]+$/i.test(props.modelValue || ''))

const displayLabel = computed(() => {
  if (!scoped.value) return t('scope.all_documents')
  // Show the leaf for compactness; tooltip carries the full path.
  return leaf(props.modelValue)
})

// Folder vs file vs root icon for the trigger
const FolderSvg = {
  render: () => h('svg', { width: 14, height: 14, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2 },
    [h('path', { d: 'M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z' })])
}
const FileSvg = {
  render: () => h('svg', { width: 14, height: 14, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2 },
    [h('path', { d: 'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z' }),
     h('path', { d: 'M14 2v6h6' })])
}
const GlobeSvg = {
  render: () => h('svg', { width: 14, height: 14, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2 },
    [h('circle', { cx: 12, cy: 12, r: 10 }),
     h('path', { d: 'M2 12h20M12 2a15 15 0 010 20M12 2a15 15 0 000 20' })])
}
const scopeIcon = computed(() => !scoped.value ? GlobeSvg : isFile.value ? FileSvg : FolderSvg)

// ── Helpers ──────────────────────────────────────────────────────────
function leaf(path) { return (path || '').split('/').pop() || path || '/' }
function parentDir(path) {
  const parts = (path || '').split('/').filter(Boolean)
  parts.pop()
  return '/' + parts.join('/')
}

// ── Open/close ──────────────────────────────────────────────────────
function toggle() {
  open.value = !open.value
  if (open.value) {
    // When opening, jump into the parent dir of the current scope so the
    // user sees the context. If they're scoped on a file, show its folder.
    const v = props.modelValue
    if (v && v !== '/') {
      currentDir.value = isFile.value ? parentDir(v) : v
    } else {
      currentDir.value = '/'
    }
    search.value = ''
    // Autofocus search field next tick
    setTimeout(() => searchEl.value?.focus(), 30)
  }
}
function close() { open.value = false }

// ── Data loading ─────────────────────────────────────────────────────
async function loadCurrent() {
  loading.value = true
  try {
    const dir = currentDir.value
    if (search.value.trim()) {
      // Server-side search across subtree (rooted at currentDir).
      const res = await listDocuments({
        search: search.value.trim(),
        path_filter: dir === '/' ? undefined : dir,
        recursive: true,
        limit: 50,
      })
      docs.value = res.items || []
      folders.value = []   // backend doesn't search folders by name
    } else {
      // Browse: subfolders + direct files
      const [tree, docList] = await Promise.all([
        getFolderTree(dir, 1, false).catch(() => ({ children: [] })),
        listDocuments({
          path_filter: dir === '/' ? undefined : dir,
          recursive: false,
          limit: 100,
        }).catch(() => ({ items: [] })),
      ])
      const children = (tree.children || []).filter(f => !f.path?.startsWith('/__trash__'))
      folders.value = children
      docs.value = (docList.items || []).filter(d => !d.path?.startsWith('/__trash__'))
    }
  } finally {
    loading.value = false
  }
}

// Debounce search a bit so we don't hammer the API on every keystroke.
let _searchTimer = null
watch(search, () => {
  if (!open.value) return
  if (_searchTimer) clearTimeout(_searchTimer)
  _searchTimer = setTimeout(() => loadCurrent(), 200)
})
watch(currentDir, () => { if (open.value) loadCurrent() })
watch(open, (v) => { if (v) loadCurrent() })

// ── Navigation ───────────────────────────────────────────────────────
function navigateUp() {
  // Pure navigation — does NOT change scope. Use the breadcrumb's
  // "选中此目录" button (or click another row) to commit to a new dir.
  currentDir.value = parentDir(currentDir.value) || '/'
  search.value = ''
}

// ── Selection ────────────────────────────────────────────────────────
// Click a folder row: scope on it AND dive in. Don't close — user may
// want to keep narrowing (or pick a file inside).
function pickFolder(folder) {
  emit('update:modelValue', folder.path)
  currentDir.value = folder.path
  search.value = ''
}
// Sugar: explicit "I'm done" button. Scope on the current dir (a no-op
// if already there) and close the panel.
function confirmCurrent() {
  if (currentDir.value && currentDir.value !== '/') {
    emit('update:modelValue', currentDir.value)
  }
  close()
}
function selectRoot() { emit('update:modelValue', ''); close() }
function selectFile(doc) { emit('update:modelValue', doc.path); close() }

// ── Click-outside ────────────────────────────────────────────────────
function onDocClick(e) {
  if (!open.value || !rootEl.value) return
  if (!rootEl.value.contains(e.target)) close()
}
onMounted(() => document.addEventListener('mousedown', onDocClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))
</script>

<style scoped>
.popup-enter-active, .popup-leave-active { transition: opacity .15s ease, transform .15s ease; }
.popup-enter-from, .popup-leave-to { opacity: 0; transform: translateY(4px); }
</style>
