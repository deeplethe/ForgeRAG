<template>
  <!-- Recycle bin view — table layout matches FileList so the trash
       feels like the same surface the user just came from, just with
       trash-specific columns (Original location / Deleted at) and
       per-row Restore / Delete-forever actions instead of the regular
       open / move / rename gestures.

       Header carries the count + Empty-bin action; the breadcrumb
       (in Workspace.vue's toolbar) handles "exit trash" so we don't
       need a Back button here. -->
  <div class="file-list trash-list">
    <div class="trash-list__hdr">
      <span class="text-[11px] text-t3">
        {{ items.length }} item{{ items.length === 1 ? '' : 's' }}
      </span>
      <button
        class="empty-btn"
        :disabled="!items.length"
        @click="onEmpty"
      >Empty bin</button>
    </div>

    <div v-if="loading" class="file-list__loading">Loading…</div>
    <div v-else-if="!items.length" class="trash-empty">Recycle bin is empty.</div>

    <table v-else class="w-full text-[11px]">
      <colgroup>
        <col class="col-name" />
        <col class="col-type" />
        <col class="col-orig" />
        <col class="col-deleted" />
        <col class="col-actions" />
      </colgroup>
      <thead>
        <tr class="text-t3">
          <th class="list-th">Name</th>
          <th class="list-th">Type</th>
          <th class="list-th">Original location</th>
          <th class="list-th">Deleted</th>
          <th class="list-th"></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="(item, idx) in items"
          :key="idx"
          class="list-row"
        >
          <td>
            <div class="name-cell">
              <FileIcon
                :kind="item.type === 'folder' ? 'folder' : 'file'"
                :name="item.filename || item.name"
                :size="16"
                class="row-icon"
              />
              <span class="name-text">{{ item.type === 'folder' ? item.name : item.filename }}</span>
            </div>
          </td>
          <td>{{ item.type === 'folder' ? 'Folder' : fmtType(item.filename) }}</td>
          <td class="path-cell" :title="item.original_path || ''">{{ item.original_path || '—' }}</td>
          <td class="path-cell">
            {{ fmtAgo(item.trashed_at) }}
            <template v-if="item.trashed_by"> · {{ item.trashed_by }}</template>
          </td>
          <td class="actions-cell">
            <button
              class="icon-btn"
              @click="onRestore(item)"
              title="Restore"
            >
              <ArrowUturnLeftIcon class="w-3.5 h-3.5" />
            </button>
            <button
              class="icon-btn icon-btn--danger"
              @click="onPurge(item)"
              title="Delete forever"
            >
              <TrashIcon class="w-3.5 h-3.5" />
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ArrowUturnLeftIcon, TrashIcon } from '@heroicons/vue/24/outline'
import { emptyTrash, listTrash, purgeTrashItems, restoreFromTrash } from '@/api'
import { useDialog } from '@/composables/useDialog'
import FileIcon from './FileIcon.vue'

const { confirm, toast } = useDialog()

defineEmits(['back', 'changed'])

const items = ref([])
const loading = ref(true)

async function load() {
  loading.value = true
  try {
    const r = await listTrash()
    items.value = r?.items || []
  } catch (e) {
    console.error('listTrash failed:', e)
    items.value = []
  } finally {
    loading.value = false
  }
}

async function onRestore(item) {
  const ok = await confirm({
    title: `Restore "${item.filename || item.name}"?`,
    description: 'It will be moved back to its original location.',
    confirmText: 'Restore',
  })
  if (!ok) return
  const body = item.type === 'folder'
    ? { folder_paths: [item.path] }
    : { doc_ids: [item.doc_id] }
  try {
    await restoreFromTrash(body)
    await load()
  } catch (e) {
    toast('Restore failed: ' + e.message, { variant: 'error' })
  }
}

