<!--
  ProjectFileBrowser — file manager view of a project workdir.

  Phase 1 capabilities:
    - Breadcrumb navigation through subdirs
    - Upload (button + drag-drop on the whole panel)
    - List rows with name / size / modified / kind
    - Click folder → drill in; click file → download
    - Row dot-menu: rename, soft-delete (move + bulk land in 1.7+)
    - "New folder" button + inline mkdir
    - Trash mode toggle (separate view; restore / purge / empty)

  All mutating actions go through the API layer; on success we
  re-list. Optimistic UI is deliberately not on the v1 menu —
  the file API is fast enough that re-fetch is cheap and there's
  no good UX win that's worth rollback bookkeeping at this size.

  Auth: the route layer 404s viewer-write attempts; we surface
  whatever the API throws inline rather than pre-disabling controls
  on the frontend (single source of truth on the server).
-->
<template>
  <section
    class="pfb"
    :class="{ 'pfb--drag-over': dragOver, 'pfb--readonly': readOnly }"
    @dragenter.prevent="onDragEnter"
    @dragover.prevent="onDragOver"
    @dragleave="onDragLeave"
    @drop.prevent="onDrop"
  >
    <header class="pfb__top">
      <Breadcrumb :crumbs="crumbs" @navigate="onCrumbNav" />

      <div class="pfb__actions">
        <button
          v-if="!viewingTrash"
          class="btn btn--ghost"
          :disabled="readOnly"
          @click="onNewFolder"
        >
          <FolderPlus :size="14" :stroke-width="1.75" />
          <span>{{ t('workspace.files.new_folder') }}</span>
        </button>
        <button
          v-if="!viewingTrash"
          class="btn btn--ghost"
          :disabled="readOnly"
          @click="onUploadClick"
        >
          <UploadCloud :size="14" :stroke-width="1.75" />
          <span>{{ t('workspace.files.upload') }}</span>
        </button>
        <button
          v-if="!viewingTrash"
          class="btn btn--ghost"
          @click="$emit('import-from-library')"
          :disabled="readOnly"
        >
          <BookOpen :size="14" :stroke-width="1.75" />
          <span>{{ t('workspace.files.import_from_library') }}</span>
        </button>
        <button
          class="btn btn--ghost"
          :class="{ 'btn--active': viewingTrash }"
          @click="toggleTrash"
        >
          <Trash2 :size="14" :stroke-width="1.75" />
          <span>{{ viewingTrash
            ? t('workspace.files.exit_trash')
            : t('workspace.files.show_trash', { n: trashCount }) }}</span>
        </button>
        <button
          v-if="viewingTrash && trashEntries.length > 0"
          class="btn btn--danger"
          :disabled="readOnly"
          @click="onEmptyTrash"
        >
          {{ t('workspace.files.empty_trash') }}
        </button>
      </div>
    </header>

    <input
      ref="fileInput"
      type="file"
      multiple
      style="display: none"
      @change="onFilePicked"
    />

    <!-- Drag-over overlay -->
    <div v-if="dragOver && !viewingTrash && !readOnly" class="pfb__drop-overlay">
      <UploadCloud :size="28" :stroke-width="1.5" />
      <p>{{ t('workspace.files.drop_to_upload', { path: currentPath || '/' }) }}</p>
    </div>

    <!-- Inline create-folder row -->
    <div v-if="creatingFolder" class="pfb__inline-create">
      <FolderPlus :size="14" :stroke-width="1.75" />
      <input
        ref="newFolderInput"
        v-model="newFolderName"
        class="pfb__inline-input"
        :placeholder="t('workspace.files.new_folder_placeholder')"
        @keydown.enter.prevent="confirmNewFolder"
        @keydown.esc="cancelNewFolder"
        @blur="confirmNewFolder"
      />
    </div>

    <!-- Body -->
    <div class="pfb__body">
      <div v-if="loading" class="pfb__state">
        <Skeleton v-for="i in 4" :key="i" class="pfb__skeleton" />
      </div>

      <div v-else-if="error" class="pfb__state pfb__state--error">
        <AlertCircle :size="20" :stroke-width="1.75" />
        <p>{{ t('workspace.files.load_error', { msg: error }) }}</p>
        <button class="btn btn--ghost" @click="reload">
          {{ t('common.retry') }}
        </button>
      </div>

      <!-- Trash view -->
      <template v-else-if="viewingTrash">
        <div v-if="!trashEntries.length" class="pfb__state pfb__state--empty">
          <Trash2 :size="28" :stroke-width="1.25" />
          <p>{{ t('workspace.files.trash_empty') }}</p>
        </div>
        <ul v-else class="pfb__list">
          <li
            v-for="entry in trashEntries"
            :key="entry.trash_id"
            class="pfb__row pfb__row--trash"
          >
            <span class="pfb__icon">
              <FolderIcon
                v-if="entry.is_dir"
                :size="14"
                :stroke-width="1.75"
              />
              <FileIcon v-else :size="14" :stroke-width="1.75" />
            </span>
            <span class="pfb__name">{{ entry.original_path }}</span>
            <span class="pfb__size">{{ fmtSize(entry.size_bytes) }}</span>
            <span class="pfb__date">{{ fmtDate(entry.trashed_at) }}</span>
            <span class="pfb__actions-cell">
              <button
                class="icon-btn"
                :disabled="readOnly || busyTrash === entry.trash_id"
                :title="t('workspace.files.restore')"
                @click="onRestore(entry)"
              >
                <RotateCcw :size="13" :stroke-width="1.75" />
              </button>
              <button
                class="icon-btn icon-btn--danger"
                :disabled="readOnly || busyTrash === entry.trash_id"
                :title="t('workspace.files.purge')"
                @click="onPurge(entry)"
              >
                <Trash2 :size="13" :stroke-width="1.75" />
              </button>
            </span>
          </li>
        </ul>
      </template>

      <!-- Regular file list -->
      <template v-else>
        <div v-if="!entries.length" class="pfb__state pfb__state--empty">
          <FolderIcon :size="28" :stroke-width="1.25" />
          <p>{{ t('workspace.files.empty', { path: currentPath || '/' }) }}</p>
          <p class="pfb__state-hint">{{ t('workspace.files.empty_hint') }}</p>
        </div>
        <ul v-else class="pfb__list">
          <li
            v-for="entry in entries"
            :key="entry.path"
            class="pfb__row"
            :class="{ 'pfb__row--dir': entry.is_dir }"
            @click="onRowClick(entry)"
          >
            <span class="pfb__icon">
              <FolderIcon
                v-if="entry.is_dir"
                :size="14"
                :stroke-width="1.75"
              />
              <FileIcon v-else :size="14" :stroke-width="1.75" />
            </span>
            <span v-if="renamingPath === entry.path" class="pfb__name">
              <input
                v-model="renameDraft"
                ref="renameInput"
                class="pfb__inline-input"
                @click.stop
                @keydown.enter.prevent="confirmRename(entry)"
                @keydown.esc="cancelRename"
                @blur="confirmRename(entry)"
              />
            </span>
            <span v-else class="pfb__name">{{ entry.name }}</span>
            <span class="pfb__size">{{ entry.is_dir ? '—' : fmtSize(entry.size_bytes) }}</span>
            <span class="pfb__date">{{ fmtDate(entry.modified_at) }}</span>
            <span class="pfb__actions-cell">
              <button
                v-if="!entry.is_dir"
                class="icon-btn"
                :title="t('workspace.files.download')"
                @click.stop="onDownload(entry)"
              >
                <Download :size="13" :stroke-width="1.75" />
              </button>
              <button
                class="icon-btn"
                :disabled="readOnly"
                :title="t('workspace.files.rename')"
                @click.stop="onRename(entry)"
              >
                <Pencil :size="13" :stroke-width="1.75" />
              </button>
              <button
                class="icon-btn icon-btn--danger"
                :disabled="readOnly"
                :title="t('workspace.files.delete')"
                @click.stop="onDelete(entry)"
              >
                <Trash2 :size="13" :stroke-width="1.75" />
              </button>
            </span>
          </li>
        </ul>
      </template>
    </div>
  </section>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  AlertCircle,
  BookOpen,
  Download,
  File as FileIcon,
  Folder as FolderIcon,
  FolderPlus,
  Pencil,
  RotateCcw,
  Trash2,
  UploadCloud,
} from 'lucide-vue-next'

