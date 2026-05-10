<template>
  <div class="workbench" tabindex="0" @click="onWorkbenchClick">
    <!-- Toolbar — same vertical metrics as Library's. The
         ``.toolbar-btn`` look is duplicated locally rather than
         imported because Library's Toolbar.vue scopes the rule. -->
    <div class="wb-toolbar">
      <Breadcrumb :crumbs="crumbs" @navigate="open" />

      <div class="flex-1"></div>

      <button
        v-if="currentPath && currentPath !== '/'"
        class="toolbar-btn"
        :title="t('workspace.open_chat_in', { path: currentPath })"
        @click="onOpenChatCurrent"
      >
        <MessageSquare :size="14" :stroke-width="1.5" />
        <span>{{ t('workspace.open_chat_here') }}</span>
      </button>
      <button
        class="toolbar-btn"
        :disabled="busy"
        @click="onMakeFolder"
      >
        <FolderPlus :size="14" :stroke-width="1.5" />
        <span>{{ t('workspace.new_folder') }}</span>
      </button>
      <label
        class="toolbar-btn"
        :class="{ 'is-disabled': busy }"
        :title="t('workspace.upload')"
      >
        <Upload :size="14" :stroke-width="1.5" />
        <span>{{ t('workspace.upload') }}</span>
        <input
          ref="uploadInput"
          type="file"
          class="upload-input"
          :disabled="busy"
          @change="onUpload"
        />
      </label>

      <!-- Search — client-side filter over the current folder's
           entries. Same affordance as Library's toolbar search; the
           input matches name substrings case-insensitively. -->
      <div class="search-wrap">
        <Search class="search-icon" :size="14" :stroke-width="1.5" />
        <input
          v-model="searchQuery"
          :placeholder="t('workspace.search_placeholder')"
          class="search-input"
        />
        <button
          v-if="searchQuery"
          class="search-clear"
          :title="t('workspace.search_clear')"
          @click="searchQuery = ''"
        ><X :size="12" :stroke-width="1.5" /></button>
      </div>

      <!-- View mode toggle — list vs grid. Persisted to localStorage so
           the user's preference survives a refresh. Library has the same
           knob; both views read/write the same key (``workspace.viewMode``)
           so the user's choice carries between Knowledge Base and Workbench. -->
      <div class="view-toggle">
        <button
          class="view-btn"
          :class="{ 'view-btn--active': viewMode === 'grid' }"
          @click="setViewMode('grid')"
          :title="t('workspace.grid_view')"
        ><LayoutGrid :size="14" :stroke-width="1.5" /></button>
        <button
          class="view-btn"
          :class="{ 'view-btn--active': viewMode === 'list' }"
          @click="setViewMode('list')"
          :title="t('workspace.list_view')"
        ><List :size="14" :stroke-width="1.5" /></button>
      </div>
    </div>

    <div class="wb-body">
      <main class="wb-main" @contextmenu.prevent="onMainContextMenu">
        <div
          v-if="error"
          class="flex items-center gap-2 text-[11px] text-red-400 mx-4 my-3 px-3 py-2 border border-red-500/30 rounded bg-red-500/5"
        >
          <AlertCircle :size="14" :stroke-width="1.75" />
          <span>{{ t('workspace.load_error', { msg: error }) }}</span>
          <button
            class="ml-auto toolbar-btn"
            @click="load(currentPath)"
          >{{ t('common.retry') || 'Retry' }}</button>
        </div>

        <!-- Marquee + table/tiles. Capabilities here gate Stage-2 features
             (select / multi-select / context-menu); ``rename`` and
             ``dragMove`` stay off until Stage 3 lands the backend
             ``/workdir/rename`` + ``/workdir/move`` endpoints. -->
        <MarqueeSelection v-if="!error" @select="onMarqueeSelect">
          <FileTiles
            v-if="viewMode === 'grid'"
            :rows="filteredRows"
            :selection="selection"
            :loading="loading"
            :capabilities="capabilities"
            :renaming-key="renamingKey"
            @select="onSelect"
            @open-row="onOpenRow"
            @context-menu="onContextMenu"
            @drop-onto-folder="onDropOntoFolder"
            @confirm-rename="onConfirmRename"
            @cancel-rename="renamingKey = ''"
          >
            <template #empty>{{ t('workspace.empty_title') }}</template>
          </FileTiles>
          <FileTable
            v-else
            :rows="filteredRows"
            :selection="selection"
            :loading="loading"
            :capabilities="capabilities"
            :columns="['name', 'type', 'size', 'created', 'modified']"
            :renaming-key="renamingKey"
            @select="onSelect"
            @open-row="onOpenRow"
            @context-menu="onContextMenu"
            @drop-onto-folder="onDropOntoFolder"
            @confirm-rename="onConfirmRename"
            @cancel-rename="renamingKey = ''"
          >
            <template #empty>{{ t('workspace.empty_title') }}</template>
            <template #row-actions="{ row }">
              <div class="row-actions-inner">
                <button
                  v-if="row.kind === 'folder'"
                  class="row-action-btn"
                  :title="t('workspace.open_chat_here')"
                  @click.stop="onOpenChatRow(row)"
                >
                  <MessageSquare :size="12" :stroke-width="1.5" />
                </button>
                <a
                  v-else
                  class="row-action-btn"
                  :href="downloadUrl(row.path)"
                  :title="t('workspace.download')"
                  @click.stop
                >
                  <Download :size="12" :stroke-width="1.5" />
                </a>
              </div>
            </template>
          </FileTable>
        </MarqueeSelection>
      </main>
    </div>

    <ContextMenu
      :open="ctx.open"
      :x="ctx.x"
      :y="ctx.y"
      :items="ctxItems"
      @close="ctx.open = false"
      @action="onContextAction"
    />

    <FilePreview
      v-model:open="previewOpen"
      :path="previewPath"
      :filename="previewFilename"
      :preview-url="previewSrcUrl"
      :download-url="previewDownloadUrl"
    />
  </div>
