<template>
  <div class="file-list" @contextmenu.prevent="onBackgroundContext">
    <div
      v-if="loading && !rows.length"
      class="file-list__loading"
    >Loading…</div>
    <table class="w-full text-[11px]">
      <colgroup>
        <col class="col-name" />
        <col v-if="cols.has('type')" class="col-type" />
        <col v-if="cols.has('size')" class="col-size" />
        <col v-if="cols.has('created')" class="col-created" />
        <col v-if="cols.has('modified')" class="col-modified" />
        <col v-if="$slots['row-actions']" class="col-actions" />
      </colgroup>
      <thead>
        <tr class="text-t3">
          <th
            class="list-th list-th--clickable"
            @click="toggleSort('name')"
          >Name<span class="sort-caret">{{ caret('name') }}</span></th>
          <th v-if="cols.has('type')" class="list-th">Type</th>
          <th
            v-if="cols.has('size')"
            class="list-th list-th--clickable"
            @click="toggleSort('size')"
          >Size<span class="sort-caret">{{ caret('size') }}</span></th>
          <th
            v-if="cols.has('created')"
            class="list-th list-th--clickable"
            @click="toggleSort('created')"
          >Created<span class="sort-caret">{{ caret('created') }}</span></th>
          <th
            v-if="cols.has('modified')"
            class="list-th list-th--clickable"
            @click="toggleSort('modified')"
          >Modified<span class="sort-caret">{{ caret('modified') }}</span></th>
          <th v-if="$slots['row-actions']" class="list-th"></th>
        </tr>
      </thead>
      <tbody>
        <!-- Inline new-folder editor — appears at the top when ``creating``
             is true. Capability ``rename`` controls inline-rename, but
             this row uses its own ``creating`` toggle since there's no
             "old name" yet. -->
        <tr v-if="creating" class="list-row list-row--creating">
          <td>
            <div class="name-cell">
              <FileIcon kind="folder" :size="16" class="row-icon" />
              <input
                ref="newNameInput"
                type="text"
                class="list-name-input"
                placeholder="New folder"
                @keydown.enter.prevent="confirmCreate"
                @keydown.esc.prevent="$emit('cancel-create')"
                @blur="confirmCreate"
              />
            </div>
          </td>
          <td v-if="cols.has('type')">Folder</td>
          <td v-if="cols.has('size')">—</td>
          <td v-if="cols.has('created')">—</td>
          <td v-if="cols.has('modified')">—</td>
          <td v-if="$slots['row-actions']"></td>
        </tr>

        <tr
          v-for="row in sortedRows"
          :key="row.key"
          class="list-row"
          :class="{
            'list-row--selected': isSelected(row.key),
            'list-row--drop': dragOverKey === row.key,
          }"
          :data-selkey="row.key"
          :draggable="canDragRow(row)"
          @click.stop="onSelect(row.key, $event)"
          @dblclick.stop="onActivate(row)"
          @contextmenu.prevent.stop="onContext($event, row)"
          @dragstart="onDragStart($event, row)"
          @dragover.prevent="row.kind === 'folder' && capabilities.dragMove
                              ? onFolderDragOver($event, row) : null"
          @dragleave="row.kind === 'folder' ? onFolderDragLeave(row) : null"
          @drop.prevent="row.kind === 'folder' && capabilities.dragMove
                          ? onDropOntoFolder($event, row) : null"
        >
          <td>
            <div class="name-cell">
              <FileIcon
                :kind="row.kind"
                :name="row.kind === 'file' ? row.name : null"
                :size="16"
                class="row-icon"
              />
              <input
                v-if="isRenaming(row)"
                ref="renameInput"
                type="text"
                class="list-name-input"
                :value="row.name"
                @click.stop
                @dblclick.stop
                @keydown.enter.prevent="confirmRename(row)"
                @keydown.esc.prevent="cancelRename"
                @blur="confirmRename(row)"
              />
              <span v-else class="name-text">{{ row.name }}</span>
              <slot name="row-status" :row="row" />
            </div>
          </td>
          <td v-if="cols.has('type')">{{ formatType(row) }}</td>
          <td v-if="cols.has('size')">{{ row.kind === 'folder' ? '—' : fmtSize(row.size) }}</td>
          <td v-if="cols.has('created')">{{ fmtDate(row.createdAt) }}</td>
          <td v-if="cols.has('modified')">{{ fmtDate(row.modifiedAt) }}</td>
          <td v-if="$slots['row-actions']" class="row-actions">
            <slot name="row-actions" :row="row" />
          </td>
        </tr>

        <tr v-if="!loading && !rows.length && !creating">
          <td :colspan="totalColumns" class="list-empty">
            <slot name="empty">This folder is empty.</slot>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
