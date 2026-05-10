<template>
  <FileTable
    :rows="rows"
    :selection="selection"
    :loading="loading"
    :creating="creating"
    :renaming-key="renamingKey"
    :columns="['name', 'type', 'size', 'created', 'modified']"
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
      <!-- Library shows a subtle "failed" pill when ingest errored.
           In-flight states are intentionally NOT badged on the row
           (see roadmap: only the failed state earns visual prominence
           on the file rail; the doc-detail page surfaces full
           per-stage status). -->
      <span
        v-if="row.kind === 'file' && row.extras?.status === 'error'"
        class="status-chip status-chip--error"
        :title="row.extras?.error_message || 'Ingestion failed'"
      >failed</span>
    </template>
  </FileTable>
</template>

<script setup>
/**
 * Library adapter for the generic ``<FileTable>``.
 *
 * Maps the Library's domain shape (``FolderOut`` + ``DocumentOut``
 * with folder_id / doc_id / status / embed_status / etc.) onto the
 * neutral ``FileRow`` the renderer expects. Keeps the public
 * contract of this component identical to the pre-refactor
 * version (same props + emits) so ``Library.vue`` doesn't need to
 * change.
 *
 * The visible behaviour change vs the pre-refactor version is
 * limited to the drag-payload MIME type
 * (``application/x-opencraig-files`` instead of
 * ``-forgerag-item``). No other part of the page consumed the old
 * MIME so this is a self-contained rebrand.
 */
import { computed } from 'vue'

import FileTable from '@/components/files/FileTable.vue'

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

// Library always wants the full feature set: select, multi-select,
// inline rename, drag-to-move, right-click. The doc-detail page
// handles preview-style flows; the table itself is the manage
// surface.
const capabilities = {
  select: true,
  multiSelect: true,
  rename: true,
  dragMove: true,
  contextMenu: true,
}

// Domain-shape → FileRow translation. The ``key`` mirrors the
// pre-refactor selection convention (``"f:<id>"`` / ``"d:<id>"``)
// so existing parent-side selection state survives this refactor
// without migration.
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
      // The full record is stashed under ``extras`` so domain-aware
      // slot consumers (status chip, future menu items) can read
      // doc-id / folder-id / shared_with / status fields without us
      // re-flattening them up here.
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
    // Re-emit the original DocumentOut so listeners that destructure
    // ``doc_id`` / ``filename`` keep working.
    emit('open-document', row.extras)
  }
}

function onContextMenu({ x, y, row }) {
  // Translate the new ``row``-shaped event back to the historical
  // ``item`` shape Library.vue's context-menu handler expects.
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
  // Old shape: ``{ items, targetPath }``. Items already carry
  // ``{key, kind, path, name}``; Library.vue's drop handler reads
  // ``items[i].path`` so we pass them through verbatim.
  emit('drop-onto-folder', { items, targetPath: targetRow.path })
}

// Mirror of the in-flight check from the pre-refactor FileList —
// kept here (not in the generic FileTable) because it's
// Library-specific (depends on the multi-stage ingest pipeline's
// ``status`` / ``embed_status`` / ``enrich_status`` / ``kg_status``
// fields). The context-menu disables certain actions on a doc
// that's mid-ingest.
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
</script>

<style scoped>
.status-chip {
  display: inline-flex;
  align-items: center;
  padding: 0 6px;
  margin-left: 6px;
  font-size: 9px;
  font-weight: 500;
  line-height: 14px;
  border-radius: 3px;
  cursor: help;
}
.status-chip--error {
  color: var(--color-err-fg);
  background: color-mix(in srgb, var(--color-err-fg) 14%, transparent);
}
</style>
