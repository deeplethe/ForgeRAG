<template>
  <div class="file-list" @contextmenu.prevent="$emit('context-menu', { x: $event.clientX, y: $event.clientY, item: null })">
    <!-- Tiny centered loading hint — replaces skeleton rows. Absolute over
         the table so it floats in the middle of an otherwise-empty list. -->
    <div
      v-if="loading && !folders.length && !documents.length"
      class="file-list__loading"
    >Loading…</div>
    <table class="w-full text-[11px]">
      <colgroup>
        <col class="col-name" />
        <col class="col-type" />
        <col class="col-size" />
        <col class="col-created" />
        <col class="col-modified" />
      </colgroup>
      <thead>
        <tr class="text-t3">
          <th @click="toggleSort('name')" class="list-th list-th--clickable">
            Name<span class="sort-caret">{{ sortKey === 'name' ? (sortDir === 1 ? '▲' : '▼') : '' }}</span>
          </th>
          <th class="list-th">Type</th>
          <th @click="toggleSort('size')" class="list-th list-th--clickable">
            Size<span class="sort-caret">{{ sortKey === 'size' ? (sortDir === 1 ? '▲' : '▼') : '' }}</span>
          </th>
          <th @click="toggleSort('created')" class="list-th list-th--clickable">
            Created<span class="sort-caret">{{ sortKey === 'created' ? (sortDir === 1 ? '▲' : '▼') : '' }}</span>
          </th>
          <th @click="toggleSort('modified')" class="list-th list-th--clickable">
            Modified<span class="sort-caret">{{ sortKey === 'modified' ? (sortDir === 1 ? '▲' : '▼') : '' }}</span>
          </th>
        </tr>
      </thead>
      <tbody>
        <!-- Inline new-folder editor (Windows-style) — appears at top of list -->
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
          <td>Folder</td>
          <td>—</td>
          <td>—</td>
          <td>—</td>
        </tr>
        <tr
          v-for="f in sortedFolders"
          :key="'f:' + f.folder_id"
          class="list-row"
          :class="{ 'list-row--selected': isSelected('f:' + f.folder_id) }"
          :data-selkey="'f:' + f.folder_id"
          :draggable="!isRenaming(f)"
          @click.stop="onSelect('f:' + f.folder_id, $event)"
          @dblclick.stop="onFolderDblClick(f)"
          @contextmenu.prevent.stop="onContext($event, { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name })"
          @dragstart="onDragStart($event, { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name })"
        >
          <td>
            <div class="name-cell">
              <FileIcon kind="folder" :size="16" class="row-icon" />
              <input
                v-if="isRenaming(f)"
                ref="renameInput"
                type="text"
                class="list-name-input"
                :value="f.name"
                @click.stop
                @dblclick.stop
                @keydown.enter.prevent="confirmRename(f)"
                @keydown.esc.prevent="cancelRename"
                @blur="confirmRename(f)"
              />
              <span v-else class="name-text">{{ f.name }}</span>
            </div>
          </td>
          <td>Folder</td>
          <td>—</td>
          <td>—</td>
          <td>—</td>
        </tr>
        <tr
          v-for="d in sortedDocuments"
          :key="'d:' + d.doc_id"
          class="list-row"
          :class="{
            'list-row--selected': isSelected('d:' + d.doc_id),
            'list-row--error': d.status === 'error',
          }"
          :data-selkey="'d:' + d.doc_id"
          :draggable="!isRenamingDoc(d)"
          @click.stop="onSelect('d:' + d.doc_id, $event)"
          @dblclick.stop="onDocDblClick(d)"
          @contextmenu.prevent.stop="onContext($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
          @dragstart="onDragStart($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
        >
          <td>
            <div class="name-cell">
              <FileIcon kind="file" :name="d.filename || d.file_name" :size="16" class="row-icon" />
              <input
                v-if="isRenamingDoc(d)"
                ref="renameInput"
                type="text"
                class="list-name-input"
                :value="d.filename || d.file_name || ''"
                @click.stop
                @dblclick.stop
                @keydown.enter.prevent="confirmRenameDoc(d)"
                @keydown.esc.prevent="cancelRename"
                @blur="confirmRenameDoc(d)"
              />
              <span v-else class="name-text">{{ d.filename || d.file_name || d.doc_id }}</span>
              <span
                v-if="d.status === 'error'"
                class="status-chip status-chip--error"
                :title="d.error_message || 'Ingestion failed'"
              >failed</span>
              <span
                v-else-if="d.status && !['ready', 'error'].includes(d.status)"
                class="status-chip status-chip--pending"
                :title="d.status"
              >{{ d.status }}</span>
            </div>
          </td>
          <td>{{ fmtType(d.filename || d.file_name) }}</td>
          <td>{{ fmtSize(d.file_size_bytes) }}</td>
          <td>{{ fmtDate(d.created_at) }}</td>
          <td>{{ fmtDate(d.updated_at || d.created_at) }}</td>
        </tr>
        <tr v-if="!loading && !folders.length && !documents.length">
          <td colspan="5" class="list-empty">This folder is empty.</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'