</template>

<script setup>
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  AlertCircle,
  ClipboardCopy,
  Download,
  Eye,
  FolderOpen,
  FolderPlus,
  LayoutGrid,
  List,
  MessageSquare,
  PenLine,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from 'lucide-vue-next'

import {
  deleteWorkdirEntry,
  getWorkdirInfo,
  listWorkdir,
  makeWorkdirFolder,
  moveWorkdirEntry,
  renameWorkdirEntry,
  uploadWorkdirFile,
  workdirDownloadUrl,
  workdirPreviewUrl,
} from '@/api'
import { useLastTabRoute } from '@/composables/useLastTabRoute'
import Breadcrumb from '@/components/workspace/Breadcrumb.vue'
import FileTable from '@/components/files/FileTable.vue'
import FileTiles from '@/components/files/FileTiles.vue'
import MarqueeSelection from '@/components/files/MarqueeSelection.vue'
import ContextMenu from '@/components/files/ContextMenu.vue'
import FilePreview from '@/components/preview/FilePreview.vue'
import { useDialog } from '@/composables/useDialog'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const dialog = useDialog()

// ── Path + load state ────────────────────────────────────────────
const currentPath = ref(route.query.path || '/')
const entries = ref([])
const loading = ref(true)
const error = ref('')
const busy = ref(false)
const uploadInput = ref(null)

const crumbs = computed(() => {
  const out = [{ path: '/', name: t('workspace.root') }]
  if (currentPath.value && currentPath.value !== '/') {
    const parts = currentPath.value
      .replace(/^\/+|\/+$/g, '')
      .split('/')
    let acc = ''
    for (const p of parts) {
      acc += '/' + p
      out.push({ path: acc, name: p })
    }
  }
  return out
})

