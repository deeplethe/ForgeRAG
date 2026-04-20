<template>
  <div class="file-list" @contextmenu.prevent="$emit('context-menu', { x: $event.clientX, y: $event.clientY, item: null })">
    <table class="w-full text-[11px]">
      <thead>
        <tr class="text-t3">
          <th @click="toggleSort('name')" class="list-th list-th--clickable">
            Name <span v-if="sortKey === 'name'">{{ sortDir === 1 ? '▲' : '▼' }}</span>
          </th>
          <th>Type</th>
          <th @click="toggleSort('size')" class="list-th list-th--clickable">
            Size <span v-if="sortKey === 'size'">{{ sortDir === 1 ? '▲' : '▼' }}</span>
          </th>
          <th>Path</th>
          <th @click="toggleSort('modified')" class="list-th list-th--clickable">
            Modified <span v-if="sortKey === 'modified'">{{ sortDir === 1 ? '▲' : '▼' }}</span>
          </th>
        </tr>
      </thead>
      <tbody>
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
          <td class="text-t3 truncate max-w-[240px]">{{ f.path }}</td>
          <td>—</td>
        </tr>
        <tr
          v-for="d in sortedDocuments"
          :key="'d:' + d.doc_id"
          class="list-row"
          :class="{ 'list-row--selected': isSelected('d:' + d.doc_id) }"
          draggable="true"
          @click.stop="onSelect('d:' + d.doc_id, $event)"
          @dblclick.stop="$emit('open-document', d)"
          @contextmenu.prevent.stop="onContext($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
          @dragstart="onDragStart($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
        >
          <td>📄 {{ d.filename || d.file_name || d.doc_id }}</td>
          <td>{{ (d.format || 'unknown').toUpperCase() }}</td>
          <td>{{ fmtSize(d.file_size_bytes) }}</td>
          <td class="text-t3 truncate max-w-[240px]">{{ d.path }}</td>
          <td>{{ fmtDate(d.updated_at || d.created_at) }}</td>
        </tr>
        <tr v-if="!folders.length && !documents.length">
          <td colspan="5" class="list-empty">This folder is empty.</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  folders: { type: Array, default: () => [] },
  documents: { type: Array, default: () => [] },
  selection: { type: Set, required: true },
})
const emit = defineEmits([
  'select', 'open-folder', 'open-document', 'context-menu', 'drag-start',
])

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
.file-list { padding: 8px 16px; }
.file-list table { border-collapse: collapse; }
.list-th { text-align: left; padding: 6px 8px; font-weight: 500; font-size: 10px; }
.list-th--clickable { cursor: pointer; user-select: none; }
.list-th--clickable:hover { color: var(--color-t1); }
.list-row { cursor: pointer; color: var(--color-t2); }
.list-row td { padding: 6px 8px; border-top: 1px solid var(--color-line); }
.list-row:hover { background: var(--color-bg2); color: var(--color-t1); }
.list-row--selected {
  background: color-mix(in srgb, var(--color-brand) 16%, var(--color-bg));
  color: var(--color-t1);
}
.list-empty { padding: 32px; text-align: center; color: var(--color-t3); }
</style>
