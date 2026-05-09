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
    <!-- Doc detail overlay — takes over when ?doc=X. 3-col layout
         (Tree / PDF / KG-mini + Chunks) lives in DocDetail.vue.
         /repository legacy URL redirects here via router. -->
    <div v-if="focusedDocId" class="workspace__doc-detail">
      <DocDetail
        :doc-id="focusedDocId"
        @close="onDocDetailClose"
      />
    </div>

    <!-- Browser mode (default) -->
    <template v-else>
    <!-- Single-row top bar: breadcrumb on the left, actions cluster
         (New / Upload / search / view-toggle / trash) on the right. -->
    <Toolbar
      class="workspace__top"
      :view-mode="ws.viewMode.value"
      :trash-count="trashCount"
      :viewing-trash="viewingTrash"
      :emptying-trash="emptyingTrash"
      v-model:search="searchQuery"
      @new-folder="onNewFolder"
      @upload="onUpload"
      @set-view="ws.setViewMode"
      @show-trash="onShowTrash"
      @empty-trash="onEmptyTrash"
      @exit-trash="onExitTrash"
    >
      <template #lead>
        <Breadcrumb
          :crumbs="viewingTrash ? trashCrumbs : ws.breadcrumbs.value"
          @navigate="onCrumbNavigate"
        />
      </template>
    </Toolbar>

    <!-- Body — single full-width pane. The sidebar tree was retired
         (was dead weight for single-Space users — one entry — and
         covered by Toolbar breadcrumb + grid drill-down for the
         multi-Space case). Move-to-other-folder lives in the
         right-click menu via FolderPickerDialog. -->
    <div class="workspace__body">
      <main
        class="workspace__main"
        @contextmenu.prevent="onMainContextMenu"
      >
        <TrashView
          v-if="viewingTrash"
          :items="trashItems"
          :loading="trashLoading"
          @restore="onRestoreItem"
          @purge="onPurgeItem"
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
              @drop-onto-folder="onDropOntoFolder"
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

    <!-- Move-to picker (replaces the old window.prompt path input) -->
    <FolderPickerDialog
      v-model:open="movePickerOpen"
      :title="movePickerTitle"
      :initial-path="ws.currentPath.value"
      :exclude-paths="movePickerExclude"
      confirm-text="Move here"
      @select="onMoveTargetPicked"
    />

    <!-- Folder Members dialog — opened from the right-click menu -->
    <FolderMembersDialog
      v-model:open="membersDialogOpen"
      :folder-id="membersDialogFolderId"
      :folder-label="membersDialogFolderLabel"
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
import { computed, onActivated, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { emptyTrash, getTrashStats, listTrash, purgeTrashItems, restoreFromTrash } from '@/api'
import { useLibrary } from '@/composables/useLibrary'
import { useCapabilitiesStore } from '@/stores/capabilities'
import { useUploadsStore } from '@/stores/uploads'
import { useDialog } from '@/composables/useDialog'
import DocDetail from '@/views/DocDetail.vue'
import Breadcrumb from '@/components/workspace/Breadcrumb.vue'
import Toolbar from '@/components/workspace/Toolbar.vue'
import FileGrid from '@/components/workspace/FileGrid.vue'
import FileList from '@/components/workspace/FileList.vue'
import ContextMenu from '@/components/workspace/ContextMenu.vue'
import MarqueeSelection from '@/components/workspace/MarqueeSelection.vue'
import TrashView from '@/components/workspace/TrashView.vue'
import FolderPickerDialog from '@/components/FolderPickerDialog.vue'
import FolderMembersDialog from '@/components/FolderMembersDialog.vue'
// Context-menu action icons — trial run of Lucide for Vercel/Geist feel.
// Lucide's stroke geometry is closer to Geist (thin, geometric, sharp
// corners) than Heroicons solid. ContextMenu passes ``stroke-width=1.5``
// and ``size=14`` at the use site so the line weight reads cleanly at
// the 14px icon column. If we keep this we'll roll Lucide out across
// toast / picker / toolbar; otherwise we can revert this file alone.
import {
  ArrowRightFromLine,
  ClipboardPaste,
  Copy,
  Eye,
  FolderOpen,
  FolderPlus,
  MessageSquare,
  PenLine,
  RefreshCw,
  Scissors,
  Search,
  Trash2,
  Upload,
  UserPlus,
} from 'lucide-vue-next'

const router = useRouter()
const route = useRoute()
const ws = useLibrary()
const { confirm, toast, dismissToast } = useDialog()

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
  // Capability-aware enqueue — images / legacy Office formats get
  // toasted + dropped here so the user knows immediately. Backend
  // 415 is still the source of truth.
  const accepted = safeEnqueue(files, { folderPath: ws.currentPath.value })
  if (accepted) uploads.toggleDrawer(true)
  // Refresh the file list shortly after so newly-ingested docs surface
  setTimeout(() => { refresh() }, 800)
}

