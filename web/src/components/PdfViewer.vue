<script setup>
/**
 * PdfViewer — renders PDF with pdf.js, supports page navigation
 * and block bbox highlighting.
 *
 * Props:
 *   url           - PDF file URL (from /api/v1/files/{id}/preview)
 *   page          - target page to scroll to (1-based)
 *   highlightBlocks - array of { page_no, bbox: {x0,y0,x1,y1} } to highlight
 */
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { MagnifyingGlassPlusIcon, MagnifyingGlassMinusIcon, ArrowDownTrayIcon } from '@heroicons/vue/24/outline'
import * as pdfjsLib from 'pdfjs-dist'
import { TextLayer } from 'pdfjs-dist'

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.mjs',
  import.meta.url,
).href

const props = defineProps({
  url: { type: String, default: '' },
  page: { type: Number, default: 1 },
  highlightBlocks: { type: Array, default: () => [] },
  noScroll: { type: Boolean, default: false },
  maxScale: { type: Number, default: 1.8 },
  /** Download URL for the PDF (or converted PDF) */
  downloadUrl: { type: String, default: '' },
  /** Download URL for the original source file (if different from PDF) */
  sourceDownloadUrl: { type: String, default: '' },
  /** Label for source download button, e.g. "DOCX" */
  sourceLabel: { type: String, default: '' },
})

const emit = defineEmits(['pdfClick'])

const container = ref(null)
const totalPages = ref(0)
const currentScale = ref(1.0)
const loading = ref(false)
const ready = ref(false)

let pdfDoc = null
const pageEntries = new Map()  // pageNum → { wrapper, overlay, viewport, rendered }
let observer = null
const BUFFER_PX = 600  // pre-render pages within 600px of viewport

/* ── Load PDF ── */
async function loadPdf(url) {
  if (!url) return
  loading.value = true
  ready.value = false
  try {
    if (pdfDoc) { pdfDoc.destroy(); pdfDoc = null }
    pdfDoc = await pdfjsLib.getDocument({ url, disableAutoFetch: false }).promise
    totalPages.value = pdfDoc.numPages
    await layoutAllPages()
    ready.value = true
    await nextTick()
    applyHighlightsAndScroll()
  } catch (e) {
    console.error('PDF load error:', e)
  }
  loading.value = false
}

/* ── Layout: create sized placeholders for all pages (no canvas yet) ── */
async function layoutAllPages(overrideScale) {
  if (!pdfDoc || !container.value) return
  const el = container.value

  // Cleanup
  if (observer) { observer.disconnect(); observer = null }
  el.querySelectorAll('.pdf-page-wrapper').forEach(w => w.remove())
  pageEntries.clear()

  // Compute scale
  if (overrideScale != null) {
    currentScale.value = overrideScale
  } else {
    const containerWidth = el.clientWidth - 16
    const firstPage = await pdfDoc.getPage(1)
    const baseViewport = firstPage.getViewport({ scale: 1.0 })
    currentScale.value = Math.min(props.maxScale, containerWidth / baseViewport.width)
  }

  // Fetch all page objects in parallel for fast dimension lookup
  const pages = await Promise.all(
    Array.from({ length: pdfDoc.numPages }, (_, i) => pdfDoc.getPage(i + 1))
  )

  // Create placeholder wrappers (no canvas rendering)
  for (let i = 0; i < pages.length; i++) {
    const pageNum = i + 1
    const viewport = pages[i].getViewport({ scale: currentScale.value })

    const wrapper = document.createElement('div')
    wrapper.className = 'pdf-page-wrapper'
    wrapper.dataset.page = pageNum
    wrapper.style.cssText = `position:relative;width:${viewport.width}px;height:${viewport.height}px;margin:0 auto 8px auto;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.12);`

    const overlay = document.createElement('div')
    overlay.className = 'pdf-highlight-overlay'
    overlay.style.cssText = 'position:absolute;inset:0;pointer-events:none;'
    wrapper.appendChild(overlay)

    el.appendChild(wrapper)
    pageEntries.set(pageNum, { wrapper, overlay, viewport, rendered: false })
  }

  // IntersectionObserver triggers canvas rendering for visible pages
  observer = new IntersectionObserver((entries) => {
    for (const ioEntry of entries) {
      if (ioEntry.isIntersecting) {
        const pageNum = parseInt(ioEntry.target.dataset.page, 10)
        renderPage(pageNum)
      }
    }
  }, { root: el, rootMargin: `${BUFFER_PX}px 0px ${BUFFER_PX}px 0px` })

  pageEntries.forEach(({ wrapper }) => observer.observe(wrapper))
}