import {
  deleteProjectFile,
  emptyProjectTrash,
  listProjectFiles,
  listProjectTrash,
  mkdirProjectFile,
  moveProjectFile,
  projectFileDownloadUrl,
  purgeProjectTrash,
  restoreProjectTrash,
  uploadProjectFile,
} from '@/api'
import Breadcrumb from '@/components/workspace/Breadcrumb.vue'
import Skeleton from '@/components/Skeleton.vue'
import { useDialog } from '@/composables/useDialog'

const { t } = useI18n()
const dialog = useDialog()

const props = defineProps({
  projectId: { type: String, required: true },
  // True when the principal can read but not write (viewer share).
  // The route layer is the source of truth (404s viewer writes); we
  // also disable buttons here so the UX matches the authz.
  readOnly: { type: Boolean, default: false },
})

const emit = defineEmits(['import-from-library'])

// ── State ──────────────────────────────────────────────────────────
const currentPath = ref('') // workdir-relative; '' = root
const entries = ref([])
const trashEntries = ref([])
const loading = ref(false)
const error = ref('')
const dragOver = ref(false)
const viewingTrash = ref(false)
const busyTrash = ref('')
const creatingFolder = ref(false)
const newFolderName = ref('')
const newFolderInput = ref(null)
const renamingPath = ref('')
const renameDraft = ref('')
const renameInput = ref(null)
const fileInput = ref(null)