// ── Click-empty-area to clear selection ───────────────────────────
// Cards/items use @click.stop so their clicks never reach this handler.
// Anything else (workspace background, file-grid empty space) clears.
function onWorkspaceClick(e) {
  // Skip if clicking inside the toolbar / breadcrumb — only clear
  // when clicking in the file-list area or the surrounding shell.
  const t = e.target
  if (t.closest('.workspace__top')) return
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
const capabilities = useCapabilitiesStore()

// Filter incoming files against the server's capability flags before
// handing them to the upload queue. Files we know the backend will
// reject (image when no VLM, .doc/.ppt/.xls always) get a toast +
// drop here, so the user gets immediate feedback instead of waiting
// for a 415 a few hundred ms later. Backend still enforces — this is
// just a UX-side safety net.
function safeEnqueue(files, opts) {
  const arr = Array.isArray(files) ? files : Array.from(files || [])
  if (!arr.length) return 0
  const accepted = []
  for (const f of arr) {
    const verdict = capabilities.classify(f)
    if (verdict.ok) {
      accepted.push(f)
      continue
    }
    if (verdict.reason === 'legacy_office') {
      toast(
        `${f.name} — legacy ${verdict.ext} format not supported. Save as ${verdict.suggested} and try again.`,
        { variant: 'error', duration: 7000 },
      )
    } else if (verdict.reason === 'image_disabled') {
      toast(
        `${f.name} — image upload requires a VLM. Enable image_enrichment in opencraig.yaml.`,
        { variant: 'error', duration: 7000 },
      )
    } else if (verdict.reason === 'spreadsheet_disabled') {
      toast(
        `${f.name} — spreadsheet upload requires an LLM. Enable table_enrichment in opencraig.yaml.`,
        { variant: 'error', duration: 7000 },
      )
    }
  }
  if (accepted.length) uploads.enqueue(accepted, opts)
  return accepted.length
}

// URL-driven doc detail. Clicking a document in the browser sets ?doc=<id>
// which swaps the main area for the embedded Repository view (PDF + tree +
// chunks). Clearing the query returns to the browser.
const focusedDocId = computed(() => {
  const d = route.query.doc
  return typeof d === 'string' && d ? d : ''
})

function onDocDetailClose(payload) {
  const q = { ...route.query }
  // Clear the doc + its side-state (tab, pipeline, node, chunk, pdf)
  delete q.doc; delete q.pipeline; delete q.node; delete q.chunk; delete q.pdf
  // Optional: route the workspace back to a specific folder. Sent by
  // the back arrow + the breadcrumb segments in DocDetail so closing
  // lands on the doc's parent folder by default rather than wherever
  // the workspace was idling. Falls through to ``q.path`` (kept by
  // onOpenDocument) when no explicit target is given.
  const toPath = payload && typeof payload === 'object' ? payload.toPath : null
  if (typeof toPath === 'string' && toPath.startsWith('/')) {
    if (toPath === '/') delete q.path; else q.path = toPath
  }
  router.replace({ path: route.path, query: q })
}

// ── Page state ────────────────────────────────────────────────────
const viewingTrash = ref(false)
// trashCount drives the bin badge in the toolbar AND the "N items"
// label inside the trash. While viewingTrash is false we get it cheaply
// via the ``getTrashStats`` summary endpoint; while viewingTrash is true
// we sync it to ``trashItems.length`` after each list/restore/purge.
const trashCount = ref(0)
const trashItems = ref([])

// Tracks doc_ids with a delete request in flight, so the menu can
// disable "Delete" for that item and a quick second click can't fire
// a duplicate DELETE while the first is in-flight or its confirm
// dialog is still open.
const deletingDocs = reactive(new Set())
const trashLoading = ref(false)
const fileInput = ref(null)

// Synthetic breadcrumb for trash mode. Clicking ``/`` exits the trash
// (handled by ``onCrumbNavigate`` below); the second crumb is the
// current location and is a no-op.
const trashCrumbs = [
  { name: '/', path: '/' },
  { name: 'Recycle bin', path: '/__trash__' },
]
function onCrumbNavigate(path) {
  // Self-clicks on the trash crumb are no-op — the user is already here.
  if (path === '/__trash__') return
  // ``navigate`` clears ``viewingTrash`` itself, so a click on ``/``
  // (or any other crumb) naturally exits the trash.
  navigate(path)
}

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
      { label: 'New folder',    icon: FolderPlus,      shortcut: 'Ctrl+N', action: 'new-folder' },
      { label: 'Upload file',   icon: Upload,                              action: 'upload' },
      { divider: true },
      {
        label: 'Paste',
        icon: ClipboardPaste,
        shortcut: 'Ctrl+V',
        action: 'paste',
        disabled: !ws.hasClipboard.value,
      },
      { divider: true },
      { label: 'Refresh', icon: RefreshCw, action: 'refresh' },
    ]
  }
  if (item.type === 'folder') {
    return [
      { label: 'Open',           icon: FolderOpen,           action: 'open' },
      { label: 'Search inside',  icon: Search,               action: 'scope-chat' },
      { divider: true },
      { label: 'Members…',       icon: UserPlus,             action: 'members' },
      { divider: true },
      { label: 'Cut',            icon: Scissors,             shortcut: 'Ctrl+X', action: 'cut' },
      { label: 'Copy',           icon: Copy,                 shortcut: 'Ctrl+C', action: 'copy' },
      { divider: true },
      { label: 'Rename',         icon: PenLine,              shortcut: 'F2',     action: 'rename' },
      { label: 'Move to…',       icon: ArrowRightFromLine,                       action: 'move' },
      { divider: true },
      { label: 'Delete',         icon: Trash2,               shortcut: 'Del',    action: 'delete', danger: true },
    ]
  }
  // document
  // In-flight ingestion: hide every action that would mutate or use
  // the doc's content (it's incomplete, so cut/copy/rename/move/scope
  // would land in inconsistent state). Delete becomes "Cancel & delete"
  // — wired via ``source: 'in-flight'`` so onDelete routes to the
  // hard-purge path instead of the recycle bin (no point recovering a
  // partial parse).
  if (_isDocInFlight(item)) {
    return [
      { label: 'Preview',           icon: Eye,    action: 'preview' },
      { divider: true },
      { label: 'Cancel & delete',   icon: Trash2, shortcut: 'Del', action: 'delete', danger: true },
    ]
  }
  return [
    { label: 'Preview',         icon: Eye,                  action: 'preview' },
    { label: 'Ask in Chat',     icon: MessageSquare,        action: 'scope-chat' },
    { divider: true },
    { label: 'Cut',             icon: Scissors,             shortcut: 'Ctrl+X', action: 'cut' },
    { label: 'Copy',            icon: Copy,                 shortcut: 'Ctrl+C', action: 'copy' },
    { divider: true },
    { label: 'Rename',          icon: PenLine,              shortcut: 'F2',     action: 'rename' },
    { label: 'Move to…',        icon: ArrowRightFromLine,                       action: 'move' },
    { divider: true },
    { label: 'Delete',          icon: Trash2,               shortcut: 'Del',    action: 'delete', danger: true },
  ]
}

