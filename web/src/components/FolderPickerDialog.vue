<template>
  <!--
    Reusable folder picker — modal that lets the user browse the folder
    tree and pick a destination. Replaces ``window.prompt`` for any
    "move to where?" / "save here?" workflow.

    Usage:
      <FolderPickerDialog
        v-model:open="moveOpen"
        title="Move to..."
        :exclude-paths="['/__trash__', sourcePath]"
        :initial-path="ws.currentPath.value"
        @select="onMoveTarget"
      />

    Emits:
      update:open — for v-model
      select(path) — when the user clicks Move (the chosen path)
      cancel       — explicit cancel
  -->
  <Teleport to="body">
    <Transition name="dialog">
      <div
        v-if="open"
        class="dialog-backdrop"
        @click.self="onCancel"
        @keydown.esc="onCancel"
      >
        <div class="picker panel" role="dialog" aria-modal="true" tabindex="-1" ref="dialogEl">
          <div class="picker__header">
            <h2 class="picker__title">{{ title }}</h2>
            <p v-if="description" class="picker__desc">{{ description }}</p>
          </div>

          <div class="picker__crumb">
            <button
              v-for="(seg, idx) in breadcrumb"
              :key="seg.path"
              class="picker__crumb-btn"
              :disabled="idx === breadcrumb.length - 1"
              @click="navigateTo(seg.path)"
            >{{ seg.label }}</button>
          </div>

          <div class="picker__body">
            <div v-if="loading" class="picker__hint">Loading…</div>
            <div v-else-if="visibleFolders.length === 0" class="picker__hint">
              No subfolders here. Click Move to drop into <b>{{ currentPath }}</b>.
            </div>
            <button
              v-for="f in visibleFolders"
              :key="f.path"
              class="picker__row"
              @click="enter(f.path)"
            >
              <FileIcon kind="folder" :size="16" />
              <span class="picker__row-name">{{ f.name }}</span>
              <ChevronRight class="picker__row-chev" :size="14" :stroke-width="1.5" />
            </button>
          </div>

          <div class="picker__footer">
            <span class="picker__current">Selected: <b>{{ currentPath }}</b></span>
            <div class="picker__actions">
              <button class="btn-secondary" @click="onCancel">Cancel</button>
              <button class="btn-primary" :disabled="!canPick" @click="onConfirm">{{ confirmText }}</button>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { listFolders } from '@/api'
import { ChevronRight } from 'lucide-vue-next'
// Folders use the same 📁 emoji as the workspace's FileIcon — visual
// consistency with the file grid the user just came from.
import FileIcon from '@/components/workspace/FileIcon.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: 'Pick a folder' },
  description: { type: String, default: '' },
  confirmText: { type: String, default: 'Select' },
  initialPath: { type: String, default: '/' },
  // Paths the user shouldn't be allowed to pick (or navigate into).
  // Typical: the source folder being moved, the trash, the items being
  // moved themselves so the user can't drop a folder into itself.
  excludePaths: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:open', 'select', 'cancel'])

const dialogEl = ref(null)
const currentPath = ref('/')
const allFolders = ref([])   // flat snapshot of the whole tree (one API call)
const loading = ref(false)

// Open → reset to ``initialPath`` and reload the whole flat tree once.
// We filter client-side per ``currentPath``, so navigating around inside
// the picker is O(1) and never spawns extra requests.
watch(() => props.open, async (now) => {
  if (now) {
    currentPath.value = props.initialPath || '/'
    await loadFolders()
    await nextTick()
    dialogEl.value?.focus()
  }
})

async function loadFolders() {
  loading.value = true
  try {
    const r = await listFolders()
    const items = Array.isArray(r) ? r : r?.items || []
    allFolders.value = items.filter(f => !f.is_system)
  } catch {
    allFolders.value = []
  } finally {
    loading.value = false
  }
}

function isExcluded(path) {
  return props.excludePaths.some(ex => path === ex || path.startsWith(ex + '/'))
}