const trashCount = computed(() => trashEntries.value.length)

const crumbs = computed(() => {
  // Root crumb + each segment of currentPath. Existing Breadcrumb.vue
  // expects each entry to carry { name, path }.
  const parts = currentPath.value ? currentPath.value.split('/') : []
  const out = [{ name: t('workspace.files.root'), path: '' }]
  let acc = ''
  for (const p of parts) {
    acc = acc ? `${acc}/${p}` : p
    out.push({ name: p, path: acc })
  }
  return out
})

// ── Loading ────────────────────────────────────────────────────────
async function loadFiles() {
  loading.value = true
  error.value = ''
  try {
    entries.value = await listProjectFiles(props.projectId, currentPath.value)
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function loadTrash() {
  loading.value = true
  error.value = ''
  try {
    trashEntries.value = await listProjectTrash(props.projectId)
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function reload() {
  if (viewingTrash.value) await loadTrash()
  else await loadFiles()
}

// Refresh trash count after every file op so the toggle label stays
// honest. Cheap — list_trash is just a JSON read.
async function refreshTrashCount() {
  try {
    trashEntries.value = await listProjectTrash(props.projectId)
  } catch {
    // silent — main file op already succeeded
  }
}

// ── Navigation ─────────────────────────────────────────────────────
function onRowClick(entry) {
  if (entry.is_dir) {
    currentPath.value = entry.path
  } else {
    onDownload(entry)
  }
}

function onCrumbNav(path) {
  currentPath.value = path
}

function toggleTrash() {
  viewingTrash.value = !viewingTrash.value
  if (viewingTrash.value) loadTrash()
  else loadFiles()
}

// ── Actions: download ──────────────────────────────────────────────
function onDownload(entry) {
  // Auth cookie rides on same-origin GET; the browser handles the
  // file-download dance via Content-Disposition: attachment.
  const url = projectFileDownloadUrl(props.projectId, entry.path)
  const a = document.createElement('a')
  a.href = url
  a.download = entry.name
  a.click()
}

// ── Actions: upload ────────────────────────────────────────────────
function onUploadClick() {
  fileInput.value?.click()
}

async function onFilePicked(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = '' // reset so picking the same file twice still fires
  await uploadFiles(files)
}

async function uploadFiles(files) {
  if (!files.length) return
  for (const f of files) {
    const target = currentPath.value
      ? `${currentPath.value}/${f.name}`
      : `inputs/${f.name}` // default to inputs/ at root level
    try {
      await uploadProjectFile(props.projectId, f, target)
    } catch (err) {
      if (err?.status === 409) {
        const overwrite = await dialog.confirm({
          title: t('workspace.files.overwrite_title'),
          description: t('workspace.files.overwrite_desc', { path: target }),
          confirmText: t('workspace.files.overwrite_confirm'),
          variant: 'danger',
        })
        if (overwrite) {
          try {
            await uploadProjectFile(props.projectId, f, target, {
              overwrite: true,
            })
          } catch (err2) {
            dialog.toast(
              t('workspace.files.upload_error', {
                name: f.name,
                msg: err2?.message || String(err2),
              }),
              { variant: 'error' },
            )
          }
        }
      } else {
        dialog.toast(
          t('workspace.files.upload_error', {
            name: f.name,
            msg: err?.message || String(err),
          }),
          { variant: 'error' },
        )
      }
    }
  }
  // Refresh once after the batch so we don't N+1 the list endpoint
  await reload()
}

// ── Actions: rename ────────────────────────────────────────────────
function onRename(entry) {
  renamingPath.value = entry.path
  renameDraft.value = entry.name
  nextTick(() => {
    const input = Array.isArray(renameInput.value)
      ? renameInput.value[0]
      : renameInput.value
    input?.focus()
    input?.select()
  })
}

async function confirmRename(entry) {
  const oldPath = entry.path
  const newName = renameDraft.value.trim()
  // Always clear the inline-edit state so blur-after-Enter doesn't
  // re-fire confirmRename a second time on a now-empty draft.
  renamingPath.value = ''
  renameDraft.value = ''
  if (!newName || newName === entry.name) return
  // Compose new relative path: same parent dir, new name
  const parent = entry.path.includes('/')
    ? entry.path.slice(0, entry.path.lastIndexOf('/'))
    : ''
  const newPath = parent ? `${parent}/${newName}` : newName
  try {
    await moveProjectFile(props.projectId, oldPath, newPath)
    await reload()
  } catch (err) {
    dialog.toast(
      t('workspace.files.rename_error', {
        name: entry.name,
        msg: err?.message || String(err),
      }),
      { variant: 'error' },
    )
  }
}

function cancelRename() {
  renamingPath.value = ''
  renameDraft.value = ''
}

// ── Actions: delete (soft) ─────────────────────────────────────────
async function onDelete(entry) {
  const confirmed = await dialog.confirm({
    title: entry.is_dir
      ? t('workspace.files.delete_dir_title')
      : t('workspace.files.delete_file_title'),
    description: t('workspace.files.delete_desc', { path: entry.path }),
    confirmText: t('workspace.files.delete_confirm'),
    variant: 'danger',
  })
  if (!confirmed) return
  try {
    await deleteProjectFile(props.projectId, entry.path)
    await reload()
    await refreshTrashCount()
    dialog.toast(t('workspace.files.delete_toast', { name: entry.name }))
  } catch (err) {
    dialog.toast(
      t('workspace.files.delete_error', {
        name: entry.name,
        msg: err?.message || String(err),
      }),
      { variant: 'error' },
    )
  }
}

// ── Actions: mkdir ─────────────────────────────────────────────────
function onNewFolder() {
  creatingFolder.value = true
  newFolderName.value = ''
  nextTick(() => newFolderInput.value?.focus())
}

async function confirmNewFolder() {
  const name = newFolderName.value.trim()
  creatingFolder.value = false
  newFolderName.value = ''
  if (!name) return
  const target = currentPath.value ? `${currentPath.value}/${name}` : name
  try {
    await mkdirProjectFile(props.projectId, target)
    await reload()
  } catch (err) {
    dialog.toast(
      t('workspace.files.mkdir_error', {
        msg: err?.message || String(err),
      }),
      { variant: 'error' },
    )
  }
}

function cancelNewFolder() {
  creatingFolder.value = false
  newFolderName.value = ''
}

// ── Actions: trash ─────────────────────────────────────────────────
async function onRestore(entry) {
  busyTrash.value = entry.trash_id
  try {
    await restoreProjectTrash(props.projectId, entry.trash_id)
    await loadTrash()
  } catch (err) {
    dialog.toast(
      t('workspace.files.restore_error', { msg: err?.message || String(err) }),
      { variant: 'error' },
    )
  } finally {
    busyTrash.value = ''
  }
}

async function onPurge(entry) {
  const confirmed = await dialog.confirm({
    title: t('workspace.files.purge_title'),
    description: t('workspace.files.purge_desc', { path: entry.original_path }),
    confirmText: t('workspace.files.purge_confirm'),
    variant: 'danger',
  })
  if (!confirmed) return
  busyTrash.value = entry.trash_id
  try {
    await purgeProjectTrash(props.projectId, entry.trash_id)
    await loadTrash()
  } catch (err) {
    dialog.toast(
      t('workspace.files.purge_error', { msg: err?.message || String(err) }),
      { variant: 'error' },
    )
  } finally {
    busyTrash.value = ''
  }
}

async function onEmptyTrash() {
  const confirmed = await dialog.confirm({
    title: t('workspace.files.empty_title'),
    description: t('workspace.files.empty_desc', { n: trashEntries.value.length }),
    confirmText: t('workspace.files.empty_confirm'),
    variant: 'danger',
  })
  if (!confirmed) return
  try {
    await emptyProjectTrash(props.projectId)
    await loadTrash()
  } catch (err) {
    dialog.toast(
      t('workspace.files.empty_error', { msg: err?.message || String(err) }),
      { variant: 'error' },
    )
  }
}

// ── Drag-drop ──────────────────────────────────────────────────────
let _dragDepth = 0
function onDragEnter() {
  if (props.readOnly || viewingTrash.value) return
  _dragDepth += 1
  dragOver.value = true
}
function onDragOver() {
  if (props.readOnly || viewingTrash.value) return
  dragOver.value = true
}
function onDragLeave() {
  _dragDepth = Math.max(0, _dragDepth - 1)
  if (_dragDepth === 0) dragOver.value = false
}
async function onDrop(e) {
  _dragDepth = 0
  dragOver.value = false
  if (props.readOnly || viewingTrash.value) return
  const files = Array.from(e.dataTransfer?.files || [])
  await uploadFiles(files)
}

// ── External: parent calls this after Library import to refresh ────
defineExpose({ reload, refreshTrashCount })

// ── Watchers + mount ───────────────────────────────────────────────
watch(currentPath, () => {
  if (!viewingTrash.value) loadFiles()
})

watch(
  () => props.projectId,
  () => {
    currentPath.value = ''
    viewingTrash.value = false
    reload()
    refreshTrashCount()
  },
)

onMounted(() => {
  loadFiles()
  // Lazy-load trash count so the badge in the toolbar is accurate
  refreshTrashCount()
})

// ── Formatters ─────────────────────────────────────────────────────
function fmtSize(bytes) {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

function fmtDate(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleString()
  } catch {
    return iso
  }
}
</script>

<style scoped>
.pfb {
  position: relative;
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
  background: var(--surface, #fff);
  overflow: hidden;
}

.pfb--drag-over {
  outline: 2px dashed var(--accent, #111827);
  outline-offset: -4px;
}

.pfb--readonly {
  opacity: 0.95;
}

.pfb__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border, #e5e7eb);
  background: var(--surface-muted, #fafafa);
  flex-wrap: wrap;
}

.pfb__actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.pfb__drop-overlay {
  position: absolute;
  inset: 0;
  background: rgba(17, 24, 39, 0.06);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text, #111827);
  pointer-events: none;
  z-index: 5;
}

.pfb__inline-create {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: var(--surface-muted, #fafafa);
  border-bottom: 1px solid var(--border, #e5e7eb);
  color: var(--text-muted, #6b7280);
  font-size: 13px;
}

.pfb__inline-input {
  flex: 1;
  height: 26px;
  padding: 0 8px;
  border: 1px solid var(--accent, #111827);
  border-radius: 4px;
  font-size: 13px;
  font-family: inherit;
  background: white;
}

.pfb__body {
  flex: 1;
  overflow-y: auto;
}

.pfb__state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 64px 16px;
  color: var(--text-muted, #6b7280);
  text-align: center;
}

.pfb__state--error {
  color: var(--danger, #b91c1c);
}

.pfb__state-hint {
  font-size: 12px;
  opacity: 0.75;
  max-width: 360px;
}

.pfb__skeleton {
  width: 95%;
  height: 36px;
  margin: 6px auto;
  border-radius: 4px;
}

.pfb__list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.pfb__row {
  display: grid;
  grid-template-columns: 24px 1fr auto auto auto;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-soft, #f3f4f6);
  cursor: default;
  font-size: 13px;
}

.pfb__row--dir {
  cursor: pointer;
}

.pfb__row:hover {
  background: var(--surface-muted, #fafafa);
}

.pfb__row--trash {
  cursor: default;
}

.pfb__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted, #6b7280);
}

.pfb__row--dir .pfb__icon {
  color: var(--accent, #111827);
}

.pfb__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}

.pfb__size,
.pfb__date {
  color: var(--text-muted, #6b7280);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.pfb__actions-cell {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  background: transparent;
  border: 0;
  border-radius: 4px;
  color: var(--text-muted, #6b7280);
  cursor: pointer;
}

.icon-btn:hover:not(:disabled) {
  background: var(--surface-muted, #f3f4f6);
  color: var(--text, #111827);
}

.icon-btn--danger:hover:not(:disabled) {
  background: rgba(220, 38, 38, 0.08);
  color: var(--danger, #b91c1c);
}

.icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 6px;
  background: var(--surface, #fff);
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  color: var(--text, #111827);
}

.btn:hover:not(:disabled) {
  background: var(--surface-muted, #f9fafb);
}

.btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.btn--ghost {
  background: transparent;
}

.btn--active {
  background: var(--accent, #111827);
  border-color: var(--accent, #111827);
  color: white;
}

.btn--active:hover:not(:disabled) {
  background: var(--accent-hover, #000);
}

.btn--danger {
  background: rgba(220, 38, 38, 0.08);
  border-color: rgba(220, 38, 38, 0.3);
  color: var(--danger, #b91c1c);
}

.btn--danger:hover:not(:disabled) {
  background: rgba(220, 38, 38, 0.16);
}
</style>
