<template>
  <div
    ref="containerEl"
    class="marquee-container"
    @mousedown="onMouseDown"
  >
    <slot />
    <div
      v-if="active"
      class="marquee-rect"
      :style="rectStyle"
    />
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'

/**
 * Windows-style rubber-band selection. Emits `select` with an array of
 * keys whose DOM elements are currently inside the box. Target elements
 * must have a `data-selkey` attribute.
 *
 * Usage:
 *   <MarqueeSelection @select="onMarqueeSelect">
 *     <FileGrid ... />
 *   </MarqueeSelection>
 */
const emit = defineEmits(['select'])

const containerEl = ref(null)
const active = ref(false)
const start = ref({ x: 0, y: 0 })
const end = ref({ x: 0, y: 0 })

const rectStyle = computed(() => {
  const x = Math.min(start.value.x, end.value.x)
  const y = Math.min(start.value.y, end.value.y)
  const w = Math.abs(end.value.x - start.value.x)
  const h = Math.abs(end.value.y - start.value.y)
  return {
    left: x + 'px',
    top: y + 'px',
    width: w + 'px',
    height: h + 'px',
  }
})

function onMouseDown(e) {
  // Only start marquee when clicking on empty container (not on a child)
  if (e.target !== containerEl.value && e.button !== 0) return
  // Allow only left mouse button
  if (e.button !== 0) return
  // If user clicked a selectable item, bail out (that's a click-to-select)
  if (e.target.closest('[data-selkey]')) return
  const rect = containerEl.value.getBoundingClientRect()
  start.value = {
    x: e.clientX - rect.left + containerEl.value.scrollLeft,
    y: e.clientY - rect.top + containerEl.value.scrollTop,
  }
  end.value = { ...start.value }
  active.value = true
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
}

function onMouseMove(e) {
  if (!active.value) return
  const rect = containerEl.value.getBoundingClientRect()
  end.value = {
    x: e.clientX - rect.left + containerEl.value.scrollLeft,
    y: e.clientY - rect.top + containerEl.value.scrollTop,
  }
  updateHits()
}

function onMouseUp() {
  active.value = false
  document.removeEventListener('mousemove', onMouseMove)
  document.removeEventListener('mouseup', onMouseUp)
}

function updateHits() {
  if (!containerEl.value) return
  const box = {
    left: Math.min(start.value.x, end.value.x),
    top: Math.min(start.value.y, end.value.y),
    right: Math.max(start.value.x, end.value.x),
    bottom: Math.max(start.value.y, end.value.y),
  }
  const contRect = containerEl.value.getBoundingClientRect()
  const hits = []
  for (const el of containerEl.value.querySelectorAll('[data-selkey]')) {
    const r = el.getBoundingClientRect()
    const elBox = {
      left: r.left - contRect.left + containerEl.value.scrollLeft,
      top: r.top - contRect.top + containerEl.value.scrollTop,
      right: r.right - contRect.left + containerEl.value.scrollLeft,
      bottom: r.bottom - contRect.top + containerEl.value.scrollTop,
    }
    if (
      elBox.left < box.right && elBox.right > box.left &&
      elBox.top < box.bottom && elBox.bottom > box.top
    ) {
      hits.push(el.getAttribute('data-selkey'))
    }
  }
  emit('select', hits)
}

onBeforeUnmount(() => {
  document.removeEventListener('mousemove', onMouseMove)
  document.removeEventListener('mouseup', onMouseUp)
})
</script>

<style scoped>
.marquee-container {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: auto;
}
.marquee-rect {
  position: absolute;
  pointer-events: none;
  background: color-mix(in srgb, var(--color-brand) 12%, transparent);
  border: 1px solid var(--color-brand);
  z-index: 10;
}
</style>
