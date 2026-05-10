<template>
  <div class="video-preview">
    <video
      :src="url"
      class="video-preview__el"
      controls
      preload="metadata"
      @error="error = true"
    />
    <div v-if="error" class="video-preview__error">
      Couldn't play this video. <a :href="url" target="_blank" rel="noopener">Open in new tab</a>.
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

defineProps({
  url: { type: String, required: true },
  filename: { type: String, default: '' },
})

const error = ref(false)
</script>

<style scoped>
.video-preview {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  background: #000;
}
.video-preview__el {
  max-width: 100%;
  max-height: 100%;
  outline: none;
}
.video-preview__error {
  position: absolute;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  padding: 8px 16px;
  background: rgba(0, 0, 0, 0.7);
  color: #fff;
  font-size: 0.75rem;
  border-radius: 6px;
}
.video-preview__error a {
  color: #93c5fd;
  text-decoration: underline;
}
</style>
