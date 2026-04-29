<template>
  <div class="file-grid" @contextmenu.prevent="$emit('context-menu', { x: $event.clientX, y: $event.clientY, item: null })">
    <!-- Tiny centered loading hint — replaces skeleton tiles. Absolute
         positioning so it floats over the (empty) grid without taking a
         cell. KeepAlive in App.vue means revisits skip this entirely. -->
    <div
      v-if="loading && !folders.length && !documents.length"
      class="file-grid__loading"
    >Loading…</div>

    <!-- Inline new-folder editor (Windows-style: ghost folder with name input) -->
    <div v-if="creating" class="file-card file-card--creating">
      <div class="file-card__icon"><FileIcon kind="folder" :size="36" /></div>
      <input
        ref="newNameInput"
        type="text"
        class="file-card__name-input"
        placeholder="New folder"
        @keydown.enter.prevent="confirmCreate"
        @keydown.esc.prevent="$emit('cancel-create')"
        @blur="confirmCreate"
      />
      <div class="file-card__meta">&nbsp;</div>
    </div>

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
      <div class="file-card__icon"><FileIcon kind="folder" :size="36" /></div>
      <div class="file-card__title" :title="f.name">{{ f.name }}</div>
      <div class="file-card__meta">{{ f.document_count }} docs · {{ f.child_folders }} subfolders</div>
    </div>

    <!-- Then documents -->
    <div
      v-for="d in documents"
      :key="'d:' + d.doc_id"
      class="file-card"
      :class="{
        'file-card--selected': isSelected('d:' + d.doc_id),
        'file-card--error': d.status === 'error',
        'file-card--pending': d.status && !['ready', 'error'].includes(d.status),
      }"
      :data-selkey="'d:' + d.doc_id"
      draggable="true"
      @click.stop="onSelect('d:' + d.doc_id, $event)"
      @dblclick.stop="$emit('open-document', d)"
      @contextmenu.prevent.stop="onContext($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
      @dragstart="onDragStart($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
    >
      <div class="file-card__icon">
        <FileIcon kind="file" :name="d.filename || d.file_name" :size="36" />
        <span
          v-if="d.status === 'error'"
          class="status-badge status-badge--error"
          :title="d.error_message || 'Ingestion failed'"
        >!</span>
        <span
          v-else-if="d.status && !['ready', 'error'].includes(d.status)"
          class="status-badge status-badge--pending"
          :title="d.status"
        >⟳</span>
      </div>
      <div class="file-card__title" :title="d.filename || d.file_name">
        {{ d.filename || d.file_name || d.doc_id }}
      </div>
      <div class="file-card__meta">
        <template v-if="d.status === 'error'">
          <span class="meta-error" :title="d.error_message || ''">failed</span>
        </template>
        <template v-else-if="d.file_size_bytes">{{ fmtSize(d.file_size_bytes) }}</template>
        <template v-else-if="d.format">{{ d.format }}</template>
      </div>
    </div>

    <!-- Empty state — hidden while loading so the skeleton has the floor -->
    <div v-if="!loading && !folders.length && !documents.length" class="file-grid__empty">
      This folder is empty.
    </div>
  </div>
</template>

<script setup>
import { nextTick, ref, watch } from 'vue'

import FileIcon from './FileIcon.vue'

const props = defineProps({
  folders: { type: Array, default: () => [] },
  documents: { type: Array, default: () => [] },
  selection: { type: Set, required: true },
  loading: { type: Boolean, default: false },
  creating: { type: Boolean, default: false },
})
const emit = defineEmits([
  'select', 'open-folder', 'open-document', 'context-menu', 'drop-onto-folder', 'drag-start',
  'confirm-create', 'cancel-create',
])

// Autofocus + select-all when entering "creating" mode
const newNameInput = ref(null)
watch(() => props.creating, async (active) => {
  if (!active) return
  await nextTick()
  newNameInput.value?.focus()
})

// Single-fire: blur AND Enter both call confirmCreate. Guard to avoid
// emitting twice for the same edit.
let _confirmFired = false
function confirmCreate() {
  if (_confirmFired) return
  _confirmFired = true
  const v = newNameInput.value?.value || ''
  emit('confirm-create', v)
  // reset for the next round
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
  position: relative;        /* anchor for the absolute loading hint */
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px;
  padding: 16px;
  align-content: start;
  min-height: 200px;
}
.file-grid__loading {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  grid-column: 1 / -1;       /* harmless if absolute removes it from grid */
  font-size: 11px;
  color: var(--color-t3);
  letter-spacing: 0.02em;
  animation: fg-loading-pulse 1.4s ease-in-out infinite;
  pointer-events: none;
}
@keyframes fg-loading-pulse {
  0%, 100% { opacity: 0.45; }
  50%      { opacity: 0.9; }
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
.file-card:hover { background: var(--color-bg3); }
/* Selected state — neutral gray (Vercel pattern), not branded.
   Both base + hover targeted so the bg doesn't disappear when hovering
   an already-selected card (CSS specificity: .class:hover beats .class). */
.file-card--selected,
.file-card--selected:hover {
  background: var(--color-bg3);
  border-color: var(--color-line2);
}
.file-card__icon {
  font-size: 32px;
  line-height: 1;
  position: relative;
}
.status-badge {
  position: absolute;
  right: -6px;
  bottom: -2px;
  width: 14px;
  height: 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  font-weight: 600;
  color: white;
  border-radius: 50%;
  line-height: 1;
  cursor: help;
}
.status-badge--error   { background: var(--color-err-fg); }
.status-badge--pending { background: var(--color-run-fg); font-size: 8px; }
.file-card--error .file-card__title { color: var(--color-err-fg); }
.meta-error { color: var(--color-err-fg); cursor: help; }

/* Inline new-folder editor — ghost card with text input as title */
.file-card--creating {
  border: 1px dashed var(--color-brand);
  background: color-mix(in srgb, var(--color-brand) 6%, transparent);
}
.file-card__name-input {
  width: 100%;
  padding: 2px 4px;
  font-size: 11px;
  text-align: center;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line2);
  border-radius: var(--r-sm);
  outline: none;
  box-shadow: var(--ring-focus);
}
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
