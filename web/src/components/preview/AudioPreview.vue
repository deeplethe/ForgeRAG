<template>
  <div class="audio-preview">
    <div class="audio-preview__card">
      <Music :size="48" :stroke-width="1.25" class="audio-preview__icon" />
      <div class="audio-preview__name">{{ filename || 'Audio' }}</div>
      <audio
        :src="url"
        class="audio-preview__el"
        controls
        preload="metadata"
        @error="error = true"
      />
      <div v-if="error" class="audio-preview__error">
        Couldn't play this audio.
        <a :href="url" target="_blank" rel="noopener">Open in new tab</a>.
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { Music } from 'lucide-vue-next'

defineProps({
  url: { type: String, required: true },
  filename: { type: String, default: '' },
})

const error = ref(false)
</script>

<style scoped>
.audio-preview {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  background: var(--color-bg);
}
.audio-preview__card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 32px 48px;
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
  border-radius: 12px;
  min-width: 360px;
  max-width: 80%;
}
.audio-preview__icon { color: var(--color-t3); }
.audio-preview__name {
  font-size: 12px;
  color: var(--color-t1);
  word-break: break-all;
  text-align: center;
}
.audio-preview__el { width: 100%; outline: none; }
.audio-preview__error { font-size: 11px; color: var(--color-t3); }
.audio-preview__error a {
  color: var(--color-accent, #3b82f6);
  text-decoration: underline;
}
</style>
