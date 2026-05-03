<!--
  DocDetail — workspace-embedded document viewer.

  Replaces the previous ``<Repository :inline="true">`` mount. Five
  panels arranged as:

      ┌─────────────────────────────────────────────────────────┐
      │ TOP BAR (52px)  /breadcrumb   chips     Reparse  …  ⟵   │
      ├──────────────┬───────────────────────────┬──────────────┤
      │              │                           │  KG (mini)   │
      │   Tree       │       PDF (center)        ├──────────────┤
      │  (LLM)       │                           │  Chunks list │
      └──────────────┴───────────────────────────┴──────────────┘

  Top-bar height + padding match the workspace toolbar and the KG
  page header (px-5 py-3 + min-h-[52px]) so transitioning between
  views doesn't jitter the page header. Back button at the rightmost
  slot mirrors the trash mode's exit pattern.

  This is the **skeleton** wiring: data flows for doc/tree/chunks/
  PDF + a minimal cross-panel sync (chunk click → PDF jumps + tree
  highlights). The KG mini panel is a placeholder for a follow-up
  iteration that will render entities scoped to this document.
-->
<script setup>
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ArrowLeft, RefreshCw } from 'lucide-vue-next'
import { useVirtualizer } from '@tanstack/vue-virtual'
import {
  blockImageUrl,
  fileDownloadUrl,
  filePreviewUrl,
  getChunkByBlock,
  getDocument,
  getTree,
  listBlocks,
  listChunks,
  reparseDocument,
} from '@/api'
import DocKgMini from '@/components/workspace/DocKgMini.vue'
import ImageViewer from '@/components/ImageViewer.vue'
import PdfViewer from '@/components/PdfViewer.vue'
import TreeNode from '@/components/TreeNode.vue'

const props = defineProps({
  docId: { type: String, required: true },
})
const emit = defineEmits(['close'])

// ── State ────────────────────────────────────────────────────────
const doc = ref(null)
const tree = ref(null)
const chunks = ref([])
const allBlocks = ref([])
const loading = ref(false)

// Cross-panel selection state
const activeChunkId = ref(null)
const activeNodeId = ref(null)
const collapsedNodes = reactive(new Set())

// Tree-pane scrollbar: hidden at rest, fades in while the user is
// scrolling, fades out 800ms after they stop. Using a class toggle
// (vs `:hover`) keeps the scrollbar invisible when the cursor is
// inside the pane but the content is static — e.g. the user is
// just hovering a row to read it. Only actual scroll activity
// surfaces the indicator.
const treeScrollEl = ref(null)
let treeScrollIdleTimer = null
function onTreeScroll() {
  const el = treeScrollEl.value
  if (!el) return
  el.classList.add('is-scrolling')
  if (treeScrollIdleTimer) clearTimeout(treeScrollIdleTimer)
  treeScrollIdleTimer = setTimeout(() => {
    el.classList.remove('is-scrolling')
    treeScrollIdleTimer = null
  }, 800)
}
onBeforeUnmount(() => {
  if (treeScrollIdleTimer) clearTimeout(treeScrollIdleTimer)
  if (_pollTimer) clearTimeout(_pollTimer)
})
const pdfPage = ref(1)
const pdfHighlightBlocks = ref([])
// Live KG counts emitted from DocKgMini after each (re)build —
// shrinks to the filtered subgraph's size while a chunk is selected,
// returns to the full doc total when deselected.
const kgCounts = ref(null)
// Per-chunk expand state — clicking the row's "view" affordance
// flips it; clicking again collapses. Keyed by chunk_id so the
// state persists while the user clicks around other panels.
const expandedChunks = reactive({})
function toggleChunkExpand(chunkId) {
  expandedChunks[chunkId] = !expandedChunks[chunkId]
  invalidateChunkSize(chunkId)
}
function chunkImageUrls(c) {
  // Image-type chunks have one or more block_ids that point to
  // image crops; the block-image endpoint serves them by id.
  if (c.content_type !== 'image' || !c.block_ids?.length) return []
  return c.block_ids.map((bid) => blockImageUrl(bid))
}

// ── Computed ─────────────────────────────────────────────────────
const breadcrumb = computed(() => {
  // Show the doc's path segments. The last segment is the filename;
  // earlier segments are folders the user can click to exit detail
  // and navigate to that folder in the workspace.
  const path = doc.value?.path || ''
  return path.split('/').filter(Boolean)
})

// Path of the doc's parent folder — the "Back" arrow targets this so
// closing the detail returns the user to where they came from rather
// than wherever the workspace was last left.
const parentFolderPath = computed(() => {
  const p = doc.value?.path || ''
  if (!p || !p.includes('/')) return '/'
  const parent = p.slice(0, p.lastIndexOf('/'))
  return parent || '/'
})

// Resolve the path of a clicked breadcrumb segment.
//   ``i = 0`` is the root ``/``; we render that separately.
//   ``i >= 1`` are filesystem segments. Folder segments (everything
//   except the final filename) navigate; the filename segment is
//   inert (last crumb).
function crumbPathAtIndex(i) {
  const segs = breadcrumb.value
  if (i < 0) return '/'
  if (i >= segs.length - 1) return null  // filename → no-op
  return '/' + segs.slice(0, i + 1).join('/')
}

