<script setup>
// ``collapsed`` is a Set<string> of node_ids the user has explicitly
// collapsed. Default behaviour is fully expanded; a node renders its
// children unless its id is in this set. This matches the semantics
// the parent (DocDetail.vue) actually maintains via ``toggleNode``,
// which add/removes node_ids from the set on the user's expand/collapse
// click. (Earlier the prop was typed ``Object`` and read with
// ``expanded[id] !== false`` — but the parent passed a Set, so the
// indexed read always returned ``undefined`` and the toggle button
// did nothing visible.)
const props = defineProps({
  node: Object,
  nodes: Object,
  depth: { type: Number, default: 0 },
  highlight: Set,
  filterNodeId: String,
  collapsed: { type: Set, default: () => new Set() },
})
const emit = defineEmits(['toggle', 'select'])

function children() {
  if (!props.node?.children) return []
  return props.node.children.map(id => props.nodes[id]).filter(Boolean)
}

function isExpanded() {
  return !props.collapsed.has(props.node.node_id)
}

function hasChildren() {
  return children().length > 0
}
</script>

<template>
  <div v-if="node">
    <div
      class="flex items-start gap-1 py-1 px-1.5 rounded cursor-pointer transition-colors hover:bg-bg2"
      :style="{ paddingLeft: depth * 16 + 6 + 'px' }"
      @click="emit('select', node.node_id)"
    >
      <!-- expand/collapse arrow -->
      <button
        v-if="hasChildren()"
        @click.stop="emit('toggle', node.node_id)"
        class="text-3xs text-t3 w-3 shrink-0 mt-px select-none"
      >{{ isExpanded() ? '\u25BE' : '\u25B8' }}</button>
      <span v-else class="w-3 shrink-0"></span>

      <!-- content -->
      <div class="min-w-0 flex-1">
        <div class="text-3xs truncate"
             :class="filterNodeId === node.node_id || highlight.has(node.node_id) ? 'text-t1 font-semibold' : 'text-t1'">
          {{ node.title || node.node_id }}
        </div>
        <div class="text-5xs"
             :class="filterNodeId === node.node_id || highlight.has(node.node_id) ? 'text-t2 font-medium' : 'text-t3'">
          L{{ node.level }}
          <template v-if="node.page_start"> · p.{{ node.page_start }}{{ node.page_end && node.page_end !== node.page_start ? '-' + node.page_end : '' }}</template>
          <template v-if="node.table_count"> · {{ node.table_count }}T</template>
          <template v-if="node.image_count"> · {{ node.image_count }}I</template>
        </div>
      </div>
    </div>

    <!-- children -->
    <template v-if="hasChildren() && isExpanded()">
      <TreeNode
        v-for="child in children()" :key="child.node_id"
        :node="child"
        :nodes="nodes"
        :depth="depth + 1"
        :highlight="highlight"
        :filterNodeId="filterNodeId"
        :collapsed="collapsed"
        @toggle="emit('toggle', $event)"
        @select="emit('select', $event)"
      />
    </template>
  </div>
</template>