import FileIcon from './FileIcon.vue'

const props = defineProps({
  folders: { type: Array, default: () => [] },
  documents: { type: Array, default: () => [] },
  selection: { type: Set, required: true },
  loading: { type: Boolean, default: false },
  creating: { type: Boolean, default: false },
  // Selection-key of the row being renamed ("f:abc-123") or '' when idle.
  renamingKey: { type: String, default: '' },
})
const emit = defineEmits([
  'select', 'open-folder', 'open-document', 'context-menu', 'drag-start',
  'confirm-create', 'cancel-create',
  'confirm-rename', 'cancel-rename',
])

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

// Inline rename — mirrors create. v-for ref collects an array but only
// the matching row mounts an input. Folder + document share the input
// pool because at most one is active at a time.
const renameInput = ref(null)
function isRenaming(f) { return props.renamingKey === 'f:' + f.folder_id }
function isRenamingDoc(d) { return props.renamingKey === 'd:' + d.doc_id }
watch(() => props.renamingKey, async (key) => {
  if (!key) return
  await nextTick()
  const el = Array.isArray(renameInput.value) ? renameInput.value[0] : renameInput.value
  if (!el) return
  el.focus()
  el.select()
})

let _renameFired = false
function _emitConfirm(key, oldName) {
  if (_renameFired) return
  _renameFired = true
  const el = Array.isArray(renameInput.value) ? renameInput.value[0] : renameInput.value
  const v = el?.value || ''
  emit('confirm-rename', { key, oldName, newName: v })
  setTimeout(() => { _renameFired = false }, 0)
}
function confirmRename(f) { _emitConfirm('f:' + f.folder_id, f.name) }
function confirmRenameDoc(d) {
  _emitConfirm('d:' + d.doc_id, d.filename || d.file_name || '')
}
function cancelRename() {
  // Trip the guard so the blur-on-unmount doesn't fire confirm
  _renameFired = true
  emit('cancel-rename')
  setTimeout(() => { _renameFired = false }, 0)
}

function onFolderDblClick(f) {
  if (isRenaming(f)) return
  emit('open-folder', f.path)
}
function onDocDblClick(d) {
  if (isRenamingDoc(d)) return
  emit('open-document', d)
}

function isSelected(key) { return props.selection.has(key) }
function onSelect(key, event) {
  const additive = event.metaKey || event.ctrlKey || event.shiftKey
  emit('select', { key, additive })
}
function onContext(event, item) {
  emit('context-menu', { x: event.clientX, y: event.clientY, item })
}
function onDragStart(event, item) {
  const key = item.type === 'folder' ? 'f:' + item.folder_id : 'd:' + item.doc_id
  const selectedKeys = props.selection.has(key) && props.selection.size > 1
    ? [...props.selection]
    : [key]
  const payload = JSON.stringify({ items: [item], keys: selectedKeys })
  event.dataTransfer.setData('application/x-forgerag-item', payload)
  event.dataTransfer.effectAllowed = 'move'
  emit('drag-start', { items: [item], keys: selectedKeys })
}

const sortKey = ref('name')
const sortDir = ref(1)
function toggleSort(key) {
  if (sortKey.value === key) sortDir.value = -sortDir.value
  else { sortKey.value = key; sortDir.value = 1 }
}

const sortedFolders = computed(() => [...props.folders].sort((a, b) => cmp(a.name, b.name) * sortDir.value))
const sortedDocuments = computed(() => {
  return [...props.documents].sort((a, b) => {
    const k = sortKey.value
    if (k === 'name') return cmp(a.filename || a.file_name || '', b.filename || b.file_name || '') * sortDir.value
    if (k === 'size') return (n(a.file_size_bytes) - n(b.file_size_bytes)) * sortDir.value
    if (k === 'created') return (new Date(a.created_at || 0) - new Date(b.created_at || 0)) * sortDir.value
    if (k === 'modified') return (new Date(a.updated_at || 0) - new Date(b.updated_at || 0)) * sortDir.value
    return 0
  })
})
function cmp(a, b) { return (a || '').localeCompare(b || '') }
function n(v) { return v == null ? 0 : +v }