function onCrumbClick(i) {
  const target = crumbPathAtIndex(i)
  if (target == null) return       // last crumb (filename) is inert
  emit('close', { toPath: target })
}

const fmtSize = (n) => {
  if (!n) return ''
  if (n < 1024) return `${n}B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(1)}GB`
}

const fileType = computed(() => {
  if (!doc.value) return ''
  const fn = doc.value.filename || doc.value.file_name || ''
  const m = fn.match(/\.([^.]+)$/)
  return m ? m[1].toUpperCase() : (doc.value.format || '').toUpperCase()
})

// Image-as-document file extensions. A document with one of these
// formats is rendered with ``<ImageViewer>`` (raw ``<img>`` + zoom)
// instead of the PDF viewer — image uploads aren't wrapped in a PDF.
// See ``parser/backends/image.py`` for the parser-side counterpart.
const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'tif', 'tiff'])

const isImage = computed(() => {
  const d = doc.value
  if (!d) return false
  const fmt = (d.format || '').toLowerCase()
  return IMAGE_EXTS.has(fmt)
})

const isPdf = computed(() => {
  const d = doc.value
  if (!d) return false
  // ``isImage`` short-circuits because image docs may also have a
  // ``pdf_file_id`` set in some edge cases (we never actually do
  // that, but guard for it) and we never want to dispatch them to
  // the PDF viewer.
  if (isImage.value) return false
  return d.format === 'pdf' || !!d.pdf_file_id
})

const imageUrl = computed(() => {
  const d = doc.value
  if (!d || !isImage.value) return ''
  return d.file_id ? filePreviewUrl(d.file_id) : ''
})

const imageDownloadUrl = computed(() => {
  const d = doc.value
  if (!d || !isImage.value) return ''
  return d.file_id ? fileDownloadUrl(d.file_id) : ''
})

const pdfUrl = computed(() => {
  const d = doc.value
  if (!d || !isPdf.value) return ''
  const fid = d.pdf_file_id || d.file_id
  return fid ? filePreviewUrl(fid) : ''
})
const pdfDownloadUrl = computed(() => {
  const d = doc.value
  if (!d) return ''
  const fid = d.pdf_file_id || d.file_id
  return fid ? fileDownloadUrl(fid) : ''
})
const sourceDownloadUrl = computed(() => {
  const d = doc.value
  if (!d || !d.pdf_file_id || !d.file_id) return ''
  return fileDownloadUrl(d.file_id)
})
const sourceLabel = computed(() => {
  const d = doc.value
  if (!d?.pdf_file_id) return ''
  const name = d.file_name || d.filename || ''
  const ext = name.split('.').pop()?.toUpperCase()
  return ext || d.format?.toUpperCase() || 'Source'
})

// ── Phase / processing state ─────────────────────────────────────
// Backend pipeline goes through:
//   1. parse  (parse_completed_at) — PDF/blocks ready
//   2. tree   (structure_completed_at) — tree built
//   3. chunk  — chunks rows in DB (we infer from chunks.length)
//   4. embed  (embed_status === "done") — vector index ready
//   5. KG     (kg_status in {done, skipped}) — entities + relations
// Document.status hits "ready" after step 3 but BEFORE KG, so the
// composite "fully ready" needs the kg gate too. Until then we poll.
const phases = computed(() => {
  const d = doc.value || {}
  return {
    parsed: !!d.parse_completed_at,
    structured: !!d.structure_completed_at && !!tree.value,
    chunked: chunks.value.length > 0,
    embedded: d.embed_status === 'done' || d.embed_status === 'skipped',
    kgDone:
      d.kg_status === 'done' ||
      d.kg_status === 'skipped' ||
      d.kg_status === 'disabled',
  }
})
// Optimistic flag — flipped TRUE the moment the user clicks Reparse,
// before the backend has had a chance to set ``status=pending``.
// Without it the button stays clickable for ~2.5s (one poll cycle)
// and the user can fire duplicate jobs. Cleared on backend rejection
// (409 etc.) and naturally bridged by polling once the backend
// reflects the in-flight state.
const _optimisticReparse = ref(false)

const fullyReady = computed(() => {
  const d = doc.value
  if (!d) return false
  return d.status === 'ready' && phases.value.kgDone
})
const inFlight = computed(() => {
  if (_optimisticReparse.value) return true
  const d = doc.value
  if (!d) return false
  if (d.status === 'error') return false
  return !fullyReady.value
})
// Human-readable stage chip:
//   parsing → structuring → chunking → embedding → kg → ready
const stageLabel = computed(() => {
  const d = doc.value
  if (!d) return 'loading'
  if (d.status === 'error') return 'error'
  if (fullyReady.value) return 'ready'
  const p = phases.value
  if (!p.parsed) return 'parsing'
  if (!p.structured) return 'structuring'
  if (!p.chunked) return 'chunking'
  if (!p.embedded) return 'embedding'
  if (!p.kgDone) return 'building graph'
  return d.status || 'processing'
})

// Tree-highlight set: every ancestor of the active chunk's node, plus
// the active node itself if a tree row is the selection driver.
const highlightNodeIds = computed(() => {
  const s = new Set()
  if (activeChunkId.value) {
    const c = chunks.value.find((x) => x.chunk_id === activeChunkId.value)
    if (c) {
      ;(c.ancestor_node_ids || []).forEach((id) => s.add(id))
      if (c.node_id) s.add(c.node_id)
    }
  }
  if (activeNodeId.value) s.add(activeNodeId.value)
  return s
})