const visibleFolders = computed(() => {
  // Direct children of currentPath only — segment count = current + 1.
  const cur = currentPath.value
  const expectedDepth = cur === '/' ? 1 : cur.split('/').filter(Boolean).length + 1
  const prefix = cur === '/' ? '/' : cur + '/'
  return allFolders.value
    .filter(f => {
      if (isExcluded(f.path)) return false
      if (cur === '/' ? !f.path.startsWith('/') : !f.path.startsWith(prefix)) return false
      const segs = f.path.split('/').filter(Boolean).length
      return segs === expectedDepth
    })
    .sort((a, b) => a.name.localeCompare(b.name))
})

const canPick = computed(() => !isExcluded(currentPath.value))

const breadcrumb = computed(() => {
  if (currentPath.value === '/') return [{ path: '/', label: 'Home' }]
  const parts = currentPath.value.split('/').filter(Boolean)
  const segs = [{ path: '/', label: 'Home' }]
  let acc = ''
  for (const p of parts) {
    acc += '/' + p
    segs.push({ path: acc, label: p })
  }
  return segs
})

function navigateTo(path) {
  currentPath.value = path
}

function enter(path) {
  if (isExcluded(path)) return
  currentPath.value = path
}

function onConfirm() {
  if (!canPick.value) return
  emit('select', currentPath.value)
  emit('update:open', false)
}

function onCancel() {
  emit('cancel')
  emit('update:open', false)
}
</script>

<style scoped>
/* Backdrop matches DialogHost's modal — same blur + overlay so the two
   feel like the same surface. */
.dialog-backdrop {
  position: fixed;
  inset: 0;
  background: color-mix(in srgb, #000 45%, transparent);
  backdrop-filter: blur(2px);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.picker {
  width: 100%;
  max-width: 480px;
  max-height: 70vh;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
  outline: none;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
  border: 1px solid var(--color-line);
  border-radius: 8px;
  overflow: hidden;
}

.picker__header {
  padding: 16px 18px 8px;
}
.picker__title {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-t1);
  margin: 0;
  letter-spacing: -0.01em;
}
.picker__desc {
  margin: 6px 0 0;
  font-size: 0.75rem;
  color: var(--color-t2);
  line-height: 1.55;
}

.picker__crumb {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  border-top: 1px solid var(--color-line);
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
  overflow-x: auto;
  white-space: nowrap;
}
.picker__crumb-btn {
  padding: 3px 7px;
  font-size: 0.6875rem;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
}
.picker__crumb-btn:hover:not(:disabled) {
  background: var(--color-bg);
  color: var(--color-t1);
}
.picker__crumb-btn:disabled {
  color: var(--color-t1);
  font-weight: 500;
  cursor: default;
}
.picker__crumb-btn:not(:last-child)::after {
  content: '/';
  margin-left: 8px;
  color: var(--color-t3);
}

.picker__body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
  min-height: 200px;
}
.picker__hint {
  padding: 18px 18px;
  font-size: 0.75rem;
  color: var(--color-t3);
  text-align: center;
}

.picker__row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 14px;
  font-size: 0.75rem;
  color: var(--color-t1);
  background: transparent;
  border: none;
  text-align: left;
  cursor: pointer;
}
.picker__row:hover {
  background: var(--color-bg2);
}
.picker__row-icon {
  width: 16px;
  height: 16px;
  color: var(--color-t3);
  flex-shrink: 0;
}
.picker__row-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
}
.picker__row-chev {
  width: 14px;
  height: 14px;
  color: var(--color-t3);
  flex-shrink: 0;
}

.picker__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-top: 1px solid var(--color-line);
}
.picker__current {
  font-size: 0.6875rem;
  color: var(--color-t2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.picker__current b {
  color: var(--color-t1);
  font-weight: 500;
}
.picker__actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

/* Re-use the dialog-* enter/leave from DialogHost via shared backdrop
   styling above — but the modal-card transition needs a local copy
   because it's scoped. */
.dialog-enter-active, .dialog-leave-active { transition: opacity 0.15s ease; }
.dialog-enter-active .picker, .dialog-leave-active .picker {
  transition: transform 0.18s cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.15s;
}
.dialog-enter-from, .dialog-leave-to { opacity: 0; }
.dialog-enter-from .picker, .dialog-leave-to .picker {
  transform: translateY(8px) scale(0.98);
  opacity: 0;
}
</style>