/* ── Render a single page (canvas + text layer) ── */
async function renderPage(pageNum) {
  const entry = pageEntries.get(pageNum)
  if (!entry || entry.rendered || !pdfDoc) return
  entry.rendered = true

  const page = await pdfDoc.getPage(pageNum)
  const { viewport, wrapper, overlay } = entry

  const canvas = document.createElement('canvas')
  const dpr = window.devicePixelRatio || 1
  canvas.width = viewport.width * dpr
  canvas.height = viewport.height * dpr
  canvas.style.width = viewport.width + 'px'
  canvas.style.height = viewport.height + 'px'
  wrapper.insertBefore(canvas, overlay)

  const textLayerDiv = document.createElement('div')
  textLayerDiv.className = 'pdf-text-layer'
  textLayerDiv.style.cssText = `position:absolute;inset:0;overflow:hidden;line-height:1;`
  // pdfjs-dist 4.x+ ``TextLayer`` positions every text span via the
  // ``--scale-factor`` CSS variable on the container. Without it, text
  // is laid out at the default scale 1.0 and drifts away from the
  // canvas as soon as the viewport scale isn't 1 (which it never is
  // — we always fit-to-width). Set it to the current viewport scale
  // before rendering so the text layer tracks the canvas exactly.
  textLayerDiv.style.setProperty('--scale-factor', String(viewport.scale))
  wrapper.insertBefore(textLayerDiv, overlay)

  const ctx = canvas.getContext('2d')
  ctx.scale(dpr, dpr)
  await page.render({ canvasContext: ctx, viewport }).promise

  const textContent = await page.getTextContent()
  const textLayer = new TextLayer({
    textContentSource: textContent,
    container: textLayerDiv,
    viewport,
  })
  await textLayer.render()
}

/* ── Combined: draw highlights + scroll ── */
async function applyHighlightsAndScroll() {
  if (!ready.value) return

  // 1. Clear all overlays
  pageEntries.forEach(({ overlay }) => { overlay.innerHTML = '' })

  const blocks = props.highlightBlocks
  let scrollTarget = props.page

  // 2. Draw highlights
  if (blocks?.length) {
    for (const block of blocks) {
      const entry = pageEntries.get(block.page_no)
      if (!entry) continue
      const { viewport, overlay } = entry
      const b = block.bbox
      if (!b) continue

      // bbox is in PDF coordinates (origin bottom-left)
      // convertToViewportPoint converts to screen coords (origin top-left)
      const [sx0, sy0] = viewport.convertToViewportPoint(b.x0, b.y0)
      const [sx1, sy1] = viewport.convertToViewportPoint(b.x1, b.y1)

      const left = Math.min(sx0, sx1)
      const top = Math.min(sy0, sy1)
      const width = Math.abs(sx1 - sx0)
      const height = Math.abs(sy1 - sy0)

      const rect = document.createElement('div')
      rect.style.cssText = `position:absolute;left:${left}px;top:${top}px;width:${width}px;height:${height}px;background:rgba(59,130,246,0.15);border:1.5px solid rgba(59,130,246,0.5);border-radius:2px;pointer-events:none;transition:opacity .2s;`
      overlay.appendChild(rect)
    }
    // Scroll to first highlighted block's page
    scrollTarget = blocks[0].page_no
  }

  // 3. Scroll — instant jump for far pages, smooth for nearby
  if (!props.noScroll && scrollTarget >= 1 && container.value) {
    const entry = pageEntries.get(scrollTarget)
    if (entry) {
      const containerRect = container.value.getBoundingClientRect()
      const wrapperRect = entry.wrapper.getBoundingClientRect()
      const dist = Math.abs(wrapperRect.top - containerRect.top)
      const far = dist > containerRect.height * 3
      const behavior = far ? 'instant' : 'smooth'

      const firstRect = entry.overlay.querySelector('div')
      if (firstRect) {
        // Jump to page first if far, then fine-scroll to highlight
        if (far) entry.wrapper.scrollIntoView({ behavior: 'instant', block: 'start' })
        await nextTick()
        const rectPos = firstRect.getBoundingClientRect()
        const newContainerRect = container.value.getBoundingClientRect()
        const scrollOffset = rectPos.top - newContainerRect.top - 60
        container.value.scrollBy({ top: scrollOffset, behavior: far ? 'instant' : 'smooth' })
      } else {
        entry.wrapper.scrollIntoView({ behavior, block: 'start' })
      }
    }
  }
}

/* ── Watchers ── */
watch(() => props.url, (url) => { if (url) loadPdf(url) })

// Use a single watcher for both page and highlights
// deep: true ensures we detect content changes inside the highlights array
watch(
  [() => props.page, () => props.highlightBlocks],
  () => { applyHighlightsAndScroll() },
  { deep: true },
)

onMounted(() => {
  if (props.url) loadPdf(props.url)
})

onUnmounted(() => {
  if (observer) { observer.disconnect(); observer = null }
  if (pdfDoc) pdfDoc.destroy()
})

/* ── Zoom ── */
const ZOOM_STEP = 0.15
const MIN_SCALE = 0.4

async function zoomIn() {
  const next = Math.min(currentScale.value + ZOOM_STEP, props.maxScale + 1.0)
  await reRenderAtScale(next)
}
async function zoomOut() {
  const next = Math.max(currentScale.value - ZOOM_STEP, MIN_SCALE)
  await reRenderAtScale(next)
}
async function zoomReset() {
  if (!pdfDoc || !container.value) return
  const firstPage = await pdfDoc.getPage(1)
  const bv = firstPage.getViewport({ scale: 1.0 })
  const fitScale = Math.min(props.maxScale, (container.value.clientWidth - 16) / bv.width)
  await reRenderAtScale(fitScale)
}
async function reRenderAtScale(scale) {
  currentScale.value = scale
  await layoutAllPages(scale)
  await nextTick()
  applyHighlightsAndScroll()
}

