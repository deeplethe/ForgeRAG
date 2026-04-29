<template>
  <div
    class="workspace"
    :class="{ 'workspace--drag-over': osDragActive }"
    @keydown="onKeydown"
    @click="onWorkspaceClick"
    @dragenter.prevent="onOSDragEnter"
    @dragover.prevent="onOSDragOver"
    @dragleave="onOSDragLeave"
    @drop.prevent="onOSDrop"
    tabindex="0"
  >
    <!-- Drag-over overlay — full viewport drop zone hint (only visible when
         the user is dragging FILES from the OS, not internal folder/doc
         drags which already have their own per-folder drop targets). -->
    <div v-if="osDragActive" class="workspace__drop-overlay">
      <div class="drop-pill">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
        </svg>
        <span>Drop files to upload to <code>{{ ws.currentPath.value }}</code></span>
      </div>
    </div>
    <!-- Doc detail overlay (PDF + tree + chunks) — takes over when ?doc=X -->
    <div v-if="focusedDocId" class="workspace__doc-detail">
      <Repository
        :inline="true"
        :initial-doc-id="focusedDocId"
        @close="onDocDetailClose"
      />
    </div>

    <!-- Browser mode (default) -->
    <template v-else>
    <!-- Top bar: breadcrumb + toolbar -->
    <div class="workspace__top">
      <Breadcrumb :crumbs="ws.breadcrumbs.value" @navigate="navigate" />
      <Toolbar
        :view-mode="ws.viewMode.value"
        :trash-count="trashCount"
        v-model:search="searchQuery"
        @new-folder="onNewFolder"
        @upload="onUpload"
        @set-view="ws.setViewMode"
        @show-trash="viewingTrash = true"
      />
    </div>

    <!-- Two-pane body -->
    <div class="workspace__body">
      <!-- Sidebar tree -->
      <aside class="workspace__sidebar">
        <FolderTree
          :root="ws.tree.value"
          :current-path="ws.currentPath.value"
          :loading="ws.treeLoading.value"
          :error="ws.treeError.value"
          @navigate="navigate"
          @drop-into="onSidebarDrop"
          @retry="ws.loadTree()"
        />
      </aside>

      <!-- Main content area -->
      <main
        class="workspace__main"
        @contextmenu.prevent="onMainContextMenu"
      >
        <TrashView
          v-if="viewingTrash"
          @back="onExitTrash"
          @changed="refresh"
        />

        <template v-else>
          <MarqueeSelection @select="onMarqueeSelect">
            <FileGrid
              v-if="ws.viewMode.value === 'grid'"
              :folders="filteredFolders"
              :documents="filteredDocuments"
              :loading="ws.contentsLoading.value"
              :selection="ws.selection"
              :creating="creatingFolder"
              :renaming-key="renamingKey"
              @select="({ key, additive }) => ws.toggleSelect(key, { additive })"
              @open-folder="navigate"
              @open-document="onOpenDocument"
              @context-menu="openContextMenu"
              @drop-onto-folder="onDropOntoFolder"
              @confirm-create="onCreateFolderInline"
              @cancel-create="creatingFolder = false"
              @confirm-rename="onConfirmRename"
              @cancel-rename="renamingKey = ''"
            />
            <FileList
              v-else
              :folders="filteredFolders"
              :documents="filteredDocuments"
              :loading="ws.contentsLoading.value"
              :selection="ws.selection"
              :creating="creatingFolder"
              :renaming-key="renamingKey"
              @select="({ key, additive }) => ws.toggleSelect(key, { additive })"
              @open-folder="navigate"
              @open-document="onOpenDocument"
              @context-menu="openContextMenu"
              @confirm-create="onCreateFolderInline"
              @cancel-create="creatingFolder = false"
              @confirm-rename="onConfirmRename"
              @cancel-rename="renamingKey = ''"
            />
          </MarqueeSelection>
        </template>
      </main>
    </div>
    </template>

    <!-- Context menu -->
    <ContextMenu
      :open="ctx.open"
      :x="ctx.x"
      :y="ctx.y"
      :items="ctxItems"
      @close="ctx.open = false"
      @action="onContextAction"
    />

    <!-- Hidden file input for uploads -->
    <input
      ref="fileInput"
      type="file"
      multiple
      class="hidden"
      @change="onFilesPicked"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { getTrashStats } from '@/api'