// ── View mode (list / grid) — persisted ──────────────────────────
// Library and Workbench share the same localStorage key so a user
// who flipped Library to grid sees Workbench in grid too. Either
// view falling back to a sensible default if the saved value is
// somehow invalid keeps the toggle working after a future change
// to the value space.
const _savedMode = localStorage.getItem('workspace.viewMode')
const viewMode = ref(['grid', 'list'].includes(_savedMode) ? _savedMode : 'list')
function setViewMode(mode) {
  if (!['grid', 'list'].includes(mode)) return
  viewMode.value = mode
  localStorage.setItem('workspace.viewMode', mode)
}

// ── Selection (Set of FileRow keys; keys are "fs:" + path) ───────
const selection = reactive(new Set())
function clearSelection() { selection.clear() }
function toggleSelect(key, additive) {
  if (!additive) {
    selection.clear()
    selection.add(key)
    return
  }
  if (selection.has(key)) selection.delete(key)
  else selection.add(key)
}

// ── Rows: fs entries → FileRow shape ─────────────────────────────
// Adapter that translates the workdir API's
// {path, name, is_dir, size_bytes, modified_at} into the neutral
// FileRow shape FileTable / FileTiles consume. The ``key`` uses
// the path as the stable identifier (paths are unique within a
// workdir at any given moment).
const rows = computed(() => entries.value.map(e => ({
  key: 'fs:' + e.path,
  kind: e.is_dir ? 'folder' : 'file',
  name: e.name,
  path: e.path,
  size: e.is_dir ? null : (e.size_bytes ?? null),
  createdAt: e.created_at ?? null,
  modifiedAt: e.modified_at ?? null,
  extras: e,
})))

// ── Search / filter ──────────────────────────────────────────────
// Client-side substring filter over the current folder's rows.
// Folders + files both match against name. Cleared automatically
// when the user navigates to a different folder so a stale query
// from "/sales" doesn't hide everything in "/legal".
const searchQuery = ref('')
const filteredRows = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return rows.value
  return rows.value.filter(r => (r.name || '').toLowerCase().includes(q))
})

// ── Capabilities the renderer should enable ───────────────────────
// All five turned on now that the backend ships ``/workdir/rename``,
// ``/workdir/move`` and ``DELETE /workdir/files``.
const capabilities = {
  select: true,
  multiSelect: true,
  rename: true,
  dragMove: true,
  contextMenu: true,
}

// ``row.key`` of the entry currently being inline-renamed. Owned here
// because we know when the user invoked rename via the context menu.
const renamingKey = ref('')

// ── Load ──────────────────────────────────────────────────────────
async function load(path) {
  loading.value = true
  error.value = ''
  try {
    await getWorkdirInfo()
    const list = await listWorkdir(path === '/' ? '' : path)
    entries.value = list
    currentPath.value = path
    clearSelection()
    // Clear the filter on folder change — a stale query from the
    // previous folder would hide entries the user clearly wants to see.
    searchQuery.value = ''
    if (route.query.path !== path && !(path === '/' && !route.query.path)) {
      router.replace({ path: route.path, query: path === '/' ? {} : { path } })
    }
  } catch (e) {
    error.value = e?.message || String(e)
    entries.value = []
  } finally {
    loading.value = false
  }
}

function open(path) { load(path) }

// ── Row interaction ─────────────────────────────────────────────
function onSelect({ key, additive }) { toggleSelect(key, additive) }

function onOpenRow(row) {
  if (row.kind === 'folder') {
    open(row.path)
    return
  }
  // Files: open the preview modal. The modal dispatches by
  // extension to image / video / audio / pdf / md / code /
  // spreadsheet / docx / html viewers.
  previewEntry.value = row.extras
  previewOpen.value = true
}

function onMarqueeSelect(keys) {
  // MarqueeSelection emits ``select`` with a plain array of keys it's
  // currently covering — see ``MarqueeSelection.vue:146``. Replace the
  // selection wholesale (the marquee gesture isn't additive on its own;
  // hold-to-extend would be a separate gesture wiring).
  selection.clear()
  for (const k of keys || []) selection.add(k)
}