function fmtSize(n) {
  if (!n) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`
}
function fmtDate(d) {
  if (!d) return '—'
  try { return new Date(d).toLocaleString() } catch { return d }
}
// Type column reads from the filename extension (Windows-Explorer style)
// rather than ``Document.format``. Keeps the displayed type aligned with
// the icon (which also derives from filename) and stays correct even when
// the parser writes a post-conversion format to the DB.
function fmtType(name) {
  const m = (name || '').match(/\.([^.]+)$/)
  return m ? m[1].toUpperCase() : '—'
}
</script>

<style scoped>
.file-list {
  position: relative;        /* anchor for the absolute loading hint */
  padding: 8px 16px;
  min-height: 160px;
  /* Suppress native text selection in the list. Without this, dragging
     a marquee across the header (or any cell text) paints the browser's
     blue text-selection over "Type"/"Modified"/etc. The grid view uses
     the same trick on .file-card. Inputs ignore this on purpose. */
  user-select: none;
}
.file-list table {
  border-collapse: collapse;
  table-layout: fixed;
  /* Sum of fixed cols (90+96+150+150 = 486) + 200 min for the
     auto-width Name column. Without this, narrow viewports + a wide
     sidebar squeeze Name down to ~6px and the inline rename / create
     input collapses to a sliver — visually it looks like "..." next
     to the icon because there's no room left for the input box.
     The marquee-container already has overflow: auto, so the table
     scrolls horizontally when the viewport is narrower than this. */
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

/* Fixed column widths keep header and body cells in the same vertical
   tracks regardless of content length. `Name` gets the flexible auto
   column; everything else is fixed. */
.col-name      { width: auto; }
.col-type      { width: 90px; }
.col-size      { width: 96px; }
.col-created   { width: 150px; }
.col-modified  { width: 150px; }

.list-th {
  text-align: left;
  padding: 6px 8px;
  font-weight: 400;         /* override browser default <th> bold */
  font-size: 10px;
  color: var(--color-t3);
  white-space: nowrap;
}
.list-th--clickable { cursor: pointer; user-select: none; }
.list-th--clickable:hover { color: var(--color-t1); }

/* Reserve horizontal space for the sort caret so the header label
   never shifts when a column is (un)selected. */
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
/* Selected uses the heavier ``--color-bg-selected`` token so it
   reads as distinct from hover. Hover layered on top mixes the two
   for a slight cue that the row is being pointed at. */
.list-row--selected {
  background: var(--color-bg-selected);
  color: var(--color-t1);
}
.list-row--selected:hover {
  background: color-mix(in srgb, var(--color-bg-selected) 75%, var(--color-bg3));
}
.list-empty { padding: 32px; text-align: center; color: var(--color-t3); }

/* Inline new-folder editor row */
/* Creating row — neutral hover tint; the input's focus ring is the
   real cue, no need for a brand-coloured highlight on the row. */
.list-row--creating,
.list-row--creating:hover { background: var(--color-bg3); }

/* Name cell uses flex so the icon stays at its natural width and the
   input/text fills the remaining space. The previous inline-block +
   ``width: calc(100% - 28px)`` setup was fragile — the 100% reference
   for inline-block inside a table-cell + ``overflow: hidden /
   text-overflow: ellipsis / white-space: nowrap`` produced a sliver
   of an input on some browser/font combinations, with the
   user-visible result being just "..." next to the icon. */
.name-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;        /* let flex children actually shrink */
}
.name-cell .row-icon {
  flex-shrink: 0;      /* never squeeze the folder/file icon */
  margin-right: 0;     /* gap on the parent handles spacing */
}
.name-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.list-name-input {
  flex: 1;
  min-width: 0;        /* default flex min is auto = content width — without this the input refuses to shrink */
  max-width: 240px;    /* don't stretch across the full column on wide viewports */
  /* Vertical padding zero + ``outline`` instead of ``border`` so the
     input occupies the same vertical space as the plain ``{{ f.name }}``
     text. Keeps row height stable when entering / exiting rename mode
     (no row-height jitter). The outline supplies the visual border
     without taking layout space. */
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

.doc-icon { margin-right: 2px; }
/* Inline FileIcon sits slightly above text baseline; nudge it onto
   the centre line so the row reads as a single block. */
.row-icon { margin-right: 6px; vertical-align: -3px; }
.list-row--error td:first-child { color: var(--color-err-fg); }
.status-chip {
  display: inline-block;
  margin-left: 6px;
  padding: 0 5px;
  font-size: 9px;
  font-weight: 500;
  border-radius: var(--r-sm);
  cursor: help;
  vertical-align: middle;
}
.status-chip--error   { background: var(--color-err-bg); color: var(--color-err-fg); }
.status-chip--pending { background: var(--color-run-bg); color: var(--color-run-fg); }
</style>