import { useWorkspace } from '@/composables/useWorkspace'
import { useUploadsStore } from '@/stores/uploads'
import { useDialog } from '@/composables/useDialog'
import Repository from '@/views/Repository.vue'
import Breadcrumb from '@/components/workspace/Breadcrumb.vue'
import Toolbar from '@/components/workspace/Toolbar.vue'
import FolderTree from '@/components/workspace/FolderTree.vue'
import FileGrid from '@/components/workspace/FileGrid.vue'
import FileList from '@/components/workspace/FileList.vue'
import ContextMenu from '@/components/workspace/ContextMenu.vue'
import MarqueeSelection from '@/components/workspace/MarqueeSelection.vue'
import TrashView from '@/components/workspace/TrashView.vue'

const router = useRouter()
const route = useRoute()
const ws = useWorkspace()
const { confirm, toast } = useDialog()

// ── OS drag-and-drop file upload ─────────────────────────────────
// Counter pattern: dragenter/leave fire for every child element nested
// inside the workspace. Naive listening drops the overlay every time
// the cursor crosses an interior border. We count pos/neg events.
const osDragActive = ref(false)
let _dragCounter = 0

function isOSFileDrag(e) {
  // dataTransfer.types is a TokenList-ish — has 'Files' for OS drags,
  // 'application/x-forgerag-item' for our internal folder/doc drags.
  const types = e.dataTransfer?.types
  if (!types) return false
  return Array.from(types).includes('Files')
}

function onOSDragEnter(e) {
  if (!isOSFileDrag(e)) return
  _dragCounter++
  osDragActive.value = true
}

function onOSDragOver(e) {
  if (!isOSFileDrag(e)) return
  e.dataTransfer.dropEffect = 'copy'
}

function onOSDragLeave(e) {
  if (!isOSFileDrag(e)) return
  _dragCounter--
  if (_dragCounter <= 0) {
    _dragCounter = 0
    osDragActive.value = false
  }
}

function onOSDrop(e) {
  if (!isOSFileDrag(e)) return
  _dragCounter = 0
  osDragActive.value = false
  const files = Array.from(e.dataTransfer.files || [])
  if (!files.length) return
  // Hand off to the global upload queue (same path as the toolbar Upload btn)
  uploads.enqueue(files, { folderPath: ws.currentPath.value })
  uploads.toggleDrawer(true)
  // Refresh the file list shortly after so newly-ingested docs surface
  setTimeout(() => { refresh() }, 800)
}

// ── Click-empty-area to clear selection ───────────────────────────
// Cards/items use @click.stop so their clicks never reach this handler.
// Anything else (workspace background, file-grid empty space) clears.
function onWorkspaceClick(e) {
  // Skip if clicking inside the toolbar / breadcrumb / sidebar — only
  // clear when clicking in the file-list area or the surrounding shell.
  const t = e.target
  if (t.closest('.workspace__top') || t.closest('.workspace__sidebar')) return
  // Skip if a context menu was just dismissed via this click (handled elsewhere)
  if (ctx.open) return
  if (ws.selection.size > 0) ws.clearSelection()
}

// ── Toolbar search + inline new-folder + inline-rename state ─────
const searchQuery = ref('')
const creatingFolder = ref(false)
// Selection-key of the folder currently being renamed ("f:abc-123"),
// or '' when no rename is in progress. Mirrors the inline-create
// pattern; FileGrid/FileList render the editable input when the key
// matches one of their rows.
const renamingKey = ref('')