// Single source of truth for "is this doc still ingesting?" — read
// from the ``inFlight`` flag the FileGrid/FileList already computed
// (they have the full doc record with all four sub-status fields:
// status, embed_status, enrich_status, kg_status). Using their
// pre-computed flag keeps the answer consistent across the meta line
// pill, the drag-blocking guard, and this menu/delete logic.
function _isDocInFlight(item) {
  return item?.type === 'document' && !!item.inFlight
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
  if (action === 'members' && item?.type === 'folder') return onOpenMembers(item)
}

// ── Members dialog — open via right-click "Members…" ────────────
const membersDialogOpen = ref(false)
const membersDialogFolderId = ref(null)
const membersDialogFolderLabel = ref('')
function onOpenMembers(folder) {
  if (!folder?.folder_id) return
  membersDialogFolderId.value = folder.folder_id
  // Show the folder's user-facing name (already disambiguated for
  // top-level Spaces). Fall back to the path's tail if name is
  // missing for any reason.
  membersDialogFolderLabel.value =
    folder.name || (folder.path ? folder.path.split('/').filter(Boolean).pop() : '') || ''
  membersDialogOpen.value = true
}

// ── Core actions ──────────────────────────────────────────────────

async function navigate(path) {
  viewingTrash.value = false
  // Drop any pending inline-rename / inline-create state — the folder
  // being edited may not exist in the new path's list.
  renamingKey.value = ''
  creatingFolder.value = false
  // Flip the URL FIRST, before the await — clicking a folder should
  // feel instant in the address bar / breadcrumb. ``ws.navigate`` then
  // wipes child arrays + flips loading on synchronously, so the file
  // grid swaps to the loading skeleton in the same tick. The actual
  // fetch resolves later; if the user clicks again before it returns,
  // the generation guard inside useLibrary drops the stale response
  // (see ``_loadGen`` there).
  const desired = path === '/' ? undefined : path
  if (route.query.path !== desired) {
    const q = { ...route.query }
    if (desired) q.path = desired; else delete q.path
    router.replace({ path: route.path, query: q })
  }
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

// ── Trash mode (state lifted out of TrashView so the toolbar can
//    drive the count + Empty-bin button without ref-fishing) ─────

async function onShowTrash() {
  viewingTrash.value = true
  await loadTrashItems()
}

async function loadTrashItems() {
  trashLoading.value = true
  try {
    const r = await listTrash()
    trashItems.value = r?.items || []
    // Keep the toolbar badge / "N items" label in sync with what the
    // user actually sees in the table.
    trashCount.value = trashItems.value.length
  } catch (e) {
    console.error('listTrash failed:', e)
    trashItems.value = []
  } finally {
    trashLoading.value = false
  }
}

async function onRestoreItem(item) {
  const ok = await confirm({
    title: `Restore "${item.filename || item.name}"?`,
    description: 'It will be moved back to its original location.',
    confirmText: 'Restore',
  })
  if (!ok) return
  const body = item.type === 'folder'
    ? { folder_paths: [item.path] }
    : { doc_ids: [item.doc_id] }
  try {
    await restoreFromTrash(body)
    // Reloading the workspace tree/contents alongside the trash list
    // because the restored item reappears in its original folder.
    await Promise.all([ws.loadTree(), loadTrashItems()])
  } catch (e) {
    toast('Restore failed: ' + e.message, { variant: 'error' })
  }
}

async function onPurgeItem(item) {
  const name = item.filename || item.name
  const ok = await confirm({
    title: `Permanently delete "${name}"?`,
    description: 'This cannot be undone.',
    confirmText: 'Delete forever',
    variant: 'destructive',
  })
  if (!ok) return
  const body = item.type === 'folder'
    ? { folder_paths: [item.path] }
    : { doc_ids: [item.doc_id] }
  // Single-item purge can still be slow when the doc has thousands of
  // chunks (vector + KG cascade); show a loading toast so the UI never
  // looks frozen.
  const loadingId = toast(`Deleting "${name}"…`, { variant: 'loading' })
  try {
    await purgeTrashItems(body)
    await loadTrashItems()
    dismissToast(loadingId)
    toast(`Deleted "${name}"`, { variant: 'success' })
  } catch (e) {
    dismissToast(loadingId)
    toast('Delete failed: ' + e.message, { variant: 'error' })
  }
}

// Tracks an in-flight Empty bin call. Bound to the Toolbar's button so
// the user can't double-fire the (slow, vector + KG + relational) purge.
const emptyingTrash = ref(false)

async function onEmptyTrash() {
  if (emptyingTrash.value) return
  const n = trashItems.value.length || trashCount.value
  const ok = await confirm({
    title: 'Empty the recycle bin?',
    description: `All ${n} item${n === 1 ? '' : 's'} will be permanently deleted. This cannot be undone.`,
    confirmText: 'Empty bin',
    variant: 'destructive',
  })
  if (!ok) return

  emptyingTrash.value = true
  // Persistent loading toast — the purge can take many seconds (vector
  // store + KG + per-doc cascade). Without this the UI feels frozen.
  const loadingId = toast(`Emptying recycle bin (${n} item${n === 1 ? '' : 's'})…`, {
    variant: 'loading',
  })
  try {
    await emptyTrash()
    trashItems.value = []
    trashCount.value = 0
    dismissToast(loadingId)
    toast(`Recycle bin emptied`, { variant: 'success' })
  } catch (e) {
    dismissToast(loadingId)
    toast('Empty bin failed: ' + e.message, { variant: 'error' })
  } finally {
    emptyingTrash.value = false
  }
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
  // Hand files off via the capability-aware wrapper — pre-flights
  // image / legacy-office gates and toasts rejected files. The
  // upload queue then handles upload + ingestion polling + progress.
  const accepted = safeEnqueue(files, { folderPath })
  if (accepted) uploads.toggleDrawer(true)
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

// Move-to flow: open the folder picker, then dispatch a single
// ``opBulkMoveDocuments`` + per-folder ``opMoveFolder`` once the user
// picks a destination. Selection-aware via ``buildClipboardItems`` —
// right-clicking an item that's part of a multi-selection moves the
// whole selection.
const movePickerOpen = ref(false)
const movePickerTitle = ref('')
const movePickerExclude = ref([])
let movePendingItems = []  // captured before the dialog opens; restored on confirm

function onMoveDialog(item) {
  const items = buildClipboardItems(item).filter(Boolean)
  if (!items.length) return
  movePendingItems = items
  // Hide only what cannot be a valid destination:
  //   - The trash (system).
  //   - Each folder being moved (and its subtree) — moving a folder
  //     into itself or any descendant is a cycle.
  // Note: the source's *parent* folder is NOT excluded. Picking it
  // back is a no-op and the backend handles that gracefully — much
  // better than hiding the parent's whole subtree, which made
  // every cousin invisible (was: "/agriculture" excluded → user at
  // "/" couldn't see agriculture even though most of its children
  // were valid destinations).
  movePickerExclude.value = [
    '/__trash__',
    ...items.filter(i => i.type === 'folder').map(i => i.path),
  ]
  movePickerTitle.value = items.length === 1
    ? `Move "${items[0].name}" to…`
    : `Move ${items.length} items to…`
  movePickerOpen.value = true
}

async function onMoveTargetPicked(target) {
  const items = movePendingItems
  movePendingItems = []
  if (!items.length || !target) return
  try {
    const docIds = items.filter(i => i.type === 'document').map(i => i.doc_id)
    if (docIds.length) await ws.opBulkMoveDocuments(docIds, target)
    for (const i of items.filter(i => i.type === 'folder')) {
      await ws.opMoveFolder(i.path, target)
    }
    ws.clearSelection()
    toast(
      items.length === 1
        ? `Moved “${items[0].name}” to ${target}`
        : `Moved ${items.length} items to ${target}`,
      { variant: 'success' },
    )
  } catch (e) {
    toast('Move failed: ' + e.message, { variant: 'error' })
  }
}

async function onDelete(item) {
  // Batch-aware: when the right-clicked item is part of the current
  // multi-selection, operate on the whole selection. Mirrors the
  // ``buildClipboardItems`` rule used by cut/copy.
  const items = buildClipboardItems(item).filter(Boolean)
  if (!items.length) return

  const docs = items.filter(i => i.type === 'document')
  const folders = items.filter(i => i.type === 'folder')
  const n = items.length

  // In-flight docs (status not in {ready, error}) are abandoned
  // mid-pipeline rather than soft-deleted: a partial parse has no
  // value to recover, and keeping it in the recycle bin would just
  // confuse the user. Routes via ``?hard=true`` which short-circuits
  // the trash detour and goes straight to vector + KG + relational
  // purge. Folders never use this path (they're never "in flight").
  const inFlightDocs = docs.filter(_isDocInFlight)
  const normalDocs = docs.filter(d => !_isDocInFlight(d))
  const purgeOnly = inFlightDocs.length > 0 && normalDocs.length === 0 && folders.length === 0

  const title = purgeOnly
    ? (n === 1 ? `Cancel ingestion of "${items[0].name}"?` : `Cancel ingestion of ${n} items?`)
    : (n === 1 ? `Move "${items[0].name}" to recycle bin?` : `Move ${n} items to recycle bin?`)
  const description = purgeOnly
    ? 'Ingestion will be aborted and any partial data will be deleted permanently.'
    : 'Items in the recycle bin can be restored later.'
  const ok = await confirm({
    title,
    description,
    confirmText: purgeOnly ? 'Cancel & delete' : 'Move to bin',
    variant: purgeOnly ? 'destructive' : undefined,
  })
  if (!ok) return

  const docIds = docs.map(d => d.doc_id)
  docIds.forEach(id => deletingDocs.add(id))

  try {
    const { request } = await import('@/api/client')
    // Parallel DELETEs — backend serializes via the DB, but client-side
    // parallelism saves an N×roundtrip stall on large selections.
    // In-flight docs go to ``?hard=true`` (purge straight through);
    // normal docs go through trash; folders go through opDeleteFolder.
    await Promise.all([
      ...inFlightDocs.map(d =>
        request(`/api/v1/documents/${d.doc_id}?hard=true`, { method: 'DELETE' })
      ),
      ...normalDocs.map(d => request(`/api/v1/documents/${d.doc_id}`, { method: 'DELETE' })),
      ...folders.map(f => ws.opDeleteFolder(f.path)),
    ])
    await refresh()
    ws.clearSelection()

    const message = purgeOnly
      ? (n === 1 ? `Cancelled “${items[0].name}”` : `Cancelled ${n} items`)
      : (n === 1 ? `Moved “${items[0].name}” to recycle bin` : `Moved ${n} items to recycle bin`)

    // Undo only meaningful for soft-deleted docs (trash), never for
    // hard-purged in-flight ones (the data is gone for good) and never
    // for folders (their trashed paths aren't easily recoverable
    // post-DELETE; user can recover via recycle bin UI).
    const undoEligibleIds = normalDocs.map(d => d.doc_id)
    const action = (undoEligibleIds.length > 0 && folders.length === 0 && inFlightDocs.length === 0)
      ? {
          label: 'Undo',
          onClick: async () => {
            try {
              await restoreFromTrash({ doc_ids: undoEligibleIds })
              await refresh()
              toast(
                undoEligibleIds.length === 1
                  ? `Restored “${normalDocs[0].name}”`
                  : `Restored ${undoEligibleIds.length} items`,
                { variant: 'info' },
              )
            } catch (e) {
              toast('Restore failed: ' + e.message, { variant: 'error' })
            }
          },
        }
      : undefined
    toast(message, { variant: 'success', action })
  } catch (e) {
    toast('Delete failed: ' + e.message, { variant: 'error' })
  } finally {
    docIds.forEach(id => deletingDocs.delete(id))
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
// Drop targets that survive the sidebar removal: folder tiles in
// the FileGrid / FileList. "Move to root" / "move to a far folder"
// is via the right-click menu's "Move to…" → FolderPickerDialog.

function onDropOntoFolder({ items, targetPath }) { doDropMove(items, targetPath) }

function _pathParent(p) {
  if (!p || p === '/') return '/'
  const i = p.lastIndexOf('/')
  return i <= 0 ? '/' : p.slice(0, i)
}

async function doDropMove(items, targetPath) {
  // Drop no-ops at the source so we never round-trip a request the
  // server would just reject or silently ignore. Three cases:
  //   - folder onto itself                 (same folder)
  //   - folder onto its own subtree        (cycle — server rejects)
  //   - folder/document onto current parent (already there)
  // ``items`` is the unwrapped array from FileGrid/FileList/FolderTreeNode.
  const effective = (items || []).filter(i => {
    if (!i || !i.path) return false
    if (i.type === 'folder') {
      if (i.path === targetPath) return false
      if (targetPath === i.path || targetPath.startsWith(i.path + '/')) return false
      if (_pathParent(i.path) === targetPath) return false
      return true
    }
    if (i.type === 'document') {
      if (_pathParent(i.path) === targetPath) return false
      return true
    }
    return false
  })
  if (!effective.length) return

  try {
    const docIds = effective.filter(i => i.type === 'document').map(i => i.doc_id)
    if (docIds.length) await ws.opBulkMoveDocuments(docIds, targetPath)
    for (const i of effective) {
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
  // Preserve ``path`` (and any other query params) so the back arrow's
  // default close (no explicit toPath) returns the user to the same
  // folder they came from. Without this, opening a doc wiped the path
  // query and back navigation always landed at the root.
  router.push({
    path: '/library',
    query: { ...route.query, doc: doc.doc_id },
  })
}

// ── Keyboard shortcuts ─────────────────────────────────────────────

function onKeydown(e) {
  if (viewingTrash.value) return
  // Bail out when the user is typing in an input — otherwise the
  // page-level shortcuts hijack characters meant for the inline-create
  // / inline-rename editor: Backspace navigates up (and side-effects
  // unmount the input), Delete kills the selected row, Ctrl+A selects
  // all rows instead of the input text, Ctrl+1/2 swaps view mode
  // mid-edit, etc. The input owns its own Esc / Enter handlers, so
  // returning here is safe.
  const t = e.target
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
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
  // Restore folder from the URL (?path=/agriculture) so refresh /
  // bookmark / direct-link land on the same view. Sanity-checks: must
  // begin with "/" and not target the trash subtree (trash has its
  // own UI flow).
  const seedPath = route.query.path
  let initial = (typeof seedPath === 'string'
    && seedPath.startsWith('/')
    && !seedPath.startsWith('/__trash__'))
    ? seedPath
    : '/'
  // Default landing: when the URL doesn't pin a path AND the user has
  // exactly one Space, drop straight into it. Saves a click for the
  // single-Space workflow and gives the breadcrumb something to render.
  // Multi-Space users land on the synthetic root showing the spaces
  // list (no preferred "home" — every Space is a peer now that the
  // personal-Space concept has been retired).
  if (initial === '/') {
    const home = ws.defaultLandingSpace()
    if (home?.path) initial = home.path
  }
  await ws.loadContents(initial)
  // Keep ws.currentPath in sync with the URL — loadContents alone
  // doesn't update it. Use the same navigate() the user-facing
  // breadcrumb/click paths go through so URL writeback stays
  // single-pathed.
  if (initial !== '/') {
    await ws.navigate(initial)
  }
  await refreshTrashCount()
})

// One-shot auto-enter: if the user lands at the synthetic root with
// no ``?path=`` pinning them there, but a sensible landing Space
// appears later (KeepAlive remount, or tree loaded later than the
// initial mount await), drop them into it ONCE. After this fires,
// the user is free to navigate back to ``/`` deliberately and we
// won't keep re-snapping them away.
const _autoEnterFired = ref(false)
watch(
  () => ws.tree.value?.children?.length,
  () => {
    if (_autoEnterFired.value) return
    if (ws.currentPath.value !== '/') { _autoEnterFired.value = true; return }
    if (route.query.path) { _autoEnterFired.value = true; return }
    const home = ws.defaultLandingSpace()
    if (!home?.path) return
    _autoEnterFired.value = true
    navigate(home.path)
  },
  { immediate: true },
)

// React to external URL changes (back / forward / direct link tweak).
// Only respond when path actually differs from current view.
watch(
  () => route.query.path,
  async (newPath) => {
    const target = (typeof newPath === 'string' && newPath.startsWith('/')) ? newPath : '/'
    if (target === ws.currentPath.value || viewingTrash.value) return
    await ws.navigate(target)
  },
)

// KeepAlive re-activation: returning to /library from another tab
// (sidebar nav fires a router-link to bare ``/library``, no query).
// Without this, the path-watcher above would see ``query.path =
// undefined`` and reset state to root — losing the user's place. Push
// the cached state BACK INTO the URL instead, so the watcher sees a
// consistent (current === target) and stays put. Also reasserts the
// trash view if the user was looking at it.
onActivated(() => {
  const cur = ws.currentPath.value
  const inTrash = viewingTrash.value
  const urlPath = route.query.path
  const desired = inTrash ? undefined : (cur === '/' ? undefined : cur)
  if (urlPath !== desired) {
    const q = { ...route.query }
    if (desired) q.path = desired; else delete q.path
    router.replace({ path: route.path, query: q })
  }
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
/* Single-row toolbar — Toolbar.vue already provides padding + the
   bottom border. We just need to keep it from shrinking under
   flex layout pressure from the body below. */
.workspace__top { flex-shrink: 0; }
.workspace__body {
  display: flex;
  flex: 1;
  min-height: 0;
  overflow: hidden;
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
