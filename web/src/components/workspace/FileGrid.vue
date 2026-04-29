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
      :draggable="!isRenaming(f)"
      @click.stop="onSelect('f:' + f.folder_id, $event)"
      @dblclick.stop="onFolderDblClick(f)"
      @contextmenu.prevent.stop="onContext($event, { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name })"
      @dragstart="onDragStart($event, { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name })"
      @dragover.prevent
      @drop.prevent="onDropOntoFolder($event, f)"
    >
      <div class="file-card__icon"><FileIcon kind="folder" :size="36" /></div>
      <input
        v-if="isRenaming(f)"
        ref="renameInput"
        type="text"
        class="file-card__name-input"
        :value="f.name"
        @click.stop
        @dblclick.stop
        @keydown.enter.prevent="confirmRename(f)"
        @keydown.esc.prevent="cancelRename"
        @blur="confirmRename(f)"
      />
      <div v-else class="file-card__title" :title="f.name">{{ f.name }}</div>
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
      :draggable="!isRenamingDoc(d)"
      @click.stop="onSelect('d:' + d.doc_id, $event)"
      @dblclick.stop="onDocDblClick(d)"
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
      <input
        v-if="isRenamingDoc(d)"
        ref="renameInput"
        type="text"
        class="file-card__name-input"
        :value="d.filename || d.file_name || ''"
        @click.stop
        @dblclick.stop
        @keydown.enter.prevent="confirmRenameDoc(d)"
        @keydown.esc.prevent="cancelRename"
        @blur="confirmRenameDoc(d)"
      />
      <div v-else class="file-card__title" :title="d.filename || d.file_name">
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
  // Selection-key of the item currently being renamed (e.g. "f:abc-123"),
  // or empty string when no rename is in progress. Mirrors `creating` —
  // the parent owns the state; we just render the inline input.
  renamingKey: { type: String, default: '' },
})
const emit = defineEmits([
  'select', 'open-folder', 'open-document', 'context-menu', 'drop-onto-folder', 'drag-start',
  'confirm-create', 'cancel-create',
  'confirm-rename', 'cancel-rename',
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

// Inline rename — same shape as create. ``renameInput`` is a v-for ref;
// only one element across folders + documents ever satisfies the v-if so
// the array always has at most one entry.
const renameInput = ref(null)
function isRenaming(f) { return props.renamingKey === 'f:' + f.folder_id }
function isRenamingDoc(d) { return props.renamingKey === 'd:' + d.doc_id }
watch(() => props.renamingKey, async (key) => {
  if (!key) return
  await nextTick()
  // ``ref`` inside v-for collects an array; grab the only mounted input
  const el = Array.isArray(renameInput.value) ? renameInput.value[0] : renameInput.value
  if (!el) return
  el.focus()
  el.select()
})

// Same blur/Enter double-fire guard as create — shared across folder
// and document rename paths since at most one input is mounted at a time.
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
  // Esc — bypass the blur-fired confirm by tripping the guard before
  // the input loses focus (blur fires on unmount, hits the guard, no-op).
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
  /* Fixed-width tiles instead of ``1fr`` — stretching tiles to fill the
     row turns them into wide horizontal rectangles, which doesn't read
     as a "file". 112px gives a roughly square card (36 icon + 2-line
     title + meta) that matches how Finder/Explorer space their tiles. */
  grid-template-columns: repeat(auto-fill, 112px);
  justify-content: start;    /* leftover horizontal space sits on the right, not stretched between cards */
  gap: 8px;
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
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.12s;
  user-select: none;
}
.file-card:hover { background: var(--color-bg3); }
/* Selected state — neutral gray, no border. Selected uses
   ``--color-bg-selected`` (one step heavier than the hover token) so
   the user can clearly tell hover from selected. */
.file-card--selected { background: var(--color-bg-selected); }
/* Hover layered on top of selected — keep the cue visible by mixing
   a touch of the lighter hover tone into the selected colour. Resolves
   correctly in both light (selected darker → mix lifts it) and dark
   (selected lighter → mix dampens it) themes. */
.file-card--selected:hover {
  background: color-mix(in srgb, var(--color-bg-selected) 75%, var(--color-bg3));
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