// Case-insensitive filter on folder name + document filename. Fallback to
// the full list when search is empty so we avoid re-allocating arrays.
const filteredFolders = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return ws.childFolders.value
  return ws.childFolders.value.filter((f) => (f.name || '').toLowerCase().includes(q))
})
const filteredDocuments = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return ws.childDocuments.value
  return ws.childDocuments.value.filter((d) =>
    (d.filename || d.file_name || '').toLowerCase().includes(q),
  )
})
const uploads = useUploadsStore()

// URL-driven doc detail. Clicking a document in the browser sets ?doc=<id>
// which swaps the main area for the embedded Repository view (PDF + tree +
// chunks). Clearing the query returns to the browser.
const focusedDocId = computed(() => {
  const d = route.query.doc
  return typeof d === 'string' && d ? d : ''
})

function onDocDetailClose() {
  const q = { ...route.query }
  // Clear the doc + its side-state (tab, pipeline, node, chunk, pdf)
  delete q.doc; delete q.pipeline; delete q.node; delete q.chunk; delete q.pdf
  router.replace({ path: route.path, query: q })
}

// ── Page state ────────────────────────────────────────────────────
const viewingTrash = ref(false)
const trashCount = ref(0)
const fileInput = ref(null)

// ── Context menu ──────────────────────────────────────────────────
const ctx = reactive({ open: false, x: 0, y: 0, item: null })
const ctxItems = computed(() => buildContextItems(ctx.item))

function openContextMenu({ x, y, item }) {
  ctx.open = true
  ctx.x = x
  ctx.y = y
  ctx.item = item
}

function onMainContextMenu(e) {
  // Fallback: right-click on main area opens menu with background items
  openContextMenu({ x: e.clientX, y: e.clientY, item: null })
}

function buildContextItems(item) {
  if (!item) {
    // Background items
    return [
      { label: 'New folder',    icon: '⊕', shortcut: 'Ctrl+N', action: 'new-folder' },
      { label: 'Upload file',   icon: '⬆',                     action: 'upload' },
      { divider: true },
      {
        label: 'Paste',
        icon: '📥',
        shortcut: 'Ctrl+V',
        action: 'paste',
        disabled: !ws.hasClipboard.value,
      },
      { divider: true },
      { label: 'Refresh', icon: '↻', action: 'refresh' },
    ]
  }
  if (item.type === 'folder') {
    return [
      { label: 'Open',           icon: '📂', action: 'open' },
      { label: 'Search inside',  icon: '🔍', action: 'scope-chat' },
      { divider: true },
      { label: 'Cut',            icon: '✂', shortcut: 'Ctrl+X', action: 'cut' },
      { label: 'Copy',           icon: '📋', shortcut: 'Ctrl+C', action: 'copy' },
      { divider: true },
      { label: 'Rename',         icon: '✏', shortcut: 'F2', action: 'rename' },
      { label: 'Move to…',       icon: '➡',                   action: 'move' },
      { divider: true },
      { label: 'Delete',         icon: '🗑', shortcut: 'Del', action: 'delete', danger: true },
    ]
  }
  // document
  return [
    { label: 'Preview',         icon: '👁', action: 'preview' },
    { label: 'Ask in Chat',     icon: '💬', action: 'scope-chat' },
    { divider: true },
    { label: 'Cut',             icon: '✂', shortcut: 'Ctrl+X', action: 'cut' },
    { label: 'Copy',            icon: '📋', shortcut: 'Ctrl+C', action: 'copy' },
    { divider: true },
    { label: 'Rename',          icon: '✏', shortcut: 'F2', action: 'rename' },
    { label: 'Move to…',        icon: '➡',                   action: 'move' },
    { divider: true },
    { label: 'Delete',          icon: '🗑', shortcut: 'Del', action: 'delete', danger: true },
  ]
}