/**
 * Generic file/folder table renderer.
 *
 * Both Library (indexed-corpus) and Workbench (filesystem) views feed
 * normalized ``FileRow`` objects (see ``./types.js``) into this
 * component. The renderer is data-source-agnostic: it doesn't know
 * about ``folder_id`` / ``doc_id`` / ingest status / file paths
 * having any particular shape — it only knows about ``row.key`` for
 * identity, ``row.kind`` for icon selection, and a small set of
 * common columns (name / type / size / created / modified).
 *
 * Domain-specific UI flows in via slots:
 *   * ``row-status`` — badges inside the name cell (Library uses
 *     this for ``failed`` / in-flight chips; Workbench passes
 *     nothing).
 *   * ``row-actions`` — right-most cell with hover-revealed icons
 *     (Library uses this for nothing today; Workbench wires up
 *     Open-chat / Download).
 *   * ``empty`` — empty-state copy.
 *
 * Capabilities the parent enables via the ``capabilities`` prop:
 *   ``select`` / ``multiSelect`` / ``rename`` / ``dragMove`` /
 *   ``contextMenu``. See ``./types.js::DEFAULT_CAPABILITIES``.
 *
 * The drag payload uses the MIME type
 * ``application/x-opencraig-files`` so any future drop target on
 * the page knows it's an OpenCraig file move (and not, say, an OS
 * file drop). The payload shape is
 * ``{ items: [{key, kind, path, name}], keys: [...] }`` —
 * downstream parsers should be defensive about extra fields the
 * future may add.
 */
import { computed, nextTick, ref, watch } from 'vue'

import FileIcon from '@/components/workspace/FileIcon.vue'
import {
  DEFAULT_CAPABILITIES,
  DEFAULT_FILE_TABLE_COLUMNS,
} from './types.js'

const props = defineProps({
  rows: { type: Array, default: () => [] },
  selection: { type: Set, required: true },
  loading: { type: Boolean, default: false },
  creating: { type: Boolean, default: false },
  // ``row.key`` of the row currently being renamed (or '' when no
  // rename is active). Parent owns this state because it knows when
  // the user invoked rename via the context menu.
  renamingKey: { type: String, default: '' },
  columns: { type: Array, default: () => [...DEFAULT_FILE_TABLE_COLUMNS] },
  capabilities: {
    type: Object,
    default: () => ({ ...DEFAULT_CAPABILITIES }),
  },
})

const emit = defineEmits([
  'select',
  'open-row',
  'context-menu',
  'drag-start',
  'drop-onto-folder',
  'confirm-create',
  'cancel-create',
  'confirm-rename',
  'cancel-rename',
])

const cols = computed(() => new Set(props.columns))
const totalColumns = computed(() => {
  // 1 (name) + each optional column + actions slot if present
  let n = 1
  for (const c of ['type', 'size', 'created', 'modified']) {
    if (cols.value.has(c)) n++
  }
  return n
})

// Inline-create autofocus
const newNameInput = ref(null)
watch(() => props.creating, async (active) => {
  if (!active) return
  await nextTick()
  newNameInput.value?.focus()
})

let _confirmFired = false
function confirmCreate() {
  if (_confirmFired) return
  _confirmFired = true
  const v = newNameInput.value?.value || ''
  emit('confirm-create', v)
  setTimeout(() => { _confirmFired = false }, 0)
}

