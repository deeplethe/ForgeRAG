<template>
  <div class="file-grid" @contextmenu.prevent="$emit('context-menu', { x: $event.clientX, y: $event.clientY, item: null })">
    <!-- Folders first -->
    <div
      v-for="f in folders"
      :key="'f:' + f.folder_id"
      class="file-card"
      :class="{ 'file-card--selected': isSelected('f:' + f.folder_id) }"
      :data-selkey="'f:' + f.folder_id"
      draggable="true"
      @click.stop="onSelect('f:' + f.folder_id, $event)"
      @dblclick.stop="$emit('open-folder', f.path)"
      @contextmenu.prevent.stop="onContext($event, { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name })"
      @dragstart="onDragStart($event, { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name })"
      @dragover.prevent
      @drop.prevent="onDropOntoFolder($event, f)"
    >
      <div class="file-card__icon">📁</div>
      <div class="file-card__title" :title="f.name">{{ f.name }}</div>
      <div class="file-card__meta">{{ f.document_count }} docs · {{ f.child_folders }} subfolders</div>
    </div>

    <!-- Then documents -->
    <div
      v-for="d in documents"
      :key="'d:' + d.doc_id"
      class="file-card"
      :class="{ 'file-card--selected': isSelected('d:' + d.doc_id) }"
      :data-selkey="'d:' + d.doc_id"
      draggable="true"
      @click.stop="onSelect('d:' + d.doc_id, $event)"
      @dblclick.stop="$emit('open-document', d)"
      @contextmenu.prevent.stop="onContext($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
      @dragstart="onDragStart($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
    >
      <div class="file-card__icon">📄</div>
      <div class="file-card__title" :title="d.filename || d.file_name">
        {{ d.filename || d.file_name || d.doc_id }}
      </div>
      <div class="file-card__meta">
        <template v-if="d.file_size_bytes">{{ fmtSize(d.file_size_bytes) }}</template>
        <template v-else-if="d.format">{{ d.format }}</template>
      </div>
    </div>

    <!-- Empty state -->
    <div v-if="!folders.length && !documents.length" class="file-grid__empty">
      This folder is empty.
    </div>
  </div>
</template>

<script setup>
const props = defineProps({
  folders: { type: Array, default: () => [] },
  documents: { type: Array, default: () => [] },
  selection: { type: Set, required: true },
})
const emit = defineEmits([
  'select', 'open-folder', 'open-document', 'context-menu', 'drop-onto-folder', 'drag-start',
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
  // Include the current selection if this card is part of it — otherwise just itself.
  const key = item.type === 'folder' ? 'f:' + item.folder_id : 'd:' + item.doc_id
  const selectedKeys = props.selection.has(key) && props.selection.size > 1
    ? [...props.selection]
    : [key]
  const payload = JSON.stringify({ items: [item], keys: selectedKeys })
  event.dataTransfer.setData('application/x-forgerag-item', payload)
  event.dataTransfer.effectAllowed = 'move'
  emit('drag-start', { items: [item], keys: selectedKeys })
}

function onDropOntoFolder(event, folder) {
  const raw = event.dataTransfer.getData('application/x-forgerag-item')
  if (!raw) return
  let payload
  try { payload = JSON.parse(raw) } catch { return }
  emit('drop-onto-folder', { items: payload.items, targetPath: folder.path })
}

function fmtSize(n) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`
}
</script>

<style scoped>
.file-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px;
  padding: 16px;
  align-content: start;
  min-height: 200px;
}
.file-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 12px 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
  user-select: none;
}
.file-card:hover { background: var(--color-bg2); }
.file-card--selected {
  background: color-mix(in srgb, var(--color-brand) 18%, var(--color-bg));
  border-color: var(--color-brand);
}
.file-card__icon { font-size: 32px; line-height: 1; }
.file-card__title {
  font-size: 11px;
  color: var(--color-t1);
  text-align: center;
  width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  word-break: break-word;
}
.file-card__meta {
  font-size: 9px;
  color: var(--color-t3);
  text-align: center;
}
.file-grid__empty {
  grid-column: 1 / -1;
  text-align: center;
  padding: 48px 16px;
  font-size: 12px;
  color: var(--color-t3);
}
</style>
