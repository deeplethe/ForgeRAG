<template>
  <div class="miller" ref="rootEl">
    <div
      v-for="(col, i) in columns"
      :key="col.path"
      class="miller-col"
    >
      <div class="miller-col__hdr" :title="col.path">{{ col.name }}</div>
      <div class="miller-col__body">
        <div
          v-for="child in col.folders"
          :key="'f:' + child.folder_id"
          class="miller-row"
          :class="{ 'miller-row--active': activePath(i + 1) === child.path }"
          @click="onClickFolder(i, child)"
          @dblclick="$emit('open-folder', child.path)"
          @contextmenu.prevent="onContext($event, { type: 'folder', folder_id: child.folder_id, path: child.path, name: child.name })"
        >
          <span class="miller-row__icon">📁</span>
          <span class="truncate">{{ child.name }}</span>
          <span class="miller-row__arrow">›</span>
        </div>
        <div
          v-for="d in col.docs"
          :key="'d:' + d.doc_id"
          class="miller-row miller-row--doc"
          @click="onClickDocument(d)"
          @dblclick="$emit('open-document', d)"
          @contextmenu.prevent="onContext($event, { type: 'document', doc_id: d.doc_id, path: d.path, name: d.filename || d.file_name })"
        >
          <span class="miller-row__icon">📄</span>
          <span class="truncate">{{ d.filename || d.file_name || d.doc_id }}</span>
        </div>
        <div v-if="col.loading" class="miller-empty">Loading…</div>
        <div v-else-if="!col.folders.length && !col.docs.length" class="miller-empty">
          Empty
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onMounted, ref, watch } from 'vue'
import { getFolderTree, listDocuments } from '@/api'

const props = defineProps({
  rootPath: { type: String, default: '/' },
  initialPath: { type: String, default: '/' },
})
const emit = defineEmits(['navigate', 'open-folder', 'open-document', 'context-menu'])

const columns = ref([])     // [{ path, name, folders, docs, loading }]
const rootEl = ref(null)

function activePath(idx) {
  return idx < columns.value.length ? columns.value[idx].path : null
}

async function loadColumn(path) {
  const col = { path, name: path === '/' ? 'Home' : path.split('/').pop(), folders: [], docs: [], loading: true }
  columns.value.push(col)
  try {
    const node = await getFolderTree(path, 1, false)
    col.folders = (node?.children || []).filter(c => !c.is_system)
    const docs = await listDocuments({ limit: 500, offset: 0 })
    const head = path === '/' ? '/' : path + '/'
    col.docs = (docs?.items || []).filter(d => {
      if (!d.path) return false
      if (!d.path.startsWith(head)) return false
      const tail = d.path.slice(head.length)
      return tail.length > 0 && !tail.includes('/')
    })
  } catch (e) {
    console.error('miller load failed:', e)
  } finally {
    col.loading = false
  }
  // Scroll the rightmost column into view
  await nextTick()
  if (rootEl.value) rootEl.value.scrollLeft = rootEl.value.scrollWidth
}

async function onClickFolder(colIdx, child) {
  // Truncate columns to the right of this one, then load the new one
  columns.value = columns.value.slice(0, colIdx + 1)
  await loadColumn(child.path)
  emit('navigate', child.path)
}

function onClickDocument(doc) {
  // Selection doesn't affect column structure; just forward for selection handling
  emit('open-document', doc)
}

function onContext(event, item) {
  emit('context-menu', { x: event.clientX, y: event.clientY, item })
}

onMounted(async () => {
  await loadColumn(props.rootPath || '/')
  // Drill into initialPath if provided
  if (props.initialPath && props.initialPath !== '/') {
    const parts = props.initialPath.split('/').filter(Boolean)
    let acc = ''
    for (const p of parts) {
      acc += '/' + p
      await loadColumn(acc)
    }
  }
})

watch(() => props.initialPath, async (newPath) => {
  columns.value = []
  await loadColumn('/')
  if (newPath && newPath !== '/') {
    const parts = newPath.split('/').filter(Boolean)
    let acc = ''
    for (const p of parts) {
      acc += '/' + p
      await loadColumn(acc)
    }
  }
})
</script>

<style scoped>
.miller {
  display: flex;
  overflow-x: auto;
  height: 100%;
  min-height: 240px;
}
.miller-col {
  flex: 0 0 240px;
  border-right: 1px solid var(--color-line);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.miller-col__hdr {
  padding: 6px 10px;
  font-size: 10px;
  color: var(--color-t3);
  background: var(--color-bg2);
  border-bottom: 1px solid var(--color-line);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.miller-col__body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.miller-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  font-size: 11px;
  color: var(--color-t2);
  cursor: pointer;
}
.miller-row:hover { background: var(--color-bg2); color: var(--color-t1); }
.miller-row--active { background: var(--color-bg3); color: var(--color-t1); }
.miller-row__icon { flex-shrink: 0; }
.miller-row__arrow {
  margin-left: auto;
  color: var(--color-t3);
}
.miller-row--doc .miller-row__arrow { display: none; }
.miller-empty {
  padding: 16px;
  text-align: center;
  font-size: 10px;
  color: var(--color-t3);
}
</style>
