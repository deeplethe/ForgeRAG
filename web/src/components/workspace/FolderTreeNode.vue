<template>
  <div>
    <div
      class="tree-row"
      :class="{ 'tree-row--active': node.path === currentPath, 'tree-row--drop': isDragOver }"
      :style="{ paddingLeft: (depth * 12 + 4) + 'px' }"
      :draggable="!node.is_system"
      @click.stop="onClick"
      @contextmenu.prevent.stop="onContextMenu"
      @dragstart="onDragStart"
      @dragover.prevent="onDragOver"
      @dragleave="isDragOver = false"
      @drop.prevent="onDrop"
    >
      <span
        class="tree-toggle"
        :class="{ invisible: !hasChildren }"
        @click.stop="$emit('toggle', node.path)"
      >{{ isExpanded ? '▾' : '▸' }}</span>
      <FileIcon kind="folder" :size="14" class="tree-icon" />
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
        @context-menu="$emit('context-menu', $event)"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

import FileIcon from './FileIcon.vue'

const props = defineProps({
  node: { type: Object, required: true },
  currentPath: { type: String, required: true },
  expanded: { type: Set, required: true },
  depth: { type: Number, default: 0 },
})
const emit = defineEmits(['toggle', 'click-folder', 'drop-into', 'context-menu'])

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

function onDragStart(e) {
  // Mirror FileGrid/FileList payload shape so the existing drop handlers
  // (sidebar, folder rows, file lists) accept tree drags transparently —
  // the consumer reads ``items`` and doesn't care about the source.
  const item = {
    type: 'folder',
    folder_id: props.node.folder_id,
    path: props.node.path,
    name: props.node.name,
  }
  const key = 'f:' + item.folder_id
  const payload = JSON.stringify({ items: [item], keys: [key] })
  e.dataTransfer.setData('application/x-forgerag-item', payload)
  e.dataTransfer.effectAllowed = 'move'
}

function onContextMenu(e) {
  // Bubble up an item-shaped payload identical to what FileGrid/FileList
  // emit on right-click, plus ``source: 'tree'`` so Workspace can drop
  // operations that need inline UI (Rename) — tree rows have no editable
  // input slot, so triggering Rename from here would silently no-op.
  emit('context-menu', {
    x: e.clientX,
    y: e.clientY,
    source: 'tree',
    item: {
      type: 'folder',
      folder_id: props.node.folder_id,
      path: props.node.path,
      name: props.node.name,
    },
  })
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
  let parsed
  try { parsed = JSON.parse(raw) } catch { return }
  // Unwrap the inner array — the dataTransfer envelope is
  // ``{ items: [...], keys: [...] }`` (mirroring FileGrid/FileList) but
  // the consumer (``doDropMove``) expects a flat array of items. Was
  // emitting the whole envelope, which crashed downstream as soon as
  // sidebar→sidebar drags started flowing through here.
  const payload = Array.isArray(parsed?.items) ? parsed.items : []
  if (!payload.length) return
  // Reject the obvious self-drop; the server catches subtree-into-self too
  // but we save a round-trip for the most common mistake.
  const targetPath = props.node.path
  if (payload.some(it => it?.type === 'folder' && it?.path === targetPath)) return
  emit('drop-into', { items: payload, targetPath })
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
/* Active uses ``--color-bg-selected``; hover-on-active layers a slight
   tint shift so the user gets a cue when pointing at an already-active
   row (without it, hover collapsing to active makes the row feel "stuck").
   Same hover-on-selected pattern .file-card and .list-row use. */
.tree-row--active {
  background: var(--color-bg-selected);
  color: var(--color-t1);
  font-weight: 500;
}
.tree-row--active:hover {
  background: color-mix(in srgb, var(--color-bg-selected) 75%, var(--color-bg2));
}
/* Drop-target highlight — Vercel-style neutral white outline + tint.
   Brand blue is reserved for selection / primary CTAs in this design
   system, so using it for drag-over confused with the active row. The
   white pair reads as a deferential "this row will accept the drop"
   without competing for attention. */
.tree-row--drop {
  background: color-mix(in srgb, var(--color-t1) 10%, transparent);
  outline: 1px solid var(--color-t1);
  outline-offset: -1px;
  color: var(--color-t1);
}
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
