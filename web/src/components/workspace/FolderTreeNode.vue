<template>
  <div>
    <div
      class="tree-row"
      :class="{ 'tree-row--active': node.path === currentPath, 'tree-row--drop': isDragOver }"
      :style="{ paddingLeft: (depth * 12 + 4) + 'px' }"
      @click.stop="onClick"
      @dragover.prevent="onDragOver"
      @dragleave="isDragOver = false"
      @drop.prevent="onDrop"
    >
      <span
        class="tree-toggle"
        :class="{ invisible: !hasChildren }"
        @click.stop="$emit('toggle', node.path)"
      >{{ isExpanded ? '▾' : '▸' }}</span>
      <span class="tree-icon">📁</span>
      <span class="tree-label">{{ node.name || 'Root' }}</span>
      <span v-if="badgeCount > 0" class="tree-count">{{ badgeCount }}</span>
    </div>
    <div v-if="isExpanded && hasChildren">
      <FolderTreeNode
        v-for="child in visibleChildren"
        :key="child.folder_id"
        :node="child"
        :current-path="currentPath"
        :expanded="expanded"
        :depth="depth + 1"
        @toggle="$emit('toggle', $event)"
        @click-folder="$emit('click-folder', $event)"
        @drop-into="$emit('drop-into', $event)"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  node: { type: Object, required: true },
  currentPath: { type: String, required: true },
  expanded: { type: Set, required: true },
  depth: { type: Number, default: 0 },
})
const emit = defineEmits(['toggle', 'click-folder', 'drop-into'])

const isExpanded = computed(() => props.expanded.has(props.node.path))
// Guard: children may be undefined / null / non-array from a malformed response
const safeChildren = computed(() => {
  const c = props.node?.children
  return Array.isArray(c) ? c : []
})
const hasChildren = computed(() => safeChildren.value.length > 0)
const visibleChildren = computed(
  // Hide system folders from sidebar navigation (trash is accessed separately)
  () => safeChildren.value.filter(c => !c.is_system),
)
// Coerce document_count to a non-negative integer; some backends may omit it
const badgeCount = computed(() => {
  const n = Number(props.node?.document_count)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
})

const isDragOver = ref(false)

function onClick() {
  // If clicking a collapsed parent, expand it so the user sees context;
  // clicking the already-active folder is handled upstream (navigate dedupes).
  if (hasChildren.value && !isExpanded.value) {
    emit('toggle', props.node.path)
  }
  emit('click-folder', props.node.path)
}

function onDragOver(e) {
  // Accept file-manager items (JSON payload with type+path)
  if (e.dataTransfer.types.includes('application/x-forgerag-item')) {
    e.dataTransfer.dropEffect = 'move'
    isDragOver.value = true
  }
}

function onDrop(e) {
  isDragOver.value = false
  const raw = e.dataTransfer.getData('application/x-forgerag-item')
  if (!raw) return
  let items
  try { items = JSON.parse(raw) } catch { return }
  // Reject the obvious self-drop; the server catches subtree-into-self too
  // but we save a round-trip for the most common mistake.
  const targetPath = props.node.path
  const payload = Array.isArray(items?.items) ? items.items : []
  if (payload.some(it => it?.type === 'folder' && it?.path === targetPath)) return
  emit('drop-into', { items, targetPath })
}
</script>

<style scoped>
.tree-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 6px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 11px;
  color: var(--color-t2);
}
.tree-row:hover { background: var(--color-bg2); color: var(--color-t1); }
/* Active needs ``:hover`` co-targeted because ``.tree-row:hover`` has
   specificity (0,1,1) which beats ``.tree-row--active`` (0,1,0) —
   without this, hovering an already-selected row reverts the highlight
   to the hover background. Same pattern .file-card--selected:hover uses. */
.tree-row--active,
.tree-row--active:hover {
  background: var(--color-bg-selected);
  color: var(--color-t1);
  font-weight: 500;
}
.tree-row--drop { outline: 1.5px dashed var(--color-brand); outline-offset: -2px; }
.tree-toggle {
  display: inline-block;
  width: 10px;
  text-align: center;
  color: var(--color-t3);
  font-size: 9px;
  flex-shrink: 0;
}
.tree-icon { flex-shrink: 0; }
.tree-label {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tree-count {
  margin-left: auto;
  font-size: 9px;
  color: var(--color-t3);
  padding: 0 4px;
  border-radius: 4px;
  background: var(--color-bg2);
}
.invisible { visibility: hidden; }
</style>
