<template>
  <div class="html-preview">
    <div class="html-preview__toolbar">
      <div class="html-preview__tabs">
        <button
          class="html-preview__tab"
          :class="{ 'is-active': mode === 'rendered' }"
          @click="mode = 'rendered'"
        >Rendered</button>
        <button
          class="html-preview__tab"
          :class="{ 'is-active': mode === 'source' }"
          @click="mode = 'source'"
        >Source</button>
      </div>
      <span class="flex-1"></span>
      <span class="html-preview__sandbox-note">Sandboxed — no scripts, no network.</span>
    </div>

    <div class="html-preview__body">
      <div v-if="loading" class="html-preview__hint">Loading…</div>
      <div v-else-if="error" class="html-preview__hint html-preview__hint--err">
        Couldn't load file: {{ error }}
      </div>
      <iframe
        v-else-if="mode === 'rendered'"
        ref="frameEl"
        class="html-preview__frame"
        sandbox=""
        :srcdoc="sanitized"
      />
      <pre v-else class="html-preview__source">{{ source }}</pre>
    </div>
  </div>
</template>

<script setup>
/**
 * HTML preview.
 *
 * Renders user-supplied HTML inside a sandboxed iframe (``sandbox=""``
 * with NO allowances — no scripts, no forms, no top-level navigation,
 * no same-origin). The body is also DOMPurify-sanitised before
 * landing in the iframe; defence-in-depth so a forced-execute
 * iframe-bypass exploit still has no script tags to grab onto.
 *
 * dompurify loads lazily — only fetched when the user opens an HTML
 * file (~30KB). Source-mode tab just shows the raw text in a ``<pre>``
 * for users who want to read the markup itself.
 *
 * Why ``srcdoc`` over a blob URL: srcdoc keeps the iframe content as
 * an inline data attribute the browser treats as cross-origin even
 * though we own the page — combined with ``sandbox=""`` (no
 * allow-same-origin) this means the iframe can't reach our cookies,
 * localStorage, or DOM. Blob URLs would inherit our origin without
 * the explicit sandbox declaration's barrier.
 */
import { onMounted, ref, watch } from 'vue'

const props = defineProps({
  url: { type: String, required: true },
})

const mode = ref('rendered')
const source = ref('')
const sanitized = ref('')
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  source.value = ''
  sanitized.value = ''
  try {
    const r = await fetch(props.url, { credentials: 'include' })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    const text = await r.text()
    source.value = text

    // Lazy import so dompurify lands in its own chunk.
    const mod = await import('dompurify')
    const DOMPurify = mod.default || mod
    const clean = DOMPurify.sanitize(text, {
      WHOLE_DOCUMENT: true,
      // Strip scripts + event handlers + javascript: URLs by default;
      // these are dompurify's documented defaults but we pin them here
      // so a future config tweak doesn't silently relax things.
      FORBID_TAGS: ['script', 'style'],
      FORBID_ATTR: ['onerror', 'onload', 'onclick'],
    })
    // Inject a minimal stylesheet so unstyled markup is still readable
    // inside the iframe. Authors' own ``<style>`` tags were stripped
    // by FORBID_TAGS — we trade some fidelity for the strong sandbox.
    const baseCss = `
      body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
             color: #1f2937; background: #fff; margin: 24px; line-height: 1.55; }
      a { color: #2563eb; }
      pre, code { font-family: ui-monospace, Menlo, monospace; }
      table { border-collapse: collapse; }
      th, td { border: 1px solid #cbd5e1; padding: 4px 8px; }
    `
    sanitized.value = clean.includes('<head')
      ? clean.replace('<head>', `<head><style>${baseCss}</style>`)
      : `<!doctype html><html><head><style>${baseCss}</style></head><body>${clean}</body></html>`
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.url, () => { load() })
</script>

<style scoped>
.html-preview {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  background: var(--color-bg);
}
.html-preview__toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
  flex-shrink: 0;
}
.html-preview__tabs {
  display: inline-flex;
  padding: 2px;
  border: 1px solid var(--color-line);
  border-radius: 6px;
  background: var(--color-bg);
}
.html-preview__tab {
  padding: 3px 10px;
  font-size: 11px;
  color: var(--color-t2);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.html-preview__tab:hover { color: var(--color-t1); }
.html-preview__tab.is-active {
  background: var(--color-bg-selected, var(--color-bg3));
  color: var(--color-t1);
}
.html-preview__sandbox-note {
  font-size: 10.5px;
  color: var(--color-t3);
}

.html-preview__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  display: flex;
}
.html-preview__hint {
  font-size: 12px;
  color: var(--color-t3);
  text-align: center;
  padding: 32px 16px;
  margin: auto;
}
.html-preview__hint--err { color: #ef4444; }

.html-preview__frame {
  flex: 1;
  width: 100%;
  height: 100%;
  border: none;
  background: #fff;
}
.html-preview__source {
  flex: 1;
  margin: 0;
  padding: 16px 20px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-t1);
  white-space: pre-wrap;
  word-break: break-word;
  overflow: auto;
}
</style>
