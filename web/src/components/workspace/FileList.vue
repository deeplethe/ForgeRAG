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
        <col class="col-path" />
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
          <th class="list-th">Path</th>
          <th @click="toggleSort('modified')" class="list-th list-th--clickable">
            Modified<span class="sort-caret">{{ sortKey === 'modified' ? (sortDir === 1 ? '▲' : '▼') : '' }}</span>
          </th>
        </tr>
      </thead>
      <tbody>
        <!-- Inline new-folder editor (Windows-style) — appears at top of list -->
        <tr v-if="creating" class="list-row list-row--creating">
          <td>
            <span class="doc-icon">📁</span>
            <input
              ref="newNameInput"
              type="text"
              class="list-name-input"
              placeholder="New folder"
              @keydown.enter.prevent="confirmCreate"
              @keydown.esc.prevent="$emit('cancel-create')"
              @blur="confirmCreate"
            />
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
          draggable="true"
          @click.stop="onSelect('f:' + f.folder_id, $event)"
          @dblclick.stop="$emit('open-folder', f.path)"
          @contextmenu.prevent.stop="onContext($event, { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name })"
          @dragstart="onDragStart($event, { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name })"
        >
          <td>📁 {{ f.name }}</td>
          <td>Folder</td>
          <td>—</td>
          <td class="path-cell">{{ f.path }}</td>
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
          draggable="true"
          @click.stop="onSelect('d:' + d.doc_id, $event)"
          @dblclick.stop="$emit('open-document', d)"
          @contextmenu.prevent.stop="onContext($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
          @dragstart="onDragStart($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
        >
          <td>
            <span class="doc-icon">📄</span>
            {{ d.filename || d.file_name || d.doc_id }}
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
          </td>
          <td>{{ (d.format || 'unknown').toUpperCase() }}</td>
          <td>{{ fmtSize(d.file_size_bytes) }}</td>
          <td class="path-cell">{{ d.path }}</td>
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

const props = defineProps({
  folders: { type: Array, default: () => [] },
  documents: { type: Array, default: () => [] },
  selection: { type: Set, required: true },
  loading: { type: Boolean, default: false },
  creating: { type: Boolean, default: false },
})
const emit = defineEmits([
  'select', 'open-folder', 'open-document', 'context-menu', 'drag-start',
  'confirm-create', 'cancel-create',
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
</script>

<style scoped>
.file-list {
  position: relative;        /* anchor for the absolute loading hint */
  padding: 8px 16px;
  min-height: 160px;
}
.file-list table { border-collapse: collapse; table-layout: fixed; }
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
.col-path      { width: 240px; }
.col-modified  { width: 160px; }

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
/* Selected state — neutral gray (Vercel pattern); double selector beats
   .list-row:hover specificity so hovering a selected row keeps its tint. */
.list-row--selected,
.list-row--selected:hover {
  background: var(--color-bg3);
  color: var(--color-t1);
}
.path-cell { color: var(--color-t3); }
.list-empty { padding: 32px; text-align: center; color: var(--color-t3); }

/* Inline new-folder editor row */
.list-row--creating { background: color-mix(in srgb, var(--color-brand) 6%, transparent); }
.list-row--creating:hover { background: color-mix(in srgb, var(--color-brand) 6%, transparent); }
.list-name-input {
  display: inline-block;
  width: calc(100% - 28px);
  margin-left: 4px;
  padding: 2px 6px;
  font-size: 11px;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line2);
  border-radius: var(--r-sm);
  outline: none;
  box-shadow: var(--ring-focus);
  vertical-align: middle;
}

.doc-icon { margin-right: 2px; }
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