// ── Loaders ──────────────────────────────────────────────────────
async function loadAll() {
  if (!props.docId) return
  loading.value = true
  try {
    doc.value = await getDocument(props.docId)
    await Promise.all([loadTree(), loadChunks(), loadBlocks()])
  } catch (e) {
    console.error('DocDetail loadAll failed:', e)
  } finally {
    loading.value = false
  }
  // Kick off polling if the doc isn't fully through KG yet — phase
  // panels reveal as their prerequisite data lands.
  schedulePoll()
}

// Soft refresh while the pipeline is still running. Pulls the doc
// row + each panel that's still missing data, so users see chunks /
// tree / KG appear without manual reload. Stops once ``fullyReady``.
let _pollTimer = null
function schedulePoll() {
  if (_pollTimer) {
    clearTimeout(_pollTimer)
    _pollTimer = null
  }
  if (!inFlight.value) return
  _pollTimer = setTimeout(pollOnce, 2500)
}
async function pollOnce() {
  _pollTimer = null
  if (!props.docId) return
  try {
    const fresh = await getDocument(props.docId)
    doc.value = fresh
    // Refetch only the panels whose prerequisite has just landed.
    const fetches = []
    if (phases.value.parsed && allBlocks.value.length === 0) fetches.push(loadBlocks())
    if (phases.value.structured && !tree.value) fetches.push(loadTree())
    // While the doc isn't fully ready, fetch only the chunks past
    // what's already loaded (append-only during ingest). The KG-done
    // watcher below issues a one-shot full reload to pick up role
    // tags that get backfilled when KG extraction finishes.
    if (!fullyReady.value) fetches.push(loadChunks({ incremental: true }))
    if (fetches.length) await Promise.all(fetches)
  } catch (e) {
    console.warn('poll failed:', e)
  }
  schedulePoll()
}

async function loadTree() {
  try {
    tree.value = await getTree(props.docId)
  } catch {
    tree.value = null
  }
}

// `incremental: true` only fetches chunks past what's already loaded
// (uses ``offset = chunks.value.length``). Safe during ingest because
// chunks are append-only — no reordering, no deletion mid-pipeline.
// The default (`incremental: false`) does a full reload, which we
// trigger once on initial open and once on KG completion (chunk role
// tags get backfilled at that boundary).
async function loadChunks({ incremental = false } = {}) {
  try {
    if (incremental) {
      const off = chunks.value.length
      const r = await listChunks(props.docId, { limit: 500, offset: off })
      const items = r.items || []
      const total = r.total || 0
      // Defensive: if server reports fewer rows than we have locally,
      // a reparse must have wiped + restarted. Drop to a full reload.
      if (total < chunks.value.length) {
        return loadChunks({ incremental: false })
      }
      if (items.length) chunks.value = chunks.value.concat(items)
      // If more remain after this batch, schedule another incremental
      // fetch on the next poll tick (rather than blocking here).
      return
    }
    const all = []
    let off = 0
    const BATCH = 500
    for (let i = 0; i < 100; i++) {
      const r = await listChunks(props.docId, { limit: BATCH, offset: off })
      all.push(...(r.items || []))
      if (all.length >= (r.total || 0) || (r.items || []).length < BATCH) break
      off += BATCH
    }
    chunks.value = all
  } catch {
    if (!incremental) chunks.value = []
  }
}

async function loadBlocks() {
  try {
    const all = []
    let off = 0
    const BATCH = 2000
    for (let i = 0; i < 100; i++) {
      const r = await listBlocks(props.docId, { limit: BATCH, offset: off })
      all.push(...(r.items || []))
      if (all.length >= (r.total || 0) || (r.items || []).length < BATCH) break
      off += BATCH
    }
    allBlocks.value = all
  } catch {
    allBlocks.value = []
  }
}

// ── Click handlers ──────────────────────────────────────────────
function onClickChunk(c) {
  activeChunkId.value = c.chunk_id
  activeNodeId.value = c.node_id
  // Translate chunk's block_ids to bbox highlights via the all-blocks
  // map; jump the PDF to the chunk's start page.
  const blockMap = new Map(allBlocks.value.map((b) => [b.block_id, b]))
  const bidSet = new Set(c.block_ids || [])
  const highlights = []
  for (const bid of c.block_ids || []) {
    const b = blockMap.get(bid)
    if (b?.bbox) highlights.push({ page_no: b.page_no, bbox: b.bbox })
  }
  // Second pass: pick up blocks that were merged INTO one of the
  // chunk's surviving blocks. Merged-out blocks carry
  // ``excluded_reason: "merged_into:<surviving_bid>"``. The
  // chunk's ``block_ids`` only lists the survivor, so without this
  // pass a chunk that wraps a page break shows highlights only on
  // the surviving block — leaving the merged continuation visually
  // un-highlighted on the previous/next page.
  for (const b of allBlocks.value) {
    if (!b.excluded || !b.excluded_reason?.startsWith('merged_into:')) continue
    const targetBid = b.excluded_reason.slice('merged_into:'.length)
    if (bidSet.has(targetBid) && b.bbox) {
      highlights.push({ page_no: b.page_no, bbox: b.bbox })
    }
  }
  pdfHighlightBlocks.value = highlights
  if (c.page_start) pdfPage.value = c.page_start
}

