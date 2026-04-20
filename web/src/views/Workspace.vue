<template>
  <div class="workspace" @keydown="onKeydown" tabindex="0">
    <!-- Top bar: breadcrumb + toolbar -->
    <div class="workspace__top">
      <Breadcrumb :crumbs="ws.breadcrumbs.value" @navigate="navigate" />
      <Toolbar
        :view-mode="ws.viewMode.value"
        :trash-count="trashCount"
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
          @navigate="navigate"
          @drop-into="onSidebarDrop"
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
          <MillerColumn
            v-if="ws.viewMode.value === 'miller'"
            :initial-path="ws.currentPath.value"
            @navigate="navigate"
            @open-folder="navigate"
            @open-document="onOpenDocument"
            @context-menu="openContextMenu"
          />
          <MarqueeSelection v-else @select="onMarqueeSelect">
            <FileGrid
              v-if="ws.viewMode.value === 'grid'"
              :folders="ws.childFolders.value"
              :documents="ws.childDocuments.value"
              :selection="ws.selection"
              @select="({ key, additive }) => ws.toggleSelect(key, { additive })"
              @open-folder="navigate"
              @open-document="onOpenDocument"
              @context-menu="openContextMenu"
              @drop-onto-folder="onDropOntoFolder"
            />
            <FileList
              v-else
              :folders="ws.childFolders.value"
              :documents="ws.childDocuments.value"
              :selection="ws.selection"
              @select="({ key, additive }) => ws.toggleSelect(key, { additive })"
              @open-folder="navigate"
              @open-document="onOpenDocument"
              @context-menu="openContextMenu"
            />
          </MarqueeSelection>
        </template>
      </main>
    </div>

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
import { useRouter } from 'vue-router'
import { getTrashStats, uploadAndIngest } from '@/api'
import { useWorkspace } from '@/composables/useWorkspace'
import Breadcrumb from '@/components/workspace/Breadcrumb.vue'
import Toolbar from '@/components/workspace/Toolbar.vue'
import FolderTree from '@/components/workspace/FolderTree.vue'
import FileGrid from '@/components/workspace/FileGrid.vue'
import FileList from '@/components/workspace/FileList.vue'
import MillerColumn from '@/components/workspace/MillerColumn.vue'
import ContextMenu from '@/components/workspace/ContextMenu.vue'
import MarqueeSelection from '@/components/workspace/MarqueeSelection.vue'
import TrashView from '@/components/workspace/TrashView.vue'

const router = useRouter()
const ws = useWorkspace()

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
  if (action === 'rename' && item?.type === 'folder') return onRenameFolder(item)
  if (action === 'move')   return onMoveDialog(item)
  if (action === 'delete') return onDelete(item)
}

// ── Core actions ──────────────────────────────────────────────────

async function navigate(path) {
  viewingTrash.value = false
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

async function onNewFolder() {
  const name = window.prompt('New folder name:')
  if (!name) return
  try {
    await ws.opCreateFolder(ws.currentPath.value, name.trim())
  } catch (e) {
    alert('Create failed: ' + e.message)
  }
}

function onUpload() {
  fileInput.value?.click()
}

async function onFilesPicked(e) {
  const files = [...(e.target.files || [])]
  if (!files.length) return
  for (const f of files) {
    try {
      await uploadAndIngest(f)
    } catch (err) {
      console.error('upload failed:', err)
    }
  }
  e.target.value = ''
  await refresh()
}

async function onRenameFolder(item) {
  const newName = window.prompt('Rename folder to:', item.name)
  if (!newName || newName === item.name) return
  try {
    await ws.opRenameFolder(item.path, newName.trim())
  } catch (e) {
    alert('Rename failed: ' + e.message)
  }
}

async function onMoveDialog(item) {
  const target = window.prompt(`Move "${item.name}" to which folder path?`, ws.currentPath.value)
  if (!target) return
  try {
    if (item.type === 'folder') await ws.opMoveFolder(item.path, target)
    else await ws.opMoveDocument(item.doc_id, target)
  } catch (e) {
    alert('Move failed: ' + e.message)
  }
}

async function onDelete(item) {
  const name = item.name
  if (!confirm(`Move "${name}" to the recycle bin?`)) return
  try {
    if (item.type === 'folder') await ws.opDeleteFolder(item.path)
    else {
      // Documents use the existing DELETE /documents endpoint which does hard-delete.
      // For Phase 1, we don't wire soft-delete for individual docs (folders cover
      // most real-world use). Show a clearer message.
      if (!confirm('Deleting a single document is permanent (no recycle bin yet). Continue?')) return
      const { request } = await import('@/api/client')
      await request(`/api/v1/documents/${item.doc_id}`, { method: 'DELETE' })
      await refresh()
    }
  } catch (e) {
    alert('Delete failed: ' + e.message)
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
    alert('Paste failed: ' + e.message)
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
    alert('Move failed: ' + e.message)
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
  // For Phase 1: open the document's repository view (existing UI),
  // or navigate to /repository?doc=X. Simplest: open PDF viewer route.
  router.push(`/repository?doc=${doc.doc_id}`)
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
    if (item?.type === 'folder') onRenameFolder(item)
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
    else if (mod && e.key === '3') { ws.setViewMode('miller') }
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
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  outline: none;
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
  background: var(--color-bg);
}
.workspace__main {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.hidden { display: none; }
</style>
