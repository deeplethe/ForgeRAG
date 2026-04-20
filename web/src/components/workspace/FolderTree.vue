<template>
  <div class="folder-tree text-[11px] text-t2 select-none">
    <FolderTreeNode
      v-if="root"
      :node="root"
      :current-path="currentPath"
      :expanded="expanded"
      @toggle="toggleExpand"
      @click-folder="$emit('navigate', $event)"
      @drop-into="$emit('drop-into', $event)"
    />
    <div v-else class="px-3 py-2 text-t3">Loading tree...</div>
  </div>
</template>

<script setup>
import { reactive } from 'vue'
import FolderTreeNode from './FolderTreeNode.vue'

defineProps({
  root: { type: Object, default: null },
  currentPath: { type: String, required: true },
})
defineEmits(['navigate', 'drop-into'])

/**
 * Expanded nodes are keyed by folder.path. Root '/' is expanded by
 * default; the rest collapse to start.
 */
const expanded = reactive(new Set(['/']))
function toggleExpand(path) {
  if (expanded.has(path)) expanded.delete(path)
  else expanded.add(path)
}
</script>

<style scoped>
.folder-tree {
  padding: 8px 4px;
}
</style>