async function onContextAction(action) {
  const item = ctx.item
  if (action === 'new-folder')  return onNewFolder()
  if (action === 'upload')      return onUpload()
  if (action === 'refresh')     return refresh()
  if (action === 'paste')       return onPaste()
  if (action === 'open' && item?.type === 'folder')      return navigate(item.path)
  if (action === 'scope-chat' && item?.type === 'folder')  return scopeChatTo(item.path)
  if (action === 'scope-chat' && item?.type === 'document') return scopeChatTo(item.path)
  if (action === 'cut')    return onCut(item)
  if (action === 'copy')   return onCopy(item)
  if (action === 'rename') return onRename(item)
  if (action === 'move')   return onMoveDialog(item)
  if (action === 'delete') return onDelete(item)
}

// ── Core actions ──────────────────────────────────────────────────

async function navigate(path) {
  viewingTrash.value = false
  // Drop any pending inline-rename / inline-create state — the folder
  // being edited may not exist in the new path's list.
  renamingKey.value = ''
  creatingFolder.value = false
  await ws.navigate(path)
}

async function refresh() {
  await Promise.all([ws.loadTree(), ws.loadContents(), refreshTrashCount()])
}

async function refreshTrashCount() {
  try {
    const r = await getTrashStats()
    trashCount.value = (r?.items || 0) + (r?.top_level_folders || 0)
  } catch { trashCount.value = 0 }
}

function onExitTrash() {
  viewingTrash.value = false
  refresh()
}

function onNewFolder() {
  // Windows-style: surface an inline editable folder row in the current
  // view, autofocus its name input. The actual create happens once the
  // user confirms (Enter / blur with text).
  creatingFolder.value = true
}

async function onCreateFolderInline(rawName) {
  const name = (rawName || '').trim()
  // Always exit the creating mode — even on empty input, otherwise the
  // ghost row sticks around.
  creatingFolder.value = false
  if (!name) return
  try {
    await ws.opCreateFolder(ws.currentPath.value, name)
  } catch (e) {
    toast('Create failed: ' + e.message, { variant: 'error' })
  }
}

function onUpload() {
  fileInput.value?.click()
}

function onFilesPicked(e) {
  const files = [...(e.target.files || [])]
  if (!files.length) return
  const folderPath = ws.currentPath.value
  // Hand files off to the global upload queue — it handles upload + ingestion
  // polling + progress UI. Open the drawer so the user sees activity start.
  uploads.enqueue(files, { folderPath })
  uploads.toggleDrawer(true)
  e.target.value = ''
  // Kick a workspace refresh after a short delay so new docs appear in the
  // file tree once they hit the DB. The queue keeps updating independently.
  setTimeout(() => { refresh() }, 800)
}

function onRename(item) {
  // Surface an inline editable name input on the matching card/row
  // (Windows-style F2 rename). Works for both folders and documents.
  // Actual rename happens once the user confirms via Enter / blur — see
  // onConfirmRename.
  if (!item) return
  if (item.type === 'folder') renamingKey.value = 'f:' + item.folder_id
  else if (item.type === 'document') renamingKey.value = 'd:' + item.doc_id
}

async function onConfirmRename({ oldName, newName }) {
  const key = renamingKey.value
  // Always exit rename mode, even when the user submits an empty / unchanged
  // name (otherwise the input sticks around after a blur).
  renamingKey.value = ''
  const trimmed = (newName || '').trim()
  if (!trimmed || trimmed === oldName) return
  // Resolve from the captured key — by the time this fires the selection
  // or list could have shifted, so we look up by id, not item ref.
  if (key.startsWith('f:')) {
    const folder = ws.childFolders.value.find(f => f.folder_id === key.slice(2))
    if (!folder) return
    try {
      await ws.opRenameFolder(folder.path, trimmed)
    } catch (e) {
      toast('Rename failed: ' + e.message, { variant: 'error' })
    }
  } else if (key.startsWith('d:')) {
    const doc = ws.childDocuments.value.find(d => d.doc_id === key.slice(2))
    if (!doc) return
    try {
      await ws.opRenameDocument(doc.doc_id, trimmed)
    } catch (e) {
      toast('Rename failed: ' + e.message, { variant: 'error' })
    }
  }
}

