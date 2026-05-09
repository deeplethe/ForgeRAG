<template>
  <div class="image-preview" @wheel.prevent="onWheel">
    <img
      v-if="!error"
      :src="url"
      :alt="filename"
      class="image-preview__img"
      :class="{ 'is-zoomed': zoom !== 1 }"
      :style="transform"
      @load="onLoad"
      @error="error = true"
      @click="toggleZoom"
      draggable="false"
    />
    <div v-else class="image-preview__error">
      Couldn't load image. <a :href="url" target="_blank" rel="noopener">Open in new tab</a>.
    </div>
  </div>
</template>

<script setup>
/**
 * Workbench image preview.
 *
 * Lightweight on purpose — zoom in/out via wheel, click to fit /
 * 100%, no pan because the modal already gives us a finite viewport
 * and double-clicking-to-pan would compete with the modal's own
 * close-on-backdrop affordance. The Library has a heavier
 * ImageViewer with pan/zoom toolbar; that one's tuned for browsing
 * an indexed image-as-document, this one's the "quick peek" peer.
 */
import { computed, ref } from 'vue'

defineProps({
  url: { type: String, required: true },
  filename: { type: String, default: '' },
})

const zoom = ref(1)
const error = ref(false)

const transform = computed(() => ({
  transform: `scale(${zoom.value})`,
  transition: 'transform 0.12s ease-out',
}))

function onLoad() {
  zoom.value = 1
  error.value = false
}

function onWheel(e) {
  // Up → zoom in. Multiplicative step so successive wheels feel
  // proportional regardless of current scale.
  const factor = e.deltaY < 0 ? 1.1 : 0.9
  zoom.value = Math.max(0.2, Math.min(8, zoom.value * factor))
}

function toggleZoom() {
  zoom.value = zoom.value === 1 ? 2 : 1
}
</script>

<style scoped>
.image-preview {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  overflow: auto;
  background: var(--color-bg);
}
.image-preview__img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  cursor: zoom-in;
  user-select: none;
}
.image-preview__img.is-zoomed { cursor: zoom-out; }
.image-preview__error {
  font-size: 12px;
  color: var(--color-t3);
  padding: 24px;
}
.image-preview__error a {
  color: var(--color-accent, #3b82f6);
  text-decoration: underline;
}
</style>