// Click on the workbench background (anywhere outside a row) —
// clear selection. Same affordance the Library uses: clicking the
// canvas is the universal "deselect all" gesture.
function onWorkbenchClick(e) {
  if (busy.value) return
  const t = e.target
  if (!t || typeof t.closest !== 'function') return
  // Ignore clicks inside any actionable element — toolbar buttons,
  // table cells, tiles, the breadcrumb — those have their own
  // handlers and we don't want to fight them.
  if (t.closest('.list-row, .file-card, .wb-toolbar, .preview-modal, .ctx-menu')) return
  selection.clear()
}

// ── Context menu ────────────────────────────────────────────────
const ctx = reactive({ open: false, x: 0, y: 0, row: null })
function onContextMenu({ x, y, row }) {
  // Single-row right-click: replace selection with that row so
  // menu actions act on what was right-clicked, regardless of any
  // previous multi-selection.
  if (row) {
    selection.clear()
    selection.add(row.key)
  }
  ctx.row = row
  ctx.x = x
  ctx.y = y
  ctx.open = true
}
function onMainContextMenu(e) {
  ctx.row = null
  ctx.x = e.clientX
  ctx.y = e.clientY
  ctx.open = true
}

// Menu item shape: { label, action, icon?, shortcut?, danger?, disabled? }.
// ContextMenu emits ``action`` with the picked item's ``action`` field;
// we route below in onContextAction. Visual + grouping conventions
// match Library's menu (icons in a 14px column on the left, dividers
// between logical groups, the destructive action last and on its own).
// Move-to-distant-folder is via drag-drop only; a folder-picker
// dialog over the workdir tree would slot into the "Rename / Move"
// group when it lands.
const ctxItems = computed(() => {
  const r = ctx.row
  if (!r) {
    return [
      { label: t('workspace.new_folder'),         icon: FolderPlus,    action: 'new-folder' },
      { label: t('workspace.open_chat_here'),     icon: MessageSquare, action: 'open-chat-here' },
      { divider: true },
      { label: t('workspace.menu.refresh'),       icon: RefreshCw,     action: 'refresh' },
    ]
  }
  if (r.kind === 'folder') {
    return [
      { label: t('workspace.menu.open'),          icon: FolderOpen,    action: 'open' },
      { label: t('workspace.open_chat_here'),     icon: MessageSquare, action: 'open-chat' },
      { divider: true },
      { label: t('workspace.menu.new_subfolder'), icon: FolderPlus,    action: 'new-folder-here' },
      { divider: true },
      { label: t('workspace.menu.rename'),        icon: PenLine,       action: 'rename' },
      { label: t('workspace.menu.copy_path'),     icon: ClipboardCopy, action: 'copy-path' },
      { divider: true },
      { label: t('workspace.menu.delete'),        icon: Trash2,        action: 'delete', danger: true },
    ]
  }
  return [
    { label: t('workspace.menu.preview'),         icon: Eye,           action: 'preview' },
    { label: t('workspace.download'),             icon: Download,      action: 'download' },
    { divider: true },
    { label: t('workspace.menu.rename'),          icon: PenLine,       action: 'rename' },
    { label: t('workspace.menu.copy_path'),       icon: ClipboardCopy, action: 'copy-path' },
    { divider: true },
    { label: t('workspace.menu.delete'),          icon: Trash2,        action: 'delete', danger: true },
  ]
})

function onContextAction(actionId) {
  ctx.open = false
  const r = ctx.row
  // Empty-area actions (no row in scope).
  if (actionId === 'new-folder') { onMakeFolder(); return }
  if (actionId === 'open-chat-here') { onOpenChatCurrent(); return }
  if (actionId === 'refresh') { load(currentPath.value); return }
  if (!r) return
  // Row actions (file or folder).
  if (actionId === 'open') open(r.path)
  else if (actionId === 'open-chat') onOpenChatRow(r)
  else if (actionId === 'new-folder-here') onMakeFolderIn(r.path)
  else if (actionId === 'rename') renamingKey.value = r.key
  else if (actionId === 'delete') onDeleteRow(r)
  else if (actionId === 'preview') {
    previewEntry.value = r.extras
    previewOpen.value = true
  } else if (actionId === 'download') {
    window.open(downloadUrl(r.path), '_blank', 'noopener')
  } else if (actionId === 'copy-path') {
    copyToClipboard(r.path)
  }
}