async function onMoveDialog(item) {
  const target = window.prompt(`Move "${item.name}" to which folder path?`, ws.currentPath.value)
  if (!target) return
  try {
    if (item.type === 'folder') await ws.opMoveFolder(item.path, target)
    else await ws.opMoveDocument(item.doc_id, target)
  } catch (e) {
    toast('Move failed: ' + e.message, { variant: 'error' })
  }
}

async function onDelete(item) {
  const name = item.name
  if (item.type === 'folder') {
    const ok = await confirm({
      title: `Move "${name}" to recycle bin?`,
      description: 'Items in the recycle bin can be restored later.',
      confirmText: 'Move to bin',
    })
    if (!ok) return
    try { await ws.opDeleteFolder(item.path) }
    catch (e) { toast('Delete failed: ' + e.message, { variant: 'error' }) }
    return
  }
  // Documents: hard-delete (no soft-delete for individual docs yet).
  const ok = await confirm({
    title: `Permanently delete "${name}"?`,
    description: 'This document will be deleted with no recycle bin. Cannot be undone.',
    confirmText: 'Delete forever',
    variant: 'destructive',
  })
  if (!ok) return
  try {
    const { request } = await import('@/api/client')
    await request(`/api/v1/documents/${item.doc_id}`, { method: 'DELETE' })
    await refresh()
  } catch (e) {
    toast('Delete failed: ' + e.message, { variant: 'error' })
  }
}

// ── Clipboard cut/copy/paste ──────────────────────────────────────

function buildClipboardItems(item) {
  const selected = [...ws.selection]
  if (selected.length > 1 && selected.some(k => k === (item?.type === 'folder' ? 'f:' + item.folder_id : 'd:' + item.doc_id))) {
    return selected.map(k => toItem(k))
  }
  if (item) return [item]
  return []
}
function toItem(key) {
  if (key.startsWith('f:')) {
    const fid = key.slice(2)
    const f = ws.childFolders.value.find(x => x.folder_id === fid)
    return f ? { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name } : null
  }
  const did = key.slice(2)
  const d = ws.childDocuments.value.find(x => x.doc_id === did)
  return d ? { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name } : null
}

function onCut(item) {
  const items = buildClipboardItems(item).filter(Boolean)
  if (!items.length) return
  ws.setClipboard('cut', items, ws.currentPath.value)
}
function onCopy(item) {
  // Copy isn't supported for documents yet (no backend duplicate API) —
  // only folders, via sequential create + move. For Phase 1 we accept
  // "copy" being a no-op alias for cut on documents and gracefully do
  // nothing for folder duplication.
  onCut(item)
}

async function onPaste() {
  const items = ws.clipboard.items
  if (!items.length) return
  const target = ws.currentPath.value
  try {
    const docIds = items.filter(i => i.type === 'document').map(i => i.doc_id)
    if (docIds.length) await ws.opBulkMoveDocuments(docIds, target)
    for (const i of items) {
      if (i.type === 'folder') await ws.opMoveFolder(i.path, target)
    }
    ws.clearClipboard()
  } catch (e) {
    toast('Paste failed: ' + e.message, { variant: 'error' })
  }
}

// ── Drag/drop ─────────────────────────────────────────────────────

function onDropOntoFolder({ items, targetPath }) { doDropMove(items, targetPath) }
function onSidebarDrop({ items, targetPath }) { doDropMove(items, targetPath) }

async function doDropMove(items, targetPath) {
  try {
    const docIds = items.filter(i => i.type === 'document').map(i => i.doc_id)
    if (docIds.length) await ws.opBulkMoveDocuments(docIds, targetPath)
    for (const i of items) {
      if (i.type === 'folder') await ws.opMoveFolder(i.path, targetPath)
    }
  } catch (e) {
    toast('Move failed: ' + e.message, { variant: 'error' })
  }
}

// ── Marquee → selection ──────────────────────────────────────────
function onMarqueeSelect(keys) { ws.selectAll(keys) }

