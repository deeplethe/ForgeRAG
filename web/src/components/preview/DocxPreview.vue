<template>
  <div class="docx-preview">
    <div class="docx-preview__body">
      <div v-if="loading" class="docx-preview__hint">Loading…</div>
      <div v-else-if="error" class="docx-preview__hint docx-preview__hint--err">
        Couldn't render document: {{ error }}
      </div>
      <div
        v-else
        class="docx-preview__page"
        v-html="html"
      />
    </div>
    <div v-if="messages.length" class="docx-preview__msgs">
      <span class="docx-preview__msgs-icon">⚠</span>
      <span>{{ messages.length }} formatting note{{ messages.length === 1 ? '' : 's' }} during conversion</span>
    </div>
  </div>
</template>

<script setup>
/**
 * Word .docx preview via ``mammoth``.
 *
 * mammoth converts docx XML to clean HTML (it deliberately does NOT
 * try to preserve Word's exact visual layout — instead it preserves
 * semantic structure: headings, lists, tables, links, images). Loaded
 * lazily — only fetched when the user opens a docx file (~200KB).
 *
 * .doc (the legacy binary format) is intentionally NOT supported;
 * mammoth requires .docx. Older files render via the modal's
 * 'unsupported' fallback (download → open in Word / LibreOffice).
 */
import { onMounted, ref, watch } from 'vue'

const props = defineProps({
  url: { type: String, required: true },
})

const html = ref('')
const messages = ref([])
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  messages.value = []
  html.value = ''
  try {
    const r = await fetch(props.url, { credentials: 'include' })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    const buf = await r.arrayBuffer()
    const mammoth = await import('mammoth/mammoth.browser.js')
    const result = await mammoth.convertToHtml(
      { arrayBuffer: buf },
      // No styleMap override — defaults give us h1-h6 / p / ul / ol /
      // table / a / img which the .docx-preview__page styles below
      // already handle.
      {},
    )
    html.value = result.value || ''
    messages.value = result.messages || []
  } catch (e) {
    error.value = e?.message || String(e)
    html.value = ''
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.url, () => { load() })
</script>

<style scoped>
.docx-preview {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  background: var(--color-bg);
}
.docx-preview__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 32px 16px;
}
.docx-preview__hint {
  font-size: 12px;
  color: var(--color-t3);
  text-align: center;
  padding: 32px 16px;
}
.docx-preview__hint--err { color: #ef4444; }

/* Center the document on the body, give it a sane reading width and
   the white-page treatment people expect from a Word document.
   ``--color-bg`` already accounts for theme; the page itself stays
   white-on-dark for visual consistency with paper documents. */
.docx-preview__page {
  max-width: 760px;
  margin: 0 auto;
  padding: 56px 64px;
  background: #fff;
  color: #1f2937;
  border: 1px solid var(--color-line);
  border-radius: 4px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  font-size: 13px;
  line-height: 1.65;
}
.docx-preview__page :deep(h1) {
  font-size: 22px;
  font-weight: 600;
  margin: 0 0 12px;
}
.docx-preview__page :deep(h2) {
  font-size: 17px;
  font-weight: 600;
  margin: 18px 0 10px;
}
.docx-preview__page :deep(h3) {
  font-size: 14.5px;
  font-weight: 600;
  margin: 14px 0 8px;
}
.docx-preview__page :deep(p) { margin: 0 0 10px; }
.docx-preview__page :deep(ul),
.docx-preview__page :deep(ol) { margin: 0 0 10px; padding-left: 28px; }
.docx-preview__page :deep(li) { margin: 0 0 4px; }
.docx-preview__page :deep(table) {
  border-collapse: collapse;
  margin: 12px 0;
  font-size: 12.5px;
}
.docx-preview__page :deep(th),
.docx-preview__page :deep(td) {
  border: 1px solid #cbd5e1;
  padding: 6px 10px;
  text-align: left;
}
.docx-preview__page :deep(a) {
  color: #2563eb;
  text-decoration: underline;
}
.docx-preview__page :deep(img) {
  max-width: 100%;
  height: auto;
}

.docx-preview__msgs {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-top: 1px solid var(--color-line);
  background: var(--color-bg2);
  flex-shrink: 0;
  font-size: 11px;
  color: var(--color-warn-fg, #b45309);
}
.docx-preview__msgs-icon { font-size: 12px; }
</style>