// Inline rename — ``renameInput`` is a v-for ref array but only one
// row mounts an input at a time (the one whose key matches
// ``renamingKey``). Pulling element zero is enough.
const renameInput = ref(null)
function isRenaming(row) {
  return !!props.capabilities.rename && props.renamingKey === row.key
}
watch(() => props.renamingKey, async (key) => {
  if (!key) return
  await nextTick()
  const el = Array.isArray(renameInput.value) ? renameInput.value[0] : renameInput.value
  if (!el) return
  el.focus()
  el.select()
})

let _renameFired = false
function confirmRename(row) {
  if (_renameFired) return
  _renameFired = true
  const el = Array.isArray(renameInput.value) ? renameInput.value[0] : renameInput.value
  const newName = el?.value || ''
  emit('confirm-rename', { key: row.key, oldName: row.name, newName })
  setTimeout(() => { _renameFired = false }, 0)
}
function cancelRename() {
  // Trip the guard so the blur-on-unmount doesn't fire confirm.
  _renameFired = true
  emit('cancel-rename')
  setTimeout(() => { _renameFired = false }, 0)
}

function isSelected(key) { return props.selection.has(key) }
function onSelect(key, event) {
  if (!props.capabilities.select) return
  const additive =
    !!props.capabilities.multiSelect &&
    (event.metaKey || event.ctrlKey || event.shiftKey)
  emit('select', { key, additive })
}
function onActivate(row) {
  if (isRenaming(row)) return
  emit('open-row', row)
}
function onContext(event, row) {
  if (!props.capabilities.contextMenu) return
  emit('context-menu', { x: event.clientX, y: event.clientY, row })
}
function onBackgroundContext(event) {
  if (!props.capabilities.contextMenu) return
  emit('context-menu', { x: event.clientX, y: event.clientY, row: null })
}

function canDragRow(row) {
  return !!props.capabilities.dragMove && !isRenaming(row)
}

function onDragStart(event, row) {
  if (!canDragRow(row)) return
  const item = { key: row.key, kind: row.kind, path: row.path, name: row.name }
  // If the dragged row is part of a multi-row selection, ship them
  // all so the drop handler can decide whether to move 1 or many.
  const selectedKeys = props.selection.has(row.key) && props.selection.size > 1
    ? [...props.selection]
    : [row.key]
  const payload = JSON.stringify({ items: [item], keys: selectedKeys })
  event.dataTransfer.setData('application/x-opencraig-files', payload)
  event.dataTransfer.effectAllowed = 'move'
  emit('drag-start', { items: [item], keys: selectedKeys })
}

// Folder rows accept drops as move-targets. Single-key highlight
// state — only one row glows at a time.
const dragOverKey = ref('')
function onFolderDragOver(e, row) {
  if (e.dataTransfer.types.includes('application/x-opencraig-files')) {
    e.dataTransfer.dropEffect = 'move'
    dragOverKey.value = row.key
  }
}
function onFolderDragLeave(row) {
  if (dragOverKey.value === row.key) dragOverKey.value = ''
}
function onDropOntoFolder(event, folderRow) {
  dragOverKey.value = ''
  const raw = event.dataTransfer.getData('application/x-opencraig-files')
  if (!raw) return
  let payload
  try { payload = JSON.parse(raw) } catch { return }
  const items = Array.isArray(payload?.items) ? payload.items : []
  // Self-drop (folder onto itself) — silently ignore; the server
  // would reject anyway, no need to round-trip.
  if (items.some(it => it?.kind === 'folder' && it?.path === folderRow.path)) return
  emit('drop-onto-folder', { items, targetRow: folderRow })
}

// Sorting — same affordance as the legacy FileList: click a header
// once for asc, twice for desc, click another header to switch.
const sortKey = ref('name')
const sortDir = ref(1)
function toggleSort(key) {
  if (sortKey.value === key) sortDir.value = -sortDir.value
  else { sortKey.value = key; sortDir.value = 1 }
}
function caret(key) {
  if (sortKey.value !== key) return ''
  return sortDir.value === 1 ? '▲' : '▼'
}

