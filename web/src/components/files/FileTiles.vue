<template>
  <div class="file-grid" @contextmenu.prevent="onBackgroundContext">
    <div
      v-if="loading && !rows.length"
      class="file-grid__loading"
    >Loading…</div>

    <!-- Inline new-folder editor (ghost tile, name input). Capability
         ``rename`` controls inline-rename; the create flow uses its
         own ``creating`` toggle since there's no "old name" yet. -->
    <div v-if="creating" class="file-card file-card--creating">
      <div class="file-card__icon"><FileIcon kind="folder" variant="jumbo" :size="48" /></div>
      <input
        ref="newNameInput"
        type="text"
        class="file-card__name-input"
        placeholder="New folder"
        @keydown.enter.prevent="confirmCreate"
        @keydown.esc.prevent="$emit('cancel-create')"
        @blur="confirmCreate"
      />
      <!-- Match the regular cards' meta-row policy: only reserve
           the line when the consumer actually passes ``row-meta``,
           so the creating tile stays the same height as siblings. -->
      <div v-if="$slots['row-meta']" class="file-card__meta">&nbsp;</div>
    </div>

    <div
      v-for="row in sortedRows"
      :key="row.key"
      class="file-card"
      :class="{
        'file-card--selected': isSelected(row.key),
        'file-card--drop': dragOverKey === row.key,
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
      <div class="file-card__icon">
        <FileIcon
          :kind="row.kind"
          :name="row.kind === 'file' ? row.name : null"
          variant="jumbo"
          :size="48"
        />
        <slot name="row-status" :row="row" />
      </div>
      <input
        v-if="isRenaming(row)"
        ref="renameInput"
        type="text"
        class="file-card__name-input"
        :value="row.name"
        @click.stop
        @dblclick.stop
        @keydown.enter.prevent="confirmRename(row)"
        @keydown.esc.prevent="cancelRename"
        @blur="confirmRename(row)"
      />
      <div v-else class="file-card__title" :title="row.name">{{ row.name }}</div>
      <!-- Meta row only renders when the consumer actually passes
           something. Tile view default is the Finder-/Explorer-style
           "icon + name" pair; surfaces that want metadata (Library:
           "N docs · M subfolders") opt in via the ``row-meta`` slot
           and the slot's content drives the line height. Surfaces
           that don't (Workspace) get a tighter card. -->
      <div v-if="$slots['row-meta']" class="file-card__meta">
        <slot name="row-meta" :row="row" />
      </div>
    </div>

    <div v-if="!loading && !rows.length && !creating" class="file-grid__empty">
      <slot name="empty">This folder is empty.</slot>
    </div>
  </div>
</template>

<script setup>
/**
 * Generic file/folder tile (grid) renderer.
 *
 * Companion to ``FileTable.vue`` — same row identity / selection /
 * drag-drop / rename semantics, just laid out as a wrap-grid of
 * ~112px tiles instead of a table. Both views consume the same
 * ``FileRow`` shape and the same ``capabilities`` knobs; the parent
 * usually offers a list/grid toggle and switches between them.
 *
 * Slots:
 *   * ``row-status`` — absolute-positioned overlay on top of the
 *     icon (e.g. red error dot in Library).
 *   * ``row-meta``   — small caption under the name (defaults to
 *     formatted file size; Library overrides with
 *     "N docs · M subfolders" for folders).
 *   * ``empty``      — empty-state copy.
 *
 * The drag MIME type ``application/x-opencraig-files`` is shared
 * with ``FileTable.vue`` so a row dragged from one renderer drops
 * cleanly onto the other.
 */
import { computed, nextTick, ref, watch } from 'vue'

import FileIcon from '@/components/workspace/FileIcon.vue'
import { DEFAULT_CAPABILITIES } from './types.js'

const props = defineProps({
  rows: { type: Array, default: () => [] },
  selection: { type: Set, required: true },
  loading: { type: Boolean, default: false },
  creating: { type: Boolean, default: false },
  renamingKey: { type: String, default: '' },
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
  const selectedKeys = props.selection.has(row.key) && props.selection.size > 1
    ? [...props.selection]
    : [row.key]
  const payload = JSON.stringify({ items: [item], keys: selectedKeys })
  event.dataTransfer.setData('application/x-opencraig-files', payload)
  event.dataTransfer.effectAllowed = 'move'
  emit('drag-start', { items: [item], keys: selectedKeys })
}

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
  if (items.some(it => it?.kind === 'folder' && it?.path === folderRow.path)) return
  emit('drop-onto-folder', { items, targetRow: folderRow })
}

// Folders rank above files; alphabetical within each kind. Same
// ordering as ``FileTable.vue`` so a list↔grid toggle doesn't shuffle
// rows visually.
const sortedRows = computed(() => {
  const folders = props.rows.filter(r => r.kind === 'folder')
  const files = props.rows.filter(r => r.kind === 'file')
  const cmp = (a, b) => (a.name || '').localeCompare(b.name || '')
  return [...folders.sort(cmp), ...files.sort(cmp)]
})

function fmtSize(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`
}
</script>

<style scoped>
.file-grid {
  position: relative;
  display: grid;
  grid-template-columns: repeat(auto-fill, 128px);
  justify-content: start;
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
  grid-column: 1 / -1;
  font-size: 0.6875rem;
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
.file-card--selected { background: var(--color-bg-selected); }
.file-card--selected:hover {
  background: color-mix(in srgb, var(--color-bg-selected) 75%, var(--color-bg3));
}
.file-card--drop,
.file-card--drop.file-card--selected {
  background: color-mix(in srgb, var(--color-t1) 10%, transparent);
  outline: 1px solid var(--color-t1);
  outline-offset: -1px;
}
.file-card__icon {
  font-size: 2rem;
  line-height: 1;
  position: relative;
}
.file-card--creating { background: var(--color-bg3); }
.file-card__name-input {
  width: 100%;
  padding: 2px 4px;
  font-size: 0.6875rem;
  text-align: center;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line2);
  border-radius: var(--r-sm);
  outline: none;
  box-shadow: var(--ring-focus);
}
.file-card__title {
  font-size: 0.6875rem;
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
  font-size: 0.5625rem;
  color: var(--color-t3);
  text-align: center;
}
.file-grid__empty {
  grid-column: 1 / -1;
  text-align: center;
  padding: 48px 16px;
  font-size: 0.75rem;
  color: var(--color-t3);
}
</style>