// Inline-rename completion. The renderer captured the new value in
// the input field and emits it here; we POST and reload. ``oldName``
// equal to ``newName`` is silently dropped — Library does the same
// to avoid a no-op round-trip on every blur.
async function onConfirmRename({ key, oldName, newName }) {
  renamingKey.value = ''
  const trimmed = (newName || '').trim()
  if (!trimmed || trimmed === oldName) return
  const row = rows.value.find(r => r.key === key)
  if (!row) return
  busy.value = true
  try {
    await renameWorkdirEntry(row.path, trimmed)
    await load(currentPath.value)
  } catch (e) {
    dialog.alert({
      title: t('workspace.rename_failed'),
      description: e?.message || String(e),
    })
  } finally {
    busy.value = false
  }
}

async function onDeleteRow(row) {
  // Permanent delete — no soft-trash for the workdir yet, so the
  // confirm dialog has to spell out the irreversibility.
  const isDir = row.kind === 'folder'
  const ok = await dialog.confirm({
    title: isDir ? t('workspace.delete_folder_title') : t('workspace.delete_file_title'),
    description: isDir
      ? t('workspace.delete_folder_desc', { path: row.path })
      : t('workspace.delete_file_desc', { path: row.path }),
    confirmText: t('workspace.delete_confirm'),
    variant: 'destructive',
  })
  if (!ok) return
  busy.value = true
  try {
    await deleteWorkdirEntry(row.path)
    await load(currentPath.value)
  } catch (e) {
    dialog.alert({
      title: t('workspace.delete_failed'),
      description: e?.message || String(e),
    })
  } finally {
    busy.value = false
  }
}

// Drag-drop move. ``items`` is the renderer's normalized payload
// ({ key, kind, path, name }); ``targetPath`` is the destination
// folder. Multi-select drag is supported — we move each item in
// sequence and reload once at the end. A failure on any item shows
// a toast but doesn't roll back the previously-moved items
// (matches Library's drag-move semantics).
async function onDropOntoFolder({ items, targetPath }) {
  if (!items?.length) return
  // Self-drop guard mirrors what FileTable already does, but defend
  // here too in case the renderer changes.
  const filtered = items.filter(it => it.path !== targetPath)
  if (!filtered.length) return
  busy.value = true
  let firstError = null
  try {
    for (const it of filtered) {
      try {
        await moveWorkdirEntry(it.path, targetPath)
      } catch (e) {
        if (!firstError) firstError = e
      }
    }
    await load(currentPath.value)
  } finally {
    busy.value = false
  }
  if (firstError) {
    dialog.alert({
      title: t('workspace.move_failed'),
      description: firstError?.message || String(firstError),
    })
  }
}

// Async clipboard isn't available in non-secure contexts; fall back
// to a hidden ``<textarea>`` + ``execCommand('copy')`` so the action
// works on http:// dev servers too.
async function copyToClipboard(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return
    }
  } catch {
    // fall through to fallback
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.left = '-9999px'
  document.body.appendChild(ta)
  ta.select()
  try { document.execCommand('copy') } catch {}
  document.body.removeChild(ta)
}

