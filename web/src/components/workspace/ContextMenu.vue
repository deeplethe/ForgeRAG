<template>
  <Teleport to="body">
    <div
      v-if="open"
      ref="menuEl"
      class="ctx-menu"
      :style="{ top: clampedY + 'px', left: clampedX + 'px' }"
      @click.stop
      @contextmenu.prevent
    >
      <template v-for="(item, idx) in visibleItems" :key="idx">
        <div v-if="item.divider" class="ctx-divider" />
        <button
          v-else
          class="ctx-item"
          :class="{ 'ctx-item--danger': item.danger, 'ctx-item--disabled': item.disabled }"
          :disabled="item.disabled"
          @click="onClick(item)"
        >
          <span class="ctx-icon">{{ item.icon || '' }}</span>
          <span class="ctx-label">{{ item.label }}</span>
          <span v-if="item.shortcut" class="ctx-shortcut">{{ item.shortcut }}</span>
        </button>
      </template>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  x: { type: Number, default: 0 },
  y: { type: Number, default: 0 },
  items: { type: Array, default: () => [] },      // [{ label, icon?, shortcut?, action, disabled?, divider? }]
})
const emit = defineEmits(['close', 'action'])

const menuEl = ref(null)
const clampedX = ref(0)
const clampedY = ref(0)

const visibleItems = computed(() => props.items.filter(i => i && (i.divider || i.label)))

function onClick(item) {
  if (item.disabled) return
  emit('action', item.action)
  emit('close')
}

function handleDocClick(e) {
  if (!props.open) return
  if (menuEl.value && !menuEl.value.contains(e.target)) emit('close')
}
function handleEsc(e) {
  if (e.key === 'Escape' && props.open) emit('close')
}

onMounted(() => {
  document.addEventListener('mousedown', handleDocClick)
  document.addEventListener('keydown', handleEsc)
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', handleDocClick)
  document.removeEventListener('keydown', handleEsc)
})

watch(() => [props.open, props.x, props.y], async () => {
  if (!props.open) return
  await nextTick()
  const el = menuEl.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  const w = rect.width, h = rect.height
  const vw = window.innerWidth, vh = window.innerHeight
  clampedX.value = Math.min(props.x, vw - w - 4)
  clampedY.value = Math.min(props.y, vh - h - 4)
})
</script>

<style scoped>
.ctx-menu {
  position: fixed;
  z-index: 9999;
  min-width: 180px;
  padding: 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 6px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.12);
  font-size: 11px;
}
.ctx-divider {
  height: 1px;
  background: var(--color-line);
  margin: 3px 0;
}
.ctx-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 5px 8px;
  color: var(--color-t1);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  text-align: left;
}
.ctx-item:hover:not(.ctx-item--disabled) {
  background: var(--color-bg2);
}
.ctx-item--disabled {
  color: var(--color-t3);
  cursor: not-allowed;
}
.ctx-item--danger { color: #dc2626; }
.ctx-item--danger:hover:not(.ctx-item--disabled) {
  background: color-mix(in srgb, #dc2626 14%, var(--color-bg));
}
.ctx-icon { width: 14px; flex-shrink: 0; text-align: center; }
.ctx-label { flex: 1; }
.ctx-shortcut { font-size: 9px; color: var(--color-t3); }
</style>
