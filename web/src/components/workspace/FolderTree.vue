<template>
  <div class="folder-tree text-[11px] text-t2 select-none">
    <!-- Loading skeleton: a few shimmer rows hinting at tree shape -->
    <div v-if="loading && !root" class="skel-tree">
      <Skeleton v-for="(w, i) in [120, 90, 140, 80]" :key="i"
        block :w="w" :h="14" class="skel-row" />
    </div>

    <!-- Error -->
    <div v-else-if="error" class="tree-status tree-status--error">
      <span>Failed to load folder tree</span>
      <button class="retry-btn" @click="$emit('retry')">retry</button>
    </div>

    <!-- Render the tree without the implicit "/" root node (it's always
         present and noise). Show its children directly at depth 0. -->
    <template v-else-if="root">
      <FolderTreeNode
        v-for="child in topChildren"
        :key="child.folder_id"
        :node="child"
        :current-path="currentPath"
        :expanded="expanded"
        :depth="0"
        @toggle="toggleExpand"
        @click-folder="$emit('navigate', $event)"
        @drop-into="$emit('drop-into', $event)"
      />
      <div
        v-if="!topChildren.length"
        class="tree-status"
      >Empty workspace.</div>
    </template>

    <!-- Neither loading nor have root — defensive fallback -->
    <div v-else class="tree-status">No folders.</div>
  </div>
</template>

<script setup>
import { computed, reactive, watch } from 'vue'
import FolderTreeNode from './FolderTreeNode.vue'
import Skeleton from '@/components/Skeleton.vue'

const props = defineProps({
  root: { type: Object, default: null },
  currentPath: { type: String, required: true },
  loading: { type: Boolean, default: false },
  error: { type: String, default: null },
})
defineEmits(['navigate', 'drop-into', 'retry'])

// Skip the implicit "/" root and surface its first-level children. Filter
// out system folders (trash) — those are accessed through dedicated UI.
const topChildren = computed(() => {
  const c = props.root?.children
  if (!Array.isArray(c)) return []
  return c.filter((x) => !x.is_system)
})

/**
 * Expanded nodes are keyed by folder.path. Root '/' is expanded by default.
 * When ``currentPath`` changes, every ancestor is auto-expanded so the
 * user always sees where they are in the tree.
 */
const expanded = reactive(new Set(['/']))
function toggleExpand(path) {
  if (expanded.has(path)) expanded.delete(path)
  else expanded.add(path)
}

watch(
  () => props.currentPath,
  (p) => {
    if (!p || p === '/') return
    // Expand '/', '/a', '/a/b', ... '/a/b/c' so the active leaf is visible.
    const parts = p.split('/').filter(Boolean)
    let acc = ''
    expanded.add('/')
    for (const seg of parts) {
      acc += '/' + seg
      expanded.add(acc)
    }
  },
  { immediate: true },
)
</script>

<style scoped>
.folder-tree {
  padding: 8px 4px;
}
.tree-status {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  color: var(--color-t3);
  font-size: 11px;
}
.tree-status--error { color: var(--color-err, #b85); }
.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: var(--color-t3);
  opacity: 0.6;
  animation: pulse 1.4s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 0.9; }
}
.retry-btn {
  margin-left: auto;
  padding: 2px 6px;
  font-size: 10px;
  border-radius: 4px;
  background: var(--color-bg2);
  color: var(--color-t2);
  border: 1px solid var(--color-line);
  cursor: pointer;
}
.retry-btn:hover { color: var(--color-t1); background: var(--color-bg3); }

.skel-tree { padding: 8px 12px; display: flex; flex-direction: column; gap: 10px; }
.skel-row { border-radius: 4px; }
.skel-row:nth-child(2n) { margin-left: 14px; }
.skel-row:nth-child(3n) { margin-left: 22px; }
</style>
