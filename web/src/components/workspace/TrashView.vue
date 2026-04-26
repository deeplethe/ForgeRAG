<template>
  <div class="trash-view">
    <div class="trash-view__hdr">
      <div class="flex items-center gap-3">
        <button class="text-xs text-t3 hover:text-t1" @click="$emit('back')">‹ Back to workspace</button>
        <span class="text-xs text-t3">·</span>
        <h2 class="text-sm text-t1 font-medium">🗑 Recycle bin</h2>
      </div>
      <div class="flex items-center gap-3">
        <span class="text-xs text-t3">{{ items.length }} item(s)</span>
        <button
          class="action-btn action-btn--danger"
          :disabled="!items.length"
          @click="onEmpty"
        >Empty bin</button>
      </div>
    </div>

    <div v-if="loading" class="trash-view__empty">Loading…</div>
    <div v-else-if="!items.length" class="trash-view__empty">
      Recycle bin is empty.
    </div>

    <div v-else class="trash-list">
      <div
        v-for="(item, idx) in items"
        :key="idx"
        class="trash-row"
      >
        <span class="trash-row__icon">
          {{ item.type === 'folder' ? '📁' : '📄' }}
        </span>
        <div class="flex-1 min-w-0">
          <div class="text-t1 text-[12px] truncate">
            {{ item.type === 'folder' ? item.name : item.filename }}
          </div>
          <div class="text-t3 text-[10px] truncate">
            Was at {{ item.original_path || '(unknown)' }} ·
            Deleted {{ fmtAgo(item.trashed_at) }}
            <template v-if="item.trashed_by"> · by {{ item.trashed_by }}</template>
          </div>
        </div>
        <div class="flex items-center gap-1 flex-shrink-0">
          <button class="action-btn" @click="onRestore(item)">Restore</button>
          <button class="action-btn action-btn--danger" @click="onPurge(item)">Delete</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { emptyTrash, listTrash, purgeTrashItems, restoreFromTrash } from '@/api'
import { useDialog } from '@/composables/useDialog'

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
.trash-view { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.trash-view__hdr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid var(--color-line);
}
.trash-view__empty {
  padding: 48px 16px;
  text-align: center;
  color: var(--color-t3);
  font-size: 12px;
}
.trash-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 16px;
}
.trash-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--color-line);
  border-radius: 8px;
  margin-bottom: 8px;
}
.trash-row__icon { font-size: 20px; flex-shrink: 0; }
.action-btn {
  font-size: 10px;
  padding: 4px 10px;
  color: var(--color-t2);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 4px;
  cursor: pointer;
}
.action-btn:hover { background: var(--color-bg2); color: var(--color-t1); }
.action-btn--danger { color: #dc2626; border-color: #fecaca; }
.action-btn--danger:hover { background: #fef2f2; }
.action-btn:disabled { opacity: 0.4; cursor: not-allowed; }
</style>