// Create a subfolder inside ``parent`` (a folder path). Reuses the
// same dialog as the toolbar's New Folder so the flow is consistent.
async function onMakeFolderIn(parent) {
  if (busy.value) return
  const name = await dialog.prompt({
    title: t('workspace.new_folder_dialog.title'),
    description: t('workspace.new_folder_dialog.description'),
    placeholder: t('workspace.new_folder_dialog.placeholder'),
    confirmText: t('workspace.new_folder_dialog.confirm'),
  })
  if (!name) return
  if (name.includes('/') || name.includes('\\')) {
    dialog.alert({
      title: t('workspace.new_folder_error_title'),
      description: t('workspace.new_folder_error_separator'),
    })
    return
  }
  busy.value = true
  try {
    const target = parent.replace(/\/+$/, '') + '/' + name
    await makeWorkdirFolder(target)
    await load(currentPath.value)
  } catch (e) {
    dialog.alert({
      title: t('workspace.new_folder_error_title'),
      description: e?.message || String(e),
    })
  } finally {
    busy.value = false
  }
}

// ── Preview ─────────────────────────────────────────────────────
const previewEntry = ref(null)
const previewOpen = ref(false)
const previewPath = computed(() => previewEntry.value?.path || '')
const previewFilename = computed(() => previewEntry.value?.name || '')
const previewSrcUrl = computed(() =>
  previewPath.value ? workdirPreviewUrl(previewPath.value) : '',
)
const previewDownloadUrl = computed(() =>
  previewPath.value ? workdirDownloadUrl(previewPath.value) : '',
)

// ── Open chat with cwd bound to this folder ─────────────────────
function onOpenChatRow(row) {
  router.push({ path: '/chat', query: { cwd: row.path } })
}
function onOpenChatCurrent() {
  router.push({ path: '/chat', query: { cwd: currentPath.value } })
}

// ── Toolbar actions ─────────────────────────────────────────────
async function onMakeFolder() {
  if (busy.value) return
  const name = await dialog.prompt({
    title: t('workspace.new_folder_dialog.title'),
    description: t('workspace.new_folder_dialog.description'),
    placeholder: t('workspace.new_folder_dialog.placeholder'),
    confirmText: t('workspace.new_folder_dialog.confirm'),
  })
  if (!name) return
  if (name.includes('/') || name.includes('\\')) {
    dialog.alert({
      title: t('workspace.new_folder_error_title'),
      description: t('workspace.new_folder_error_separator'),
    })
    return
  }
  busy.value = true
  try {
    const target = currentPath.value === '/'
      ? '/' + name
      : currentPath.value.replace(/\/+$/, '') + '/' + name
    await makeWorkdirFolder(target)
    await load(currentPath.value)
  } catch (e) {
    dialog.alert({
      title: t('workspace.new_folder_error_title'),
      description: e?.message || String(e),
    })
  } finally {
    busy.value = false
  }
}

async function onUpload(event) {
  const file = event?.target?.files?.[0]
  if (!file) return
  busy.value = true
  try {
    await uploadWorkdirFile(currentPath.value, file)
    await load(currentPath.value)
  } catch (e) {
    dialog.alert({
      title: t('workspace.upload_error_title'),
      description: e?.message || String(e),
    })
  } finally {
    busy.value = false
    if (uploadInput.value) uploadInput.value.value = ''
  }
}

function downloadUrl(path) { return workdirDownloadUrl(path) }

// ── URL ↔ state sync ────────────────────────────────────────────
// Only react to ``route.query.path`` changes when this view is
// actually the active route. Without the guard, navigating to
// /library wipes ``route.query.path`` to undefined; the cached
// Workspace's watcher would fire and reload root — so on return
// the user briefly sees the workdir root before being snapped back
// to where they were. Mirror of Library's ``viewingTrash`` guard.
watch(
  () => [route.path, route.query.path],
  ([rp, p]) => {
    if (rp !== '/workspace') return
    p = p || '/'
    if (p !== currentPath.value) load(p)
  },
)

// Record where the user is so the sidebar's "Workspace" tab can
// bring them back here on the next click instead of dropping them
// at root. Watching fullPath so a path-only change (?path=/sales →
// ?path=/legal) updates the registry too.
const lastTabRoute = useLastTabRoute()
watch(
  () => route.fullPath,
  (fp) => {
    if (route.path === '/workspace') lastTabRoute.set('/workspace', fp)
  },
  { immediate: true },
)