function onClickTreeNode(nodeId) {
  activeNodeId.value = nodeId
  // Pick the first chunk that lives under this tree node so the PDF
  // jumps to a useful page. Future iteration: open a node-scoped
  // chunks filter rather than just jumping to one.
  const c = chunks.value.find(
    (c) => c.node_id === nodeId || c.ancestor_node_ids?.includes(nodeId),
  )
  if (c) onClickChunk(c)
}

function toggleNode(nodeId) {
  if (collapsedNodes.has(nodeId)) collapsedNodes.delete(nodeId)
  else collapsedNodes.add(nodeId)
}

// ── Virtualized chunks list ─────────────────────────────────────
// Off-screen rows aren't in the DOM, so the old chunkRefs-based
// scroll-into-view doesn't work for arbitrary targets. The
// virtualizer's ``scrollToIndex`` handles this — it figures out
// where the row will be (using cached or estimated heights) and
// scrolls there, then re-measures once the row is mounted.
const chunksScrollRef = ref(null)
// IMPORTANT: pass options as a `computed` so reactive deps (here:
// ``chunks.value.length`` driving ``count``) trigger the
// virtualizer's internal ``setOptions`` watcher. Passing
// ``count: computed(...)`` directly does NOT work — the library
// only ``unref``s the outer options, leaving inner ComputedRefs
// un-unwrapped, and the virtualizer gets ``count: undefined``.
const chunksVirtualizer = useVirtualizer(
  computed(() => ({
    count: chunks.value.length,
    getScrollElement: () => chunksScrollRef.value,
    // ~70px is a reasonable median for a clamped chunk row (header
    // strip + 2 lines of body + action row + 1px border). Real rows
    // are auto-measured on mount via ``measureElement`` ref below,
    // so this only affects pre-render layout estimates.
    estimateSize: () => 70,
    overscan: 8,
  })),
)
function measureChunkEl(el) {
  if (el) chunksVirtualizer.value.measureElement(el)
}
function scrollToChunk(chunkId) {
  const idx = chunks.value.findIndex((c) => c.chunk_id === chunkId)
  if (idx < 0) return
  // ``align: 'center'`` keeps the target away from sticky edges; the
  // virtualizer falls back to a sane no-op if the scroll element
  // isn't mounted yet (e.g. the chunks pane is still in skeleton).
  chunksVirtualizer.value.scrollToIndex(idx, { align: 'center' })
}
// When a chunk row's expanded state flips, its measured height
// changes — tell the virtualizer to invalidate the cached size on
// the next tick, otherwise the abs-positioned siblings overlap.
function invalidateChunkSize(chunkId) {
  const idx = chunks.value.findIndex((c) => c.chunk_id === chunkId)
  if (idx < 0) return
  nextTick(() => {
    const v = chunksVirtualizer.value
    // ``measureElement`` covers visible rows automatically; the
    // explicit re-measure here keeps the height cache consistent
    // for chunks the user just collapsed but is still scrolling
    // past.
    if (v?.resizeItem) v.resizeItem(idx)
  })
}

async function onPdfClick({ page_no, x, y }) {
  // Hit-test against the block list cached at load time. Bbox is in
  // PDF coordinates (origin bottom-left). Kept inline (no shared util)
  // until a second caller appears.
  if (!allBlocks.value.length || !doc.value) return
  let hit = null
  for (const b of allBlocks.value) {
    if (b.page_no !== page_no || !b.bbox) continue
    const { x0, y0, x1, y1 } = b.bbox
    const minX = Math.min(x0, x1), maxX = Math.max(x0, x1)
    const minY = Math.min(y0, y1), maxY = Math.max(y0, y1)
    if (x >= minX && x <= maxX && y >= minY && y <= maxY) {
      hit = b
      break
    }
  }
  if (!hit) return

  // Some blocks get merged into another during chunking; follow the
  // pointer to the surviving block.
  let bid = hit.block_id
  if (hit.excluded && hit.excluded_reason?.startsWith('merged_into:')) {
    bid = hit.excluded_reason.slice('merged_into:'.length)
  }

  // Try local chunks first (skip the network round-trip when possible).
  let target = chunks.value.find((c) => c.block_ids?.includes(bid))
  if (!target) {
    try {
      const resp = await getChunkByBlock(bid, doc.value.doc_id)
      target = resp?.chunk
    } catch {
      return
    }
  }
  if (!target) return

  // Reuse the chunk-click flow — it sets activeChunkId, jumps the
  // PDF (well, we're already on that page), and rebuilds the bbox
  // highlight set.
  onClickChunk(target)
  // Then scroll the chunks pane so the selected row is in view.
  scrollToChunk(target.chunk_id)
}

async function onReparse() {
  if (!doc.value || inFlight.value) return
  // Optimistic UI: flip the flag synchronously so the button
  // disables before the network round-trip finishes. Polling will
  // take over once the backend has actually moved to a non-ready
  // status. If the API call fails (e.g. 409 because the backend
  // already has this doc in flight), restore the flag so the user
  // can retry.
  _optimisticReparse.value = true
  try {
    await reparseDocument(doc.value.doc_id)
    schedulePoll()
  } catch (e) {
    _optimisticReparse.value = false
    console.error('reparse failed:', e)
  }
}

