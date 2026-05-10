<template>
  <FileTiles
    :rows="rows"
    :selection="selection"
    :loading="loading"
    :creating="creating"
    :renaming-key="renamingKey"
    :capabilities="capabilities"
    @select="emit('select', $event)"
    @open-row="onOpenRow"
    @context-menu="onContextMenu"
    @drag-start="emit('drag-start', $event)"
    @drop-onto-folder="onDropOntoFolder"
    @confirm-create="emit('confirm-create', $event)"
    @cancel-create="emit('cancel-create')"
    @confirm-rename="emit('confirm-rename', $event)"
    @cancel-rename="emit('cancel-rename')"
  >
    <template #row-status="{ row }">
      <!-- Tiny solid red dot in the bottom-right of the icon when an
           ingest run failed. Mirrors the pre-refactor placement; the
           dot is the one signal that needs colour because failure is
           the only state the user MUST not miss. In-flight states
           are intentionally NOT badged here (per product decision —
           the doc-detail page surfaces full per-stage status). -->
      <span
        v-if="row.kind === 'file' && row.extras?.status === 'error'"
        class="status-badge status-badge--error"
        :title="row.extras?.error_message || 'Ingestion failed'"
      ></span>
    </template>
    <template #row-meta="{ row }">
      <!-- Folder meta: "N docs · M subfolders" — Library-specific
           fields the workspace browser exposes. Leave folders that
           are still loading their counts blank rather than showing
           "0 docs · 0 subfolders" which would imply emptiness. -->
      <template v-if="row.kind === 'folder' && row.extras">
        {{ row.extras.document_count ?? 0 }} docs · {{ row.extras.child_folders ?? 0 }} subfolders
      </template>
      <!-- Failed-doc tile: red "failed" caption replaces the file
           size so the tile reads as a problem, not a regular file. -->
      <template v-else-if="row.kind === 'file' && row.extras?.status === 'error'">
        <span class="meta-error" :title="row.extras?.error_message || ''">failed</span>
      </template>
      <!-- Default: file size for files; format if size missing. -->
      <template v-else-if="row.kind === 'file' && row.size">{{ fmtSize(row.size) }}</template>
      <template v-else-if="row.kind === 'file' && row.extras?.format">{{ row.extras.format }}</template>
      <template v-else>&nbsp;</template>
    </template>
  </FileTiles>
</template>

<script setup>
/**
 * Library adapter for the generic ``<FileTiles>``.
 *
 * Same role as ``FileList.vue`` (Library adapter for ``FileTable``),
 * just laid out as a wrap-grid of ~112px tiles instead of a table.
 * The two share the row-shape adapter logic; we'd factor that out
 * if a third Library view ever showed up, but two is fine.
 *
 * Public contract preserved verbatim — same props + emits as the
 * pre-refactor ``FileGrid``. ``Library.vue`` doesn't need to know
 * the implementation switched.
 */
import { computed } from 'vue'

import FileTiles from '@/components/files/FileTiles.vue'

const props = defineProps({
  folders: { type: Array, default: () => [] },
  documents: { type: Array, default: () => [] },
  selection: { type: Set, required: true },
  loading: { type: Boolean, default: false },
  creating: { type: Boolean, default: false },
  renamingKey: { type: String, default: '' },
})

const emit = defineEmits([
  'select',
  'open-folder',
  'open-document',
  'context-menu',
  'drag-start',
  'drop-onto-folder',
  'confirm-create',
  'cancel-create',
  'confirm-rename',
  'cancel-rename',
])

const capabilities = {
  select: true,
  multiSelect: true,
  rename: true,
  dragMove: true,
  contextMenu: true,
}

const rows = computed(() => {
  const out = []
  for (const f of props.folders) {
    out.push({
      key: 'f:' + f.folder_id,
      kind: 'folder',
      name: f.name,
      path: f.path,
      size: null,
      createdAt: null,
      modifiedAt: null,
      extras: f,
    })
  }
  for (const d of props.documents) {
    out.push({
      key: 'd:' + d.doc_id,
      kind: 'file',
      name: d.filename || d.file_name || d.doc_id,
      path: d.path,
      size: d.file_size_bytes ?? null,
      createdAt: d.created_at ?? null,
      modifiedAt: d.updated_at ?? d.created_at ?? null,
      extras: d,
    })
  }
  return out
})

function onOpenRow(row) {
  if (row.kind === 'folder') {
    emit('open-folder', row.path)
  } else {
    emit('open-document', row.extras)
  }
}

function onContextMenu({ x, y, row }) {
  let item = null
  if (row) {
    if (row.kind === 'folder') {
      item = {
        type: 'folder',
        folder_id: row.extras?.folder_id,
        path: row.path,
        name: row.name,
      }
    } else {
      item = {
        type: 'document',
        doc_id: row.extras?.doc_id,
        path: row.path,
        name: row.name,
        inFlight: isDocInFlight(row.extras),
      }
    }
  }
  emit('context-menu', { x, y, item })
}

function onDropOntoFolder({ items, targetRow }) {
  emit('drop-onto-folder', { items, targetPath: targetRow.path })
}

const _DOC_TERMINAL_STATUSES = new Set(['ready', 'error'])
const _SUB_TERMINAL_STATUSES = new Set(['done', 'error', 'skipped', null, undefined, ''])
function _stageInFlight(s, terminalSet) {
  if (s == null) return false
  return !terminalSet.has(s)
}
function isDocInFlight(d) {
  if (!d) return false
  return _stageInFlight(d.status, _DOC_TERMINAL_STATUSES)
      || _stageInFlight(d.embed_status, _SUB_TERMINAL_STATUSES)
      || _stageInFlight(d.enrich_status, _SUB_TERMINAL_STATUSES)
      || _stageInFlight(d.kg_status, _SUB_TERMINAL_STATUSES)
}

function fmtSize(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`
}
</script>

<style scoped>
.status-badge {
  position: absolute;
  right: -6px;
  bottom: -2px;
  width: 14px;
  height: 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 0.5625rem;
  font-weight: 600;
  color: white;
  border-radius: 50%;
  line-height: 1;
  cursor: help;
}
.status-badge--error { background: var(--color-err-fg); }
.meta-error { color: var(--color-err-fg); cursor: help; }
</style>