async function onPurge(item) {
  const name = item.filename || item.name
  const ok = await confirm({
    title: `Permanently delete "${name}"?`,
    description: 'This cannot be undone.',
    confirmText: 'Delete forever',
    variant: 'destructive',
  })
  if (!ok) return
  const body = item.type === 'folder'
    ? { folder_paths: [item.path] }
    : { doc_ids: [item.doc_id] }
  try {
    await purgeTrashItems(body)
    await load()
  } catch (e) {
    toast('Delete failed: ' + e.message, { variant: 'error' })
  }
}

async function onEmpty() {
  const n = items.value.length
  const ok = await confirm({
    title: `Empty the recycle bin?`,
    description: `All ${n} item${n === 1 ? '' : 's'} will be permanently deleted. This cannot be undone.`,
    confirmText: 'Empty bin',
    variant: 'destructive',
  })
  if (!ok) return
  try {
    await emptyTrash()
    await load()
  } catch (e) {
    toast('Empty bin failed: ' + e.message, { variant: 'error' })
  }
}

// Reuse FileList's filename-extension type derivation (same logic;
// kept local to avoid a cross-component import for one helper).
function fmtType(name) {
  const m = (name || '').match(/\.([^.]+)$/)
  return m ? m[1].toUpperCase() : '—'
}

function fmtAgo(iso) {
  if (!iso) return 'recently'
  const then = new Date(iso)
  const diff = (Date.now() - then.getTime()) / 1000
  if (diff < 60) return 'just now'
  if (diff < 3600) return Math.floor(diff / 60) + ' min ago'
  if (diff < 86400) return Math.floor(diff / 3600) + ' h ago'
  return Math.floor(diff / 86400) + ' d ago'
}

onMounted(load)
</script>

<style scoped>
/* Reuse FileList's structural rules (same look & feel) and add the
   trash-specific bits (header strip, action-icon column). */

.trash-list {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  padding: 0;
  user-select: none;
}

.trash-list__hdr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  border-bottom: 1px solid var(--color-line);
  flex-shrink: 0;
}
.empty-btn {
  font-size: 11px;
  padding: 4px 12px;
  color: var(--color-err-fg, #dc2626);
  background: transparent;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.empty-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-err-fg, #dc2626) 8%, transparent);
  border-color: var(--color-err-fg, #dc2626);
}
.empty-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.file-list__loading {
  padding: 48px 16px;
  text-align: center;
  font-size: 11px;
  color: var(--color-t3);
}
.trash-empty {
  padding: 48px 16px;
  text-align: center;
  font-size: 12px;
  color: var(--color-t3);
}

table {
  border-collapse: collapse;
  table-layout: fixed;
  margin: 0 16px;
  width: calc(100% - 32px);
  min-width: 760px;        /* fixed cols (90 + 200 + 150 + 80) + 240 min name */
}

.col-name      { width: auto; }
.col-type      { width: 90px; }
.col-orig      { width: 200px; }
.col-deleted   { width: 150px; }
.col-actions   { width: 80px; }

.list-th {
  text-align: left;
  padding: 6px 8px;
  font-weight: 400;
  font-size: 10px;
  color: var(--color-t3);
  white-space: nowrap;
}

.list-row { color: var(--color-t2); }
.list-row td {
  padding: 6px 8px;
  border-top: 1px solid var(--color-line);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.list-row:hover { background: var(--color-bg3); color: var(--color-t1); }

.name-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.name-cell .row-icon { flex-shrink: 0; margin-right: 0; }
.name-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.path-cell { color: var(--color-t3); }

.actions-cell {
  text-align: right;
  white-space: nowrap;
}
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  margin-left: 2px;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: var(--r-sm);
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.icon-btn:hover { background: var(--color-bg3); color: var(--color-t1); }
.icon-btn--danger:hover {
  background: color-mix(in srgb, var(--color-err-fg, #dc2626) 12%, transparent);
  color: var(--color-err-fg, #dc2626);
}
</style>