const scalePercent = ref(100)
watch(currentScale, (v) => { scalePercent.value = Math.round(v * 100) })

/* ── Click on PDF → emit PDF coordinates ── */
function onContainerClick(e) {
  if (!ready.value) return
  // Ignore if user is selecting text
  const sel = window.getSelection()
  if (sel && sel.toString().length > 0) return
  // Walk up from click target to find the page wrapper
  let wrapper = e.target.closest('.pdf-page-wrapper')
  if (!wrapper) return
  const pageNum = parseInt(wrapper.dataset.page, 10)
  const entry = pageEntries.get(pageNum)
  if (!entry) return

  // Get click position relative to the wrapper (screen coords, origin top-left)
  const rect = wrapper.getBoundingClientRect()
  const sx = e.clientX - rect.left
  const sy = e.clientY - rect.top

  // Convert screen coords → PDF coords (origin bottom-left)
  // viewport.convertToPdfPoint does the inverse of convertToViewportPoint
  const [pdfX, pdfY] = entry.viewport.convertToPdfPoint(sx, sy)

  emit('pdfClick', { page_no: pageNum, x: pdfX, y: pdfY })
}

/* ── Expose for parent to force scroll ── */
defineExpose({ scrollToPage: (p) => { applyHighlightsAndScroll() } })
</script>

<template>
  <div class="pdf-viewer-root flex flex-col h-full">
    <!-- Page info bar -->
    <div class="shrink-0 px-3 py-1.5 border-b border-line flex items-center justify-between text-[9px] text-t3">
      <span v-if="totalPages">{{ totalPages }} pages</span>
      <span v-if="loading" class="text-brand animate-pulse">Loading...</span>
      <div class="flex items-center gap-1">
        <!-- Zoom controls -->
        <button @click="zoomOut" class="p-0.5 rounded hover:bg-bg2 transition-colors" title="Zoom out">
          <MagnifyingGlassMinusIcon class="w-3.5 h-3.5" />
        </button>
        <span class="w-8 text-center tabular-nums">{{ scalePercent }}%</span>
        <button @click="zoomIn" class="p-0.5 rounded hover:bg-bg2 transition-colors" title="Zoom in">
          <MagnifyingGlassPlusIcon class="w-3.5 h-3.5" />
        </button>
        <button @click="zoomReset" class="px-1 py-0.5 rounded hover:bg-bg2 transition-colors text-[8px]" title="Fit width">Fit</button>

        <!-- Separator -->
        <span v-if="downloadUrl || sourceDownloadUrl" class="mx-1 h-3 border-l border-line"></span>

        <!-- Download PDF -->
        <a v-if="downloadUrl" :href="downloadUrl" target="_blank"
           class="p-0.5 rounded hover:bg-bg2 transition-colors flex items-center gap-0.5" title="Download PDF">
          <ArrowDownTrayIcon class="w-3.5 h-3.5" />
          <span>PDF</span>
        </a>

        <!-- Download source (only if different from PDF) -->
        <a v-if="sourceDownloadUrl" :href="sourceDownloadUrl" target="_blank"
           class="p-0.5 rounded hover:bg-bg2 transition-colors flex items-center gap-0.5" :title="'Download ' + (sourceLabel || 'source')">
          <ArrowDownTrayIcon class="w-3.5 h-3.5" />
          <span>{{ sourceLabel || 'Source' }}</span>
        </a>
      </div>
    </div>
    <!-- Scrollable PDF canvas container -->
    <div ref="container" class="flex-1 overflow-y-auto bg-neutral-100 dark:bg-neutral-800 p-2 " @click="onContainerClick" />
  </div>
</template>

<style>
/* Mirrors the rules from pdfjs-dist's official ``pdf_viewer.css``
   (.textLayer + .textLayer :is(span,br)) — pdfjs's TextLayer applies
   ``transform: scaleX(...) scale(...) rotate(...)`` per span based on
   canvas-measured text width; without ``transform-origin: 0% 0%`` the
   transform pivots around the span centre and the text drifts away
   from the canvas glyph it's meant to overlay. */
.pdf-text-layer {
  position: absolute;
  inset: 0;
  overflow: clip;
  opacity: 0.25;
  line-height: 1;
  text-align: initial;
  -webkit-text-size-adjust: none;
  -moz-text-size-adjust: none;
  text-size-adjust: none;
  forced-color-adjust: none;
  transform-origin: 0 0;
}
.pdf-text-layer :is(span, br) {
  color: transparent;
  position: absolute;
  white-space: pre;
  cursor: text;
  transform-origin: 0% 0%;
}
.pdf-text-layer span::selection {
  background: rgba(120, 120, 120, 0.35);
}
</style>