// ── Chat scoping ──────────────────────────────────────────────────
function scopeChatTo(path) {
  const url = `/chat?path_filter=${encodeURIComponent(path)}`
  router.push(url)
}

function onOpenDocument(doc) {
  router.push({ path: '/workspace', query: { doc: doc.doc_id } })
}

// ── Keyboard shortcuts ─────────────────────────────────────────────

function onKeydown(e) {
  if (viewingTrash.value) return
  const mod = e.ctrlKey || e.metaKey
  if (e.key === 'Delete') {
    const item = firstSelectedItem()
    if (item) onDelete(item)
  } else if (e.key === 'F2') {
    const item = firstSelectedItem()
    if (item) onRename(item)
  } else if (mod && e.key.toLowerCase() === 'n') {
    e.preventDefault(); onNewFolder()
  } else if (mod && e.key.toLowerCase() === 'x') {
    const item = firstSelectedItem(); if (item) onCut(item)
  } else if (mod && e.key.toLowerCase() === 'c') {
    const item = firstSelectedItem(); if (item) onCopy(item)
  } else if (mod && e.key.toLowerCase() === 'v') {
    onPaste()
  } else if (mod && e.key === '1') { ws.setViewMode('grid') }
    else if (mod && e.key === '2') { ws.setViewMode('list') }
  else if (mod && e.key.toLowerCase() === 'a') {
    e.preventDefault()
    const all = [
      ...ws.childFolders.value.map(f => 'f:' + f.folder_id),
      ...ws.childDocuments.value.map(d => 'd:' + d.doc_id),
    ]
    ws.selectAll(all)
  } else if (e.key === 'Escape') {
    ws.clearSelection()
  } else if (e.key === 'Backspace') {
    // Go up
    const parent = ws.breadcrumbs.value.at(-2)
    if (parent) navigate(parent.path)
  }
}

function firstSelectedItem() {
  const key = [...ws.selection][0]
  if (!key) return ctx.item
  return toItem(key)
}

// ── Lifecycle ─────────────────────────────────────────────────────
onMounted(async () => {
  await ws.loadTree()
  await ws.loadContents('/')
  await refreshTrashCount()
})
</script>

<style scoped>
.workspace {
  position: relative;          /* anchor for the drop overlay */
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  outline: none;
}

/* Whole-workspace drop hint when user drags FILES from the OS */
.workspace__drop-overlay {
  position: absolute;
  inset: 0;
  z-index: 40;
  pointer-events: none;        /* events still reach drop targets below */
  display: flex;
  align-items: center;
  justify-content: center;
  background: color-mix(in srgb, var(--color-bg2) 70%, transparent);
  backdrop-filter: blur(2px);
  border: 2px dashed var(--color-line2);
  border-radius: var(--r-md);
}
.drop-pill {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 12px 18px;
  font-size: 12px;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
}
.drop-pill code {
  font-size: 11px;
  color: var(--color-t2);
  padding: 1px 5px;
  background: var(--color-bg2);
  border-radius: var(--r-sm);
}
/* Slight tinted-border pulse when actively over the area */
.workspace--drag-over .workspace__drop-overlay {
  border-color: var(--color-t2);
}
.workspace__top {
  display: flex;
  flex-direction: column;
  border-bottom: 1px solid var(--color-line);
  padding: 8px 16px 0;
  gap: 6px;
  flex-shrink: 0;
}
.workspace__body {
  display: flex;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
.workspace__sidebar {
  width: 240px;
  flex-shrink: 0;
  border-right: 1px solid var(--color-line);
  overflow-y: auto;
  background: var(--color-bg2);   /* canvas — matches outer body */
}
.workspace__main {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.hidden { display: none; }

/* Inline doc detail — takes the full Workspace area */
.workspace__doc-detail {
  flex: 1;
  min-height: 0;
  display: flex;
}
.workspace__doc-detail > :deep(*) {
  flex: 1;
  min-width: 0;
  min-height: 0;
}
</style>