// Clear optimistic flag once the document is fully ready again so
// the next click cycle starts from a clean slate. Avoids the case
// where a stuck job leaves the button frozen forever.
watch(fullyReady, (v) => {
  if (v) _optimisticReparse.value = false
})

// One-shot full reload when KG extraction transitions to done — chunk
// role tags get backfilled at that boundary, and the incremental
// poll path only appends, never re-fetches existing rows.
watch(
  () => phases.value.kgDone,
  (now, prev) => {
    if (now && !prev && doc.value && chunks.value.length) {
      loadChunks({ incremental: false })
    }
  },
)

// Reparse safety: when the active parse version changes, the old
// chunks (from the previous parse) are no longer the source of
// truth — clear the array and trigger a full reload. Without this,
// the incremental poll path would graft new-version chunks onto a
// stale prefix from the old version.
watch(
  () => doc.value?.active_parse_version,
  (now, prev) => {
    if (now != null && prev != null && now !== prev) {
      chunks.value = []
      allBlocks.value = []
      tree.value = null
      loadChunks({ incremental: false })
      loadBlocks()
      loadTree()
    }
  },
)

watch(() => props.docId, loadAll, { immediate: true })
</script>

<template>
  <div class="doc-detail">
    <!-- ═══════════════════════════════════════════════════════════
         TOP BAR — same shape as Workspace toolbar / KG topbar
         ═══════════════════════════════════════════════════════════ -->
    <header class="doc-detail__top">
      <!-- Breadcrumb. Clicking ``/`` exits to workspace root; folder
           segments exit and navigate the workspace to that folder; the
           filename segment is the doc itself, inert. ``close`` carries
           an optional ``toPath`` so the workspace can land the user
           back at the right folder instead of wherever they last were. -->
      <nav class="doc-detail__crumbs">
        <button class="crumb" @click="emit('close', { toPath: '/' })">/</button>
        <template v-for="(seg, i) in breadcrumb" :key="i">
          <span class="crumb-sep">›</span>
          <button
            class="crumb"
            :class="{ 'crumb--active': i === breadcrumb.length - 1 }"
            :disabled="i === breadcrumb.length - 1"
            @click="onCrumbClick(i)"
          >{{ seg }}</button>
        </template>
      </nav>

      <!-- Metadata chips. Consolidated into the top bar (instead of a
           dedicated "file info" panel) — info density is low and a
           horizontal chip strip reads as page-header chrome. -->
      <div v-if="doc" class="doc-detail__chips">
        <span v-if="fileType" class="chip">{{ fileType }}</span>
        <span v-if="doc.file_size_bytes" class="chip-sep">·</span>
        <span v-if="doc.file_size_bytes" class="chip-text">{{ fmtSize(doc.file_size_bytes) }}</span>
        <span v-if="doc.num_pages" class="chip-sep">·</span>
        <span v-if="doc.num_pages" class="chip-text">{{ doc.num_pages }}p</span>
        <span class="chip-sep">·</span>
        <span
          class="chip-text chip-stage"
          :class="[
            `chip-status--${stageLabel.replace(/\s+/g, '-')}`,
            { 'chip-stage--inflight': inFlight },
          ]"
        >
          <span v-if="inFlight" class="chip-stage__dot" aria-hidden="true"></span>
          {{ stageLabel }}
        </span>
      </div>

      <!-- Right action cluster. Reparse is the only context-specific
           action that always belongs here (read the doc, see something
           wrong, reparse). Other actions stay in the workspace's
           context menu so the detail page reads as a viewer. -->
      <div class="doc-detail__actions">
        <button
          class="toolbar-btn"
          :disabled="inFlight"
          @click="onReparse"
          :title="inFlight ? 'Processing — please wait' : 'Reparse this document'"
        >
          <RefreshCw
            class="w-3.5 h-3.5"
            :class="{ 'animate-spin': inFlight }"
            :stroke-width="1.5"
          />
          <span>{{ inFlight ? 'Processing' : 'Reparse' }}</span>
        </button>
        <button
          class="toolbar-btn ml-2"
          @click="emit('close', { toPath: parentFolderPath })"
          :title="`Back to ${parentFolderPath}`"
        >
          <ArrowLeft class="w-3.5 h-3.5" :stroke-width="1.5" />
        </button>
      </div>
    </header>

    <!-- ═══════════════════════════════════════════════════════════
         BODY — three columns
         ═══════════════════════════════════════════════════════════ -->
    <div class="doc-detail__body">
      <!-- LEFT: Tree -->
      <aside class="pane pane--tree">
        <div class="pane-hdr">
          <span class="pane-title">Structure</span>
          <span v-if="tree" class="pane-meta">
            {{ tree.generation_method }}<template v-if="tree.quality_score != null"> · {{ tree.quality_score.toFixed(2) }}</template>
          </span>
        </div>
        <div
          ref="treeScrollEl"
          class="pane-body pane-body--auto-scrollbar"
          @scroll.passive="onTreeScroll"
        >
          <!-- Skeleton state while structure phase is in flight.
               Polling will swap this out for the real tree as soon
               as ``structure_completed_at`` lands. -->
          <div
            v-if="!tree && inFlight && !phases.structured"
            class="pane-skeleton"
          >
            <span class="pane-skeleton__dot" />
            Building tree…
          </div>
          <div v-else-if="loading && !tree" class="pane-empty">Loading…</div>
          <div v-else-if="!tree" class="pane-empty">No tree</div>
          <TreeNode
            v-else
            :node="tree.nodes[tree.root_id]"
            :nodes="tree.nodes"
            :depth="0"
            :highlight="highlightNodeIds"
            :filterNodeId="activeNodeId"
            :collapsed="collapsedNodes"
            @toggle="toggleNode"
            @select="onClickTreeNode"
          />
        </div>
      </aside>

      <!-- CENTER: PDF or Image viewer (mutually exclusive). Image
           docs render through <ImageViewer> with the raw blob URL —
           no PDF wrapping, no bbox highlights (the IMAGE block has
           a sentinel zero bbox; nothing meaningful to highlight). -->
      <main class="pane pane--pdf">
        <PdfViewer
          v-if="doc && isPdf && pdfUrl && phases.parsed"
          :url="pdfUrl"
          :page="pdfPage"
          :highlightBlocks="pdfHighlightBlocks"
          :maxScale="1.0"
          :downloadUrl="pdfDownloadUrl"
          :sourceDownloadUrl="sourceDownloadUrl"
          :sourceLabel="sourceLabel"
          @pdf-click="onPdfClick"
        />
        <ImageViewer
          v-else-if="doc && isImage && imageUrl"
          :url="imageUrl"
          :downloadUrl="imageDownloadUrl"
          :filename="doc.filename || doc.file_name || ''"
        />
        <div v-else-if="inFlight && !phases.parsed" class="pane-empty pane-empty--center">
          <span class="pane-skeleton">
            <span class="pane-skeleton__dot" />
            Parsing document…
          </span>
        </div>
        <div v-else class="pane-empty pane-empty--center">
          <span v-if="loading">Loading…</span>
          <span v-else>No preview</span>
        </div>
      </main>

      <!-- RIGHT: KG mini (top) + Chunks (bottom) -->
      <aside class="pane pane--right">
        <!-- KG mini — entities sourced from this doc + relations
             among them. Activates a node-fade pass when a chunk is
             selected so the user sees which entities came from that
             chunk (uses the ``source_chunk_ids`` provenance). -->
        <section class="pane pane--kg">
          <div class="pane-hdr">
            <span class="pane-title">Knowledge graph</span>
            <span class="pane-meta">
              <template v-if="kgCounts?.total != null && kgCounts.total > kgCounts.entities">
                {{ kgCounts.entities }}/{{ kgCounts.total }} entities ·
              </template>
              <template v-else>
                {{ kgCounts?.entities ?? doc?.kg_entity_count ?? 0 }} entities ·
              </template>
              {{ kgCounts?.relations ?? doc?.kg_relation_count ?? 0 }} relations
            </span>
          </div>
          <div class="pane-body pane-body--canvas">
            <!-- Render the mini KG only when extraction has finished;
                 otherwise the component fetches an empty graph and
                 renders a "no entities" overlay that contradicts the
                 fact we're still building. -->
            <DocKgMini
              v-if="doc && phases.kgDone"
              :doc-id="doc.doc_id"
              :active-chunk-id="activeChunkId || ''"
              @counts-change="kgCounts = $event"
            />
            <div v-else class="pane-empty pane-empty--center">
              <span class="pane-skeleton">
                <span class="pane-skeleton__dot" />
                {{ phases.embedded ? 'Building knowledge graph…' : 'Waiting for extraction…' }}
              </span>
            </div>
          </div>
        </section>

        <!-- Chunks list — virtualized via @tanstack/vue-virtual.
             Only rows in the visible window (+ overscan) are mounted,
             which keeps DOM size O(viewport) regardless of how many
             chunks the doc has. Heights are auto-measured via
             ``measureChunkEl`` so variable-length chunks lay out
             correctly. ``scrollToChunk(chunkId)`` (used by the PDF
             click handler) calls ``virtualizer.scrollToIndex`` and
             re-measures on land. -->
        <section class="pane pane--chunks">
          <div class="pane-hdr">
            <span class="pane-title">Chunks</span>
            <span class="pane-meta">{{ chunks.length }} total</span>
          </div>
          <div ref="chunksScrollRef" class="pane-body pane-body--scroll">
            <div
              v-if="!chunks.length && inFlight && !phases.chunked"
              class="pane-empty"
            >
              <span class="pane-skeleton">
                <span class="pane-skeleton__dot" />
                Chunking…
              </span>
            </div>
            <div v-else-if="loading && !chunks.length" class="pane-empty">Loading…</div>
            <div v-else-if="!chunks.length" class="pane-empty">No chunks</div>
            <div
              v-else
              class="chunks-virt-spacer"
              :style="{ height: `${chunksVirtualizer.getTotalSize()}px` }"
            >
              <div
                v-for="vRow in chunksVirtualizer.getVirtualItems()"
                :key="vRow.key"
                :data-index="vRow.index"
                :ref="measureChunkEl"
                class="chunks-virt-row chunk-row group"
                :class="{
                  'chunk-row--active': activeChunkId === chunks[vRow.index].chunk_id,
                  'chunk-row--expanded': expandedChunks[chunks[vRow.index].chunk_id],
                }"
                :style="{ transform: `translateY(${vRow.start}px)` }"
                @click="onClickChunk(chunks[vRow.index])"
              >
                <div class="chunk-row__hdr">
                  <span class="chunk-row__page">p.{{ chunks[vRow.index].page_start }}<template v-if="chunks[vRow.index].page_end && chunks[vRow.index].page_end !== chunks[vRow.index].page_start">–{{ chunks[vRow.index].page_end }}</template></span>
                  <span v-if="chunks[vRow.index].content_type && chunks[vRow.index].content_type !== 'text'" class="chunk-row__type">{{ chunks[vRow.index].content_type }}</span>
                  <span class="chunk-row__tok">{{ chunks[vRow.index].token_count }}t</span>
                </div>
                <div
                  class="chunk-row__body"
                  :class="{ 'chunk-row__body--clamp': !expandedChunks[chunks[vRow.index].chunk_id] }"
                >{{ chunks[vRow.index].content }}</div>

                <!-- Inline image preview when expanded. Uses the
                     ``blockImageUrl`` endpoint to fetch the image crop. -->
                <div
                  v-if="expandedChunks[chunks[vRow.index].chunk_id] && chunks[vRow.index].content_type === 'image'"
                  class="chunk-row__figs"
                >
                  <img
                    v-for="url in chunkImageUrls(chunks[vRow.index])"
                    :key="url"
                    :src="url"
                    class="chunk-row__fig"
                    loading="lazy"
                    @error="$event.target.style.display = 'none'"
                  />
                </div>

                <!-- Hover-revealed expand / collapse affordance.
                     Stays out of the way at rest; click reveals the
                     full chunk content + any attached image crops. -->
                <div class="chunk-row__action">
                  <button
                    v-show="!expandedChunks[chunks[vRow.index].chunk_id]"
                    class="chunk-row__view-btn"
                    @click.stop="toggleChunkExpand(chunks[vRow.index].chunk_id)"
                  >view detail</button>
                  <button
                    v-show="expandedChunks[chunks[vRow.index].chunk_id]"
                    class="chunk-row__close-btn"
                    @click.stop="toggleChunkExpand(chunks[vRow.index].chunk_id)"
                  >collapse</button>
                </div>
              </div>
            </div>
          </div>
        </section>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.doc-detail {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--color-bg2);
}