// KeepAlive re-activation: returning to /workspace from another tab
// (or directly via the bare ``/workspace`` link in the sidebar). Push
// the cached ``currentPath`` back into the URL so the path watcher
// above sees a consistent (current === target) and stays put. Mirror
// of Library's ``onActivated`` hook.
onActivated(() => {
  const cur = currentPath.value
  const urlPath = route.query.path
  const desired = cur === '/' ? undefined : cur
  if (urlPath !== desired) {
    const q = { ...route.query }
    if (desired) q.path = desired
    else delete q.path
    router.replace({ path: route.path, query: q })
  }
})

onMounted(() => { load(currentPath.value) })
</script>

<style scoped>
.workbench {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  outline: none;
}
.wb-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
  min-height: 52px;
  flex-shrink: 0;
}
.wb-body {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
}
.wb-main {
  flex: 1 1 auto;
  min-width: 0;
  overflow: auto;
}

.toolbar-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  font-size: 11px;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.toolbar-btn:hover:not(:disabled):not(.is-disabled) {
  background: var(--color-bg2);
  color: var(--color-t1);
}
.toolbar-btn:disabled,
.toolbar-btn.is-disabled {
  opacity: 0.4;
  cursor: not-allowed;
  pointer-events: none;
}

.upload-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}

/* Search — same shape + metrics as Library's Toolbar.vue, just
   scoped here. Don't widen past 220px; the toolbar already carries
   crumb + 3 buttons + view toggle, and we want the input to be
   comfortable but not dominate the row. */
.search-wrap {
  position: relative;
  display: inline-flex;
  align-items: center;
  margin-left: 8px;
}
.search-icon {
  position: absolute;
  left: 8px;
  color: var(--color-t3);
  pointer-events: none;
}
.search-input {
  width: 220px;
  padding: 4px 24px 4px 26px;
  font-size: 11px;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 6px;
  outline: none;
}
.search-input:hover { border-color: var(--color-line2); }
.search-input:focus {
  border-color: var(--color-line2);
  box-shadow: var(--ring-focus);
}
.search-input::placeholder { color: var(--color-t3); }
.search-clear {
  position: absolute;
  right: 4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  padding: 0;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}
.search-clear:hover { background: var(--color-bg2); color: var(--color-t1); }

/* View toggle — same shape as Library's Toolbar.vue. Two icon
   buttons inside a thin border-bordered pill so the user reads
   them as a single grouped control. */
.view-toggle {
  display: inline-flex;
  align-items: center;
  gap: 0.5px;
  padding: 0.5px;
  margin-left: 4px;
  border: 1px solid var(--color-line);
  border-radius: 6px;
}
.view-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 22px;
  padding: 0;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.view-btn:hover { color: var(--color-t1); background: var(--color-bg2); }
.view-btn--active {
  color: var(--color-t1);
  background: var(--color-bg3);
}

/* Hover-revealed action icons in the table's right-most column.
   Mirror of Library's pattern (icons appear on row-hover). The
   parent table doesn't reveal these — the parent of FileTable
   wraps the actions slot and applies its own hover via the
   ``.list-row:hover .row-actions-inner`` rule below. */
.row-actions-inner {
  /* ``flex`` (not ``inline-flex``) so the cell's inline-baseline strut
     doesn't add descender space under the buttons — that's what
     pushed Workspace rows 2px taller than Library's. */
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.12s;
}
:deep(.list-row:hover) .row-actions-inner { opacity: 1; }
.row-action-btn {
  /* 16×16 to match FileIcon's 16px size — anything taller inflates
     the cell and pushes Workspace rows above Library's row height
     (Library has no actions column so its tallest cell content is
     the 16px FileIcon). The button stays clickable; hover state and
     icon are visible at this size. */
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 16px;
  padding: 0;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  text-decoration: none;
  transition: background 0.12s, color 0.12s;
}
.row-action-btn:hover {
  background: var(--color-bg2);
  color: var(--color-t1);
}
</style>
