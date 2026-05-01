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
      :class="{
        'file-card--selected': isSelected('f:' + f.folder_id),
        'file-card--drop': dragOverKey === 'f:' + f.folder_id,
      }"
      :data-selkey="'f:' + f.folder_id"
      :draggable="!isRenaming(f)"
      @click.stop="onSelect('f:' + f.folder_id, $event)"
      @dblclick.stop="onFolderDblClick(f)"
      @contextmenu.prevent.stop="onContext($event, { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name })"
      @dragstart="onDragStart($event, { type: 'folder', folder_id: f.folder_id, path: f.path, name: f.name })"
      @dragover.prevent="onFolderDragOver($event, 'f:' + f.folder_id)"
      @dragleave="onFolderDragLeave('f:' + f.folder_id)"
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
      :draggable="!isRenamingDoc(d) && !isDocInFlight(d)"
      @click.stop="onSelect('d:' + d.doc_id, $event)"
      @dblclick.stop="onDocDblClick(d)"
      @contextmenu.prevent.stop="onContext($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name, inFlight: isDocInFlight(d) })"
      @dragstart="onDragStart($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
    >
      <div class="file-card__icon">
        <FileIcon kind="file" :name="d.filename || d.file_name" :size="36" />
        <!-- Error: tiny solid red dot. Carries colour because failure
             is the one state that must be impossible to miss. -->
        <span
          v-if="d.status === 'error'"
          class="status-badge status-badge--error"
          :title="d.error_message || 'Ingestion failed'"
        ></span>
        <!-- In-flight: bare spinner overlaid on the corner — no
             coloured disc behind it. Vercel-style: monochrome currentColor,
             only the motion conveys "active". -->
        <Loader2
          v-else-if="isDocInFlight(d)"
          class="status-spinner"
          :title="inFlightStage(d)"
          :size="11"
          :stroke-width="2"
        />
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
        <template v-else-if="isDocInFlight(d)">
          <!-- Plain inline label — Vercel-style: no chip, no fill,
               neutral muted color (the badge spinner above already
               signals motion). Lowercase to read as a status word
               rather than a button. -->
          <span class="meta-pending">{{ inFlightStage(d) }}</span>
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
import { Loader2 } from 'lucide-vue-next'

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
// In-flight = ANY of the four pipeline sub-states (parse, embed,
// enrich, kg) is still running. ``status`` covers parse-side phases
// (parsing/structuring/chunking); the other three are independent
// async jobs that finish AFTER ``status=ready``. A doc with
// ``status=ready`` but ``kg_status=running`` is still ingesting from
// the user's perspective — KG-driven features (graph view, kg-aware
// retrieval) won't see this doc until kg_status flips to ``done``.
const _DOC_TERMINAL_STATUSES = new Set(['ready', 'error'])
const _SUB_TERMINAL_STATUSES = new Set(['done', 'error', 'skipped', null, undefined, ''])
function _stageInFlight(s, terminalSet) {
  if (s == null) return false                  // not started → treat as terminal
  return !terminalSet.has(s)
}
function isDocInFlight(d) {
  return _stageInFlight(d.status, _DOC_TERMINAL_STATUSES)
      || _stageInFlight(d.embed_status, _SUB_TERMINAL_STATUSES)
      || _stageInFlight(d.enrich_status, _SUB_TERMINAL_STATUSES)
      || _stageInFlight(d.kg_status, _SUB_TERMINAL_STATUSES)
}
function inFlightStage(d) {
  // Pick the most informative non-terminal stage to surface in the
  // meta line. Order matters: parse → embed → enrich → kg.
  if (_stageInFlight(d.status, _DOC_TERMINAL_STATUSES)) return d.status
  if (_stageInFlight(d.embed_status, _SUB_TERMINAL_STATUSES)) return 'embedding'
  if (_stageInFlight(d.enrich_status, _SUB_TERMINAL_STATUSES)) return 'enriching'
  if (_stageInFlight(d.kg_status, _SUB_TERMINAL_STATUSES)) return 'building graph'
  return null
}
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

// Single-key drag-over state — at most one card can be the drop target
// at a time. A ref<string> is enough; using a Set would let stale
// ``dragenter`` events leave multiple cards highlighted if leave fires
// out of order, which is a real problem because dragenter/leave on a
// nested element can fire in either order.
const dragOverKey = ref('')

function onFolderDragOver(e, key) {
  if (e.dataTransfer.types.includes('application/x-forgerag-item')) {
    e.dataTransfer.dropEffect = 'move'
    dragOverKey.value = key
  }
}
function onFolderDragLeave(key) {
  if (dragOverKey.value === key) dragOverKey.value = ''
}

function onDropOntoFolder(event, folder) {
  dragOverKey.value = ''
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
/* Drop-target highlight — neutral white-on-dark / black-on-light pair
   keyed off ``--color-t1``. Shared visual contract with
   ``.tree-row--drop`` so the same gesture (dragging onto a folder)
   reads identically in the sidebar and the grid. */
.file-card--drop,
.file-card--drop.file-card--selected {
  background: color-mix(in srgb, var(--color-t1) 10%, transparent);
  outline: 1px solid var(--color-t1);
  outline-offset: -1px;
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
/* Bare spinner overlaid on the file icon's corner — amber so the
   in-flight cards pop visually against ready ones. ``--color-warn-fg``
   keeps it theme-adaptive (amber-400 dark / amber-700 light). */
.status-spinner {
  position: absolute;
  right: -4px;
  bottom: -4px;
  color: var(--color-warn-fg);
  background: var(--color-bg2);
  border-radius: 50%;
  padding: 1px;
  animation: status-spin 0.9s linear infinite;
}
@keyframes status-spin { to { transform: rotate(360deg); } }
.file-card--error .file-card__title { color: var(--color-err-fg); }
.meta-error { color: var(--color-err-fg); cursor: help; }
/* In-flight label — amber so it scans clearly amongst neutral file
   sizes. No italics: italic + tiny + amber together read as
   apologetic; amber alone is enough emphasis. */
.meta-pending {
  color: var(--color-warn-fg);
}

/* Inline new-folder editor — the input itself carries the focus ring,
   so the card stays on the same neutral tint as a hovered card.
   Earlier version had a brand-coloured dashed outline that read as a
   stray selection / drop-target cue against the otherwise grayscale
   workspace. */
.file-card--creating { background: var(--color-bg3); }
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