/* ── Top bar ─ 52px min-height matches Workspace toolbar so the page
   header is identical across views (no jitter when entering /
   exiting detail). px-5 py-3 + bg-bg2 + border-b match too. */
.doc-detail__top {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 12px 20px;
  min-height: 52px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
}
.doc-detail__crumbs {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  font-size: 12px;
  color: var(--color-t2);
  user-select: none;
}
.doc-detail__crumbs::-webkit-scrollbar { display: none; }
.crumb {
  padding: 4px 8px;
  border-radius: var(--r-sm);
  white-space: nowrap;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.crumb:hover { background: var(--color-bg3); color: var(--color-t1); }
.crumb--active { color: var(--color-t1); font-weight: 500; }
.crumb-sep { color: var(--color-t3); padding: 0 2px; }

.doc-detail__chips {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: 8px;
  font-size: 10px;
  color: var(--color-t3);
  flex-shrink: 0;
}
.chip {
  padding: 1px 6px;
  background: var(--color-bg3);
  color: var(--color-t2);
  border-radius: var(--r-sm);
  text-transform: uppercase;
  font-weight: 500;
  letter-spacing: 0.02em;
}
.chip-sep { color: var(--color-t3); }
.chip-text { color: var(--color-t3); }
.chip-status--ready { color: #10b981; }
.chip-status--error { color: #f43f5e; }
/* In-flight stages — amber so they pop against the page chrome and
   match the workspace file grid's pending indicators. ``--color-warn-fg``
   is theme-adaptive (amber-400 dark / amber-700 light). */
.chip-status--pending,
.chip-status--processing,
.chip-status--parsing,
.chip-status--converting,
.chip-status--structuring,
.chip-status--chunking,
.chip-status--embedding,
.chip-status--building-graph { color: var(--color-warn-fg); }

/* In-flight stage indicator: pulsing amber dot before the label so
   the user perceives liveness even when the same word stays for a
   while (e.g. "building graph" can run 2-5 min). */
.chip-stage {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.chip-stage__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  animation: chip-pulse 1.4s ease-in-out infinite;
}
@keyframes chip-pulse {
  0%, 100% { opacity: 0.35; }
  50%      { opacity: 1; }
}

/* Skeleton loader for not-yet-ready panels — same vocabulary as the
   chip dot so users learn one cue across the page. Padded centered
   text reads as "still working" without flashing/jumping. */
.pane-skeleton {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 24px 12px;
  font-size: 11px;
  color: var(--color-t3);
}
.pane-skeleton__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-warn-fg);
  animation: chip-pulse 1.4s ease-in-out infinite;
  flex-shrink: 0;
}

.doc-detail__actions {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: 8px;
  flex-shrink: 0;
}

.toolbar-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  font-size: 11px;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.toolbar-btn:hover:not(:disabled) {
  background: var(--color-bg3);
  color: var(--color-t1);
}
.ml-2 { margin-left: 8px; }

/* ── 3-col body ──────────────────────────────────────────────── */
.doc-detail__body {
  flex: 1;
  display: flex;
  min-height: 0;
}
.pane {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.pane--tree {
  width: 260px;
  flex-shrink: 0;
  border-right: 1px solid var(--color-line);
}
.pane--pdf {
  flex: 1;
  min-width: 0;
}
.pane--right {
  width: 340px;
  flex-shrink: 0;
  border-left: 1px solid var(--color-line);
}
.pane--kg {
  flex: 0 0 40%;
  border-bottom: 1px solid var(--color-line);
}
.pane--chunks {
  flex: 1 1 60%;
}

/* Generic pane scaffolding */
.pane-hdr {
  flex-shrink: 0;
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px 6px;
  border-bottom: 1px solid var(--color-line);
}
.pane-title {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--color-t3);
}
.pane-meta {
  font-size: 9px;
  color: var(--color-t3);
  text-align: right;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pane-body {
  flex: 1;
  min-height: 0;
  padding: 4px;
}
.pane-body--scroll {
  overflow-y: auto;
  padding: 0;
}

/* Auto-hiding scrollbar for the structure tree pane.
   Default state: track + thumb fully transparent so the rail is
   invisible. While the user is actively scrolling the JS handler
   adds ``is-scrolling`` for 800ms — that fades the thumb in. We
   reserve gutter so the layout doesn't jump when the thumb appears. */
.pane-body--auto-scrollbar {
  overflow-y: auto;
  scrollbar-gutter: stable;
  scrollbar-width: thin;
  scrollbar-color: transparent transparent;
  transition: scrollbar-color 0.25s ease;
}
.pane-body--auto-scrollbar.is-scrolling {
  scrollbar-color: var(--color-t3, rgba(255, 255, 255, 0.25)) transparent;
}
.pane-body--auto-scrollbar::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}
.pane-body--auto-scrollbar::-webkit-scrollbar-track {
  background: transparent;
}
.pane-body--auto-scrollbar::-webkit-scrollbar-thumb {
  background: transparent;
  border-radius: 3px;
  transition: background 0.25s ease;
}
.pane-body--auto-scrollbar.is-scrolling::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.22);
}
.pane-body--canvas {
  position: relative;       /* anchor for sigma's absolute canvas */
  padding: 0;
  overflow: hidden;
}
.pane-empty {
  padding: 24px 12px;
  text-align: center;
  font-size: 10px;
  color: var(--color-t3);
}
.pane-empty--center {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

/* Virtualized scrolling: the spacer is the full virtual height (so
   the scrollbar is sized correctly), and rows are absolutely
   positioned inside it via translateY from the virtualizer. */
.chunks-virt-spacer {
  position: relative;
  width: 100%;
}
.chunks-virt-row {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  /* ``transition: background`` from .chunk-row stays; transform is
     applied per-frame by the virtualizer and shouldn't animate. */
  will-change: transform;
}

/* Chunk row */
.chunk-row {
  padding: 8px 10px;
  border-bottom: 1px solid var(--color-line);
  cursor: pointer;
  transition: background 0.12s;
}
.chunk-row:hover { background: var(--color-bg3); }
.chunk-row--active { background: var(--color-bg-selected); }
.chunk-row--active:hover {
  background: color-mix(in srgb, var(--color-bg-selected) 75%, var(--color-bg3));
}
.chunk-row__hdr {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
  font-size: 9px;
  color: var(--color-t3);
}
.chunk-row__page { font-weight: 500; color: var(--color-t2); }
.chunk-row__type {
  padding: 0 4px;
  background: var(--color-bg3);
  border-radius: var(--r-sm);
  text-transform: uppercase;
  letter-spacing: 0.02em;
}
.chunk-row__body {
  font-size: 10px;
  line-height: 1.45;
  color: var(--color-t2);
  white-space: pre-wrap;
  word-break: break-word;
}
.chunk-row__body--clamp {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: normal;
}

/* Inline image previews, only visible when the chunk is expanded
   (and only emitted for ``content_type === 'image'``). */
.chunk-row__figs {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.chunk-row__fig {
  max-width: 100%;
  max-height: 220px;
  object-fit: contain;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
}

/* The view/collapse affordance stays calm at rest — only the
   collapsed-state "view detail" link is hidden until hover, so the
   row reads as a passive list item; once expanded, "collapse" is
   visible all the time so the user can re-fold without
   having to hover precisely. */
.chunk-row__action {
  display: flex;
  justify-content: flex-end;
  margin-top: 4px;
}
.chunk-row__view-btn {
  font-size: 9px;
  color: var(--color-brand, #3291ff);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0;
  opacity: 0;
  transition: opacity 0.12s;
}
.chunk-row.group:hover .chunk-row__view-btn {
  opacity: 1;
}
.chunk-row--active .chunk-row__view-btn { opacity: 1; }
.chunk-row__close-btn {
  font-size: 9px;
  color: var(--color-t3);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0;
  transition: color 0.12s;
}
.chunk-row__close-btn:hover { color: var(--color-t1); }
</style>
