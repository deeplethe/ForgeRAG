<template>
  <div class="md-preview">
    <div class="md-preview__toolbar">
      <div class="md-preview__tabs">
        <button
          class="md-preview__tab"
          :class="{ 'is-active': mode === 'rendered' }"
          @click="mode = 'rendered'"
        >Rendered</button>
        <button
          class="md-preview__tab"
          :class="{ 'is-active': mode === 'raw' }"
          @click="mode = 'raw'"
        >Raw</button>
      </div>
    </div>
    <div class="md-preview__body">
      <div v-if="loading" class="md-preview__hint">Loading…</div>
      <div v-else-if="error" class="md-preview__hint md-preview__hint--err">
        Couldn't load file: {{ error }}
      </div>
      <MarkdownBody
        v-else-if="mode === 'rendered'"
        class="md-preview__rendered"
        :source="source"
      />
      <pre v-else class="md-preview__raw">{{ source }}</pre>
    </div>
  </div>
</template>

<script setup>
/**
 * Markdown preview with rendered ↔ raw toggle.
 *
 * Defers the actual markdown rendering + typography to the shared
 * ``<MarkdownBody>`` component, so the ``.md`` file viewer always
 * looks identical to the chat agent reply (same marked + katex
 * pipeline, same h1-h4 / list / code / table styling).
 *
 * The file body is fetched once on mount via the inline preview URL.
 * Auth flows through cookies / Authorization headers the same way
 * the rest of the app does — there's no separate "fetch this
 * file's text" endpoint, just the inline-disposition variant of
 * the workdir download endpoint.
 */
import { onMounted, ref, watch } from 'vue'
import MarkdownBody from '@/components/MarkdownBody.vue'

const props = defineProps({
  url: { type: String, required: true },
})

const mode = ref('rendered')
const source = ref('')
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await fetch(props.url, { credentials: 'include' })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    source.value = await r.text()
  } catch (e) {
    error.value = e?.message || String(e)
    source.value = ''
  } finally {
    loading.value = false
  }
}

onMounted(load)
// If the modal stays open while the parent swaps to a different
// file, refetch instead of showing stale content.
watch(() => props.url, () => { load() })
</script>

<style scoped>
.md-preview {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  background: var(--color-bg);
}
.md-preview__toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
  flex-shrink: 0;
}
.md-preview__tabs {
  display: inline-flex;
  gap: 0;
  padding: 2px;
  border: 1px solid var(--color-line);
  border-radius: 6px;
  background: var(--color-bg);
}
.md-preview__tab {
  padding: 3px 10px;
  font-size: 0.6875rem;
  color: var(--color-t2);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.md-preview__tab:hover { color: var(--color-t1); }
.md-preview__tab.is-active {
  background: var(--color-bg-selected, var(--color-bg3));
  color: var(--color-t1);
}

.md-preview__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 24px 32px;
}
.md-preview__hint {
  font-size: 0.75rem;
  color: var(--color-t3);
  text-align: center;
  padding: 32px 16px;
}
.md-preview__hint--err { color: #ef4444; }

.md-preview__rendered {
  font-size: 0.8125rem;
  color: var(--color-t1);
  line-height: 1.65;
  max-width: 760px;
  margin: 0 auto;
}

.md-preview__raw {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.75rem;
  line-height: 1.55;
  color: var(--color-t1);
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
</style>