const sortedRows = computed(() => {
  // Folders always rank above files, then sort within each kind.
  const folders = props.rows.filter(r => r.kind === 'folder')
  const files = props.rows.filter(r => r.kind === 'file')
  const cmp = sortFn(sortKey.value, sortDir.value)
  return [...folders.sort(cmp), ...files.sort(cmp)]
})

function sortFn(key, dir) {
  return (a, b) => {
    let v = 0
    if (key === 'name') v = (a.name || '').localeCompare(b.name || '')
    else if (key === 'size') v = (a.size || 0) - (b.size || 0)
    else if (key === 'created') v = new Date(a.createdAt || 0) - new Date(b.createdAt || 0)
    else if (key === 'modified') v = new Date(a.modifiedAt || 0) - new Date(b.modifiedAt || 0)
    return v * dir
  }
}

function fmtSize(n) {
  if (n == null) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`
}
function fmtDate(d) {
  if (!d) return '—'
  try { return new Date(d).toLocaleString() } catch { return d }
}
// Type column derives from the filename extension (Windows-Explorer
// style) rather than any domain-specific format field. Keeps the
// displayed type aligned with the icon (also derived from filename).
function formatType(row) {
  if (row.kind === 'folder') return 'Folder'
  const m = (row.name || '').match(/\.([^.]+)$/)
  return m ? m[1].toUpperCase() : '—'
}
</script>

<style scoped>
.file-list {
  position: relative;
  padding: 8px 16px;
  min-height: 160px;
  user-select: none;
}
.file-list table {
  border-collapse: collapse;
  table-layout: fixed;
  min-width: 686px;
}
.file-list__loading {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  font-size: 11px;
  color: var(--color-t3);
  letter-spacing: 0.02em;
  animation: fl-loading-pulse 1.4s ease-in-out infinite;
  pointer-events: none;
}
@keyframes fl-loading-pulse {
  0%, 100% { opacity: 0.45; }
  50%      { opacity: 0.9; }
}

.col-name      { width: auto; }
.col-type      { width: 90px; }
.col-size      { width: 96px; }
.col-created   { width: 150px; }
.col-modified  { width: 150px; }
.col-actions   { width: 56px; }

.list-th {
  text-align: left;
  padding: 6px 8px;
  font-weight: 400;
  font-size: 10px;
  color: var(--color-t3);
  white-space: nowrap;
}
.list-th--clickable { cursor: pointer; user-select: none; }
.list-th--clickable:hover { color: var(--color-t1); }

.sort-caret {
  display: inline-block;
  width: 10px;
  margin-left: 4px;
  text-align: center;
  font-size: 9px;
}

.list-row { cursor: pointer; color: var(--color-t2); }
.list-row td {
  padding: 6px 8px;
  border-top: 1px solid var(--color-line);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.list-row:hover { background: var(--color-bg3); color: var(--color-t1); }
.list-row--selected {
  background: var(--color-bg-selected);
  color: var(--color-t1);
}
.list-row--selected:hover {
  background: color-mix(in srgb, var(--color-bg-selected) 75%, var(--color-bg3));
}
.list-row--drop,
.list-row--drop.list-row--selected {
  background: color-mix(in srgb, var(--color-t1) 10%, transparent);
  box-shadow:
    inset 0 1px 0 var(--color-t1),
    inset 0 -1px 0 var(--color-t1);
  color: var(--color-t1);
}
.list-empty {
  padding: 32px;
  text-align: center;
  color: var(--color-t3);
}

.list-row--creating,
.list-row--creating:hover { background: var(--color-bg3); }

.name-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.name-cell .row-icon {
  flex-shrink: 0;
}
.name-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.list-name-input {
  flex: 1;
  min-width: 0;
  max-width: 240px;
  padding: 0 6px;
  font-size: 11px;
  line-height: inherit;
  color: var(--color-t1);
  background: var(--color-bg);
  border: none;
  border-radius: var(--r-sm);
  outline: 1px solid var(--color-line2);
  box-shadow: var(--ring-focus);
}

.row-actions {
  text-align: right;
}
</style>
