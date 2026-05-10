<!--
  ImageViewer — viewer for image-as-document uploads (PNG / JPEG /
  WEBP / GIF / BMP / TIFF).

  Image-as-document docs have a single IMAGE block whose ``bbox`` is
  the sentinel ``(0,0,0,0)`` — there's no internal spatial layout to
  highlight. So this viewer is the simplest possible thing: an
  ``<img>`` tag pointing at the file-preview route, with mouse-wheel
  zoom and click-drag pan for inspection. No PDF.js, no canvas, no
  text layer.

  Mirrors PdfViewer's outer prop / event surface (``url``,
  ``downloadUrl``, ``sourceDownloadUrl``, ``sourceLabel``) so
  DocDetail can drop it in without per-viewer plumbing — but
  intentionally drops the ``page``, ``highlightBlocks``, and
  ``pdf-click`` ones because they don't apply.
-->
<script setup>
import { computed, ref } from 'vue'
import { Download, Maximize2 } from 'lucide-vue-next'

const props = defineProps({
  url: { type: String, required: true },
  // Optional: separate download URL (defaults to ``url`` if blank).
  downloadUrl: { type: String, default: '' },
  // Filename + label, displayed in the toolbar.
  filename: { type: String, default: '' },
})

// ── Zoom + pan state ─────────────────────────────────────────────
// Held outside reactive system would be premature for this size of
// component — refs are fine. Both reset on ``onLoad`` so navigating
// between image docs starts at a sensible default.
const zoom = ref(1)
const panX = ref(0)
const panY = ref(0)
const dragging = ref(false)
const dragStart = ref({ x: 0, y: 0, panX: 0, panY: 0 })

const transformStyle = computed(() => ({
  transform: `translate(${panX.value}px, ${panY.value}px) scale(${zoom.value})`,
  // ``transition: none`` while dragging keeps the pan crisp; on
  // wheel zoom we let it ease in for visual smoothness.
  transition: dragging.value ? 'none' : 'transform 0.12s ease-out',
}))

function onWheel(e) {
  e.preventDefault()
  // Wheel up → zoom in. Multiplicative step (5%) so zooming feels
  // proportional regardless of current scale.
  const factor = e.deltaY < 0 ? 1.1 : 0.9
  const next = Math.max(0.1, Math.min(8, zoom.value * factor))
  zoom.value = next
}

function onMouseDown(e) {
  if (e.button !== 0) return
  dragging.value = true
  dragStart.value = {
    x: e.clientX,
    y: e.clientY,
    panX: panX.value,
    panY: panY.value,
  }
}
function onMouseMove(e) {
  if (!dragging.value) return
  panX.value = dragStart.value.panX + (e.clientX - dragStart.value.x)
  panY.value = dragStart.value.panY + (e.clientY - dragStart.value.y)
}
function onMouseUp() {
  dragging.value = false
}

function reset() {
  zoom.value = 1
  panX.value = 0
  panY.value = 0
}

const dl = computed(() => props.downloadUrl || props.url)
</script>

<template>
  <div class="image-viewer" @wheel="onWheel" @mousedown="onMouseDown" @mousemove="onMouseMove" @mouseup="onMouseUp"
    @mouseleave="onMouseUp">
    <!-- Toolbar — anchored top-right, mirrors PdfViewer's idiom. -->
    <div class="iv-toolbar">
      <button class="iv-btn" @click="reset" title="Reset zoom + pan">
        <Maximize2 :size="14" :stroke-width="1.6" />
      </button>
      <a class="iv-btn" :href="dl" :download="filename || true" title="Download original">
        <Download :size="14" :stroke-width="1.6" />
      </a>
      <span class="iv-zoom">{{ Math.round(zoom * 100) }}%</span>
    </div>

    <!-- The image. ``draggable=false`` prevents the browser's native
         drag-image behaviour from fighting our pan handler. -->
    <div class="iv-stage">
      <img class="iv-img" :src="url" :style="transformStyle" :alt="filename" draggable="false" @load="reset" />
    </div>
  </div>
</template>

<style scoped>
.image-viewer {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: var(--color-bg2);
  cursor: grab;
  user-select: none;
}

.image-viewer:active {
  cursor: grabbing;
}

.iv-stage {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.iv-img {
  max-width: 90%;
  max-height: 90%;
  object-fit: contain;
  transform-origin: center center;
  pointer-events: none;
  /* image-rendering: -webkit-optimize-contrast lets diagrams/charts
     render crisp at zoom levels above 1, instead of bilinear-blurry. */
  image-rendering: -webkit-optimize-contrast;
}

.iv-toolbar {
  position: absolute;
  top: 12px;
  right: 12px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 6px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.12);
  z-index: 10;
}

.iv-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: var(--color-t2);
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.1s, color 0.1s;
  text-decoration: none;
}

.iv-btn:hover {
  background: var(--color-bg2);
  color: var(--color-t1);
}

.iv-zoom {
  font-size: 0.625rem;
  color: var(--color-t3);
  font-family: var(--font-mono, ui-monospace, monospace);
  padding: 0 6px;
  min-width: 36px;
  text-align: right;
}
</style>
