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

      <!-- View mode toggle — list vs grid. Persisted to localStorage so
           the user's preference survives a refresh. Library has the same
           knob; both views read/write the same key (``workspace.viewMode``)
           so the user's choice carries between Knowledge Base and Workbench. -->
      <div class="view-toggle">
        <button
          class="view-btn"
          :class="{ 'view-btn--active': viewMode === 'grid' }"
          @click="setViewMode('grid')"
          title="Grid view"
        ><LayoutGrid :size="14" :stroke-width="1.5" /></button>
        <button
          class="view-btn"
          :class="{ 'view-btn--active': viewMode === 'list' }"
          @click="setViewMode('list')"
          title="List view"
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
            :rows="rows"
            :selection="selection"
            :loading="loading"
            :capabilities="capabilities"
            @select="onSelect"
            @open-row="onOpenRow"
            @context-menu="onContextMenu"
          >
            <template #empty>{{ t('workspace.empty_title') }}</template>
          </FileTiles>
          <FileTable
            v-else
            :rows="rows"
            :selection="selection"
            :loading="loading"
            :capabilities="capabilities"
            :columns="['name', 'type', 'size', 'modified']"
            @select="onSelect"
            @open-row="onOpenRow"
            @context-menu="onContextMenu"
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  AlertCircle,
  Download,
  FolderPlus,
  LayoutGrid,
  List,
  MessageSquare,
  Upload,
} from 'lucide-vue-next'

import {
  getWorkdirInfo,
  listWorkdir,
  makeWorkdirFolder,
  uploadWorkdirFile,
  workdirDownloadUrl,
  workdirPreviewUrl,
} from '@/api'
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
  createdAt: null,                     // workdir API doesn't expose
  modifiedAt: e.modified_at ?? null,
  extras: e,
})))

// ── Capabilities the renderer should enable ───────────────────────
// Stage 2 turns on select / multi-select / context-menu but keeps
// rename + drag-move off until the backend endpoints land in
// Stage 3 (otherwise the menu items would silently no-op which is
// worse than not appearing).
const capabilities = {
  select: true,
  multiSelect: true,
  rename: false,
  dragMove: false,
  contextMenu: true,
}

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

function onMarqueeSelect({ keys, additive }) {
  // MarqueeSelection emits the keys it's covering. Same semantics as
  // a click: additive means union with existing selection;
  // non-additive replaces.
  if (!additive) selection.clear()
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

// Menu item shape: { id, label, icon?, danger?, disabled? }.
// ContextMenu emits ``action`` with the picked id; we route below.
const ctxItems = computed(() => {
  const r = ctx.row
  if (!r) {
    // Empty-area menu — only "New folder" makes sense without a row.
    return [
      { id: 'new-folder', label: t('workspace.new_folder') },
    ]
  }
  if (r.kind === 'folder') {
    return [
      { id: 'open', label: 'Open' },
      { id: 'open-chat', label: t('workspace.open_chat_here') },
    ]
  }
  return [
    { id: 'preview', label: 'Preview' },
    { id: 'download', label: t('workspace.download') },
  ]
})

function onContextAction(actionId) {
  ctx.open = false
  const r = ctx.row
  if (actionId === 'new-folder') {
    onMakeFolder()
    return
  }
  if (!r) return
  if (actionId === 'open') open(r.path)
  else if (actionId === 'open-chat') onOpenChatRow(r)
  else if (actionId === 'preview') {
    previewEntry.value = r.extras
    previewOpen.value = true
  } else if (actionId === 'download') {
    window.open(downloadUrl(r.path), '_blank', 'noopener')
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
      message: t('workspace.new_folder_error_separator'),
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
      message: e?.message || String(e),
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
      message: e?.message || String(e),
    })
  } finally {
    busy.value = false
    if (uploadInput.value) uploadInput.value.value = ''
  }
}

function downloadUrl(path) { return workdirDownloadUrl(path) }

// ── URL ↔ state sync ────────────────────────────────────────────
watch(
  () => route.query.path,
  (p) => {
    p = p || '/'
    if (p !== currentPath.value) load(p)
  },
)

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
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.12s;
}
:deep(.list-row:hover) .row-actions-inner { opacity: 1; }
.row-action-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
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
