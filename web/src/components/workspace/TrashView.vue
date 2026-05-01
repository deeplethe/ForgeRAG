<template>
  <!-- Recycle bin — pure presenter. State (items / loading / mutations)
       lives in Workspace.vue so the toolbar can render the count +
       Empty-bin button without lifecycle plumbing, and so this view
       contributes no extra header strip (which previously made the
       page header height jitter when entering / exiting the trash). -->
  <div class="file-list trash-list">
    <div v-if="loading" class="trash-empty">Loading…</div>
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
              @click="$emit('restore', item)"
              title="Restore"
            >
              <Undo2 class="w-3.5 h-3.5" :stroke-width="1.5" />
            </button>
            <button
              class="icon-btn icon-btn--danger"
              @click="$emit('purge', item)"
              title="Delete forever"
            >
              <Trash2 class="w-3.5 h-3.5" :stroke-width="1.5" />
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { Undo2, Trash2 } from 'lucide-vue-next'
import FileIcon from './FileIcon.vue'

defineProps({
  items: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
})
defineEmits(['restore', 'purge'])

// Mirrors FileList's filename-extension type derivation; kept local
// to avoid pulling FileList in for one helper.
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
</script>

<style scoped>
.trash-list {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  user-select: none;
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
/* Destructive variant — matches the context-menu Delete colour at
   rest (red text always visible, signalling the action's nature
   before the user hovers) and gets a stronger red wash on hover.
   14% mix matches ContextMenu.ctx-item--danger so the two reads
   identically across the workspace. */
.icon-btn--danger {
  color: var(--color-err-fg, #dc2626);
}
.icon-btn--danger:hover {
  background: color-mix(in srgb, var(--color-err-fg, #dc2626) 14%, transparent);
  color: var(--color-err-fg, #dc2626);
}
</style>
