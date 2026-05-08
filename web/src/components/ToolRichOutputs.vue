<script setup>
/**
 * Inline renderer for python_exec rich outputs (Phase 2.5).
 *
 * Each entry is one figure / DataFrame HTML / Plotly JSON / etc.
 * persisted to the project workdir at ``scratch/_rich_outputs/<file>``.
 * The backend's persister (``rich_output_persister.py``) writes the
 * file and emits a ref:
 *
 *     { kind, mime, path, size_bytes, project_id }
 *
 * We render based on MIME:
 *   - image/* (png, jpg, gif, svg)  → <img> via project file-download
 *   - text/html / *html              → sandboxed <iframe srcdoc>
 *                                       (DataFrame.to_html, etc.)
 *   - application/vnd.plotly.* / vega → "Open figure" download link
 *                                       (plotly.js too heavy for the
 *                                       agent trace; user clicks
 *                                       through if interested)
 *   - application/json / others      → small "View N KB JSON" link
 *
 * No prop-drilling: each ref carries its own ``project_id`` so the
 * URL is self-contained. Survives chat reload (files are on disk),
 * survives SSE disconnect (re-fetch from disk on next render).
 */
import { computed } from 'vue'

const props = defineProps({
  outputs: { type: Array, required: true },
})

function downloadUrl(o) {
  if (!o.project_id || !o.path) return ''
  // Same auth-cookie route the file browser uses; project read
  // access already gates this, so a non-member trying to deep-link
  // would 404 just like any other workdir file fetch.
  const enc = encodeURIComponent(o.path)
  return `/api/v1/projects/${o.project_id}/files/download?path=${enc}`
}

function isImage(mime) {
  return typeof mime === 'string' && mime.startsWith('image/')
}
function isHtml(mime) {
  return mime === 'text/html'
}
function isInteractiveJson(mime) {
  return typeof mime === 'string' && (
    mime.startsWith('application/vnd.plotly') ||
    mime.startsWith('application/vnd.vega') ||
    mime.startsWith('application/vnd.vegalite')
  )
}
function isJsonLike(mime) {
  return mime === 'application/json' || isInteractiveJson(mime)
}

function fmtSize(bytes) {
  if (bytes == null) return ''
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

// Cap inline iframe size — DataFrame HTML can run long. User can
// click through to see the full file via the download link if
// they want unbounded scroll.
const _MAX_IFRAME_HEIGHT = 480

const items = computed(() => props.outputs.map((o, i) => ({
  ...o,
  _idx: i,
  _url: downloadUrl(o),
  _isImage: isImage(o.mime),
  _isHtml: isHtml(o.mime),
  _isInteractiveJson: isInteractiveJson(o.mime),
  _isJsonLike: isJsonLike(o.mime),
  _size: fmtSize(o.size_bytes),
})))
</script>

<template>
  <div class="rich-outputs" v-if="items.length">
    <div v-for="o in items" :key="o._idx" class="rich-output">
      <!-- Image: render inline. ``loading=lazy`` so a chat with many
           figures doesn't block first paint; ``decoding=async``
           keeps decoding off the main thread. -->
      <a
        v-if="o._isImage"
        :href="o._url"
        target="_blank"
        rel="noopener"
        class="image-link"
        :title="`${o.mime} · ${o._size} · click to open`"
      >
        <img :src="o._url" :alt="o.path" loading="lazy" decoding="async" />
      </a>

      <!-- HTML: sandboxed iframe pointing at the download URL. The
           ``sandbox`` attribute (deliberately empty allow-list)
           blocks scripts / forms / popups — DataFrame.to_html
           output is plain styled tables, no JS needed. -->
      <iframe
        v-else-if="o._isHtml"
        :src="o._url"
        :title="o.path"
        sandbox=""
        loading="lazy"
        :style="{ height: _MAX_IFRAME_HEIGHT + 'px' }"
        class="html-frame"
      />

      <!-- Interactive viz JSON (Plotly / Vega) — defer rendering;
           offer a download link. Phase 6 polish could embed plotly.js
           lazily; for v1 the file is on disk and downloadable. -->
      <a
        v-else-if="o._isInteractiveJson"
        :href="o._url"
        target="_blank"
        rel="noopener"
        class="link-fallback"
      >
        📈 {{ o.mime.split('/')[1] || 'figure' }} ({{ o._size }})
      </a>

      <!-- JSON / other text bundles — link only. -->
      <a
        v-else-if="o._isJsonLike"
        :href="o._url"
        target="_blank"
        rel="noopener"
        class="link-fallback"
      >
        {{ o.mime }} · {{ o._size }}
      </a>

      <!-- Fallback: just a link for unknown MIMEs. -->
      <a
        v-else
        :href="o._url"
        target="_blank"
        rel="noopener"
        class="link-fallback"
      >
        {{ o.mime }} ({{ o._size }})
      </a>
    </div>
  </div>
</template>

<style scoped>
.rich-outputs {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 8px 0 4px 24px;  /* indent under the chip headline */
}
.rich-output {
  display: block;
  max-width: 100%;
}

.image-link {
  display: inline-block;
  border: 1px solid var(--color-line);
  border-radius: 6px;
  overflow: hidden;
  background: var(--color-bg2, #fff);
  line-height: 0;  /* avoid <img> baseline padding */
}
.image-link img {
  display: block;
  max-width: 100%;
  max-height: 480px;
  height: auto;
  width: auto;
}

.html-frame {
  width: 100%;
  border: 1px solid var(--color-line);
  border-radius: 6px;
  background: var(--color-bg2, #fff);
}

.link-fallback {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  font-size: 12px;
  color: var(--color-t2);
  border: 1px solid var(--color-line);
  border-radius: 6px;
  background: var(--color-bg3, transparent);
  text-decoration: none;
  transition: background-color .15s, border-color .15s;
}
.link-fallback:hover {
  background: var(--color-bg2);
  border-color: var(--color-line2);
}
</style>
