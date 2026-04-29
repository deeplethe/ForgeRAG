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
import { computed, reactive, ref, watch } from 'vue'
import { ArrowLeftIcon, ArrowPathIcon } from '@heroicons/vue/24/outline'
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
const expandedNodes = reactive(new Set())
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
}
function chunkImageUrls(c) {
  // Figure-type chunks have one or more block_ids that point to
  // figure crops; the block-image endpoint serves them by id.
  if (c.content_type !== 'figure' || !c.block_ids?.length) return []
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

const isPdf = computed(() => {
  const d = doc.value
  if (!d) return false
  return d.format === 'pdf' || !!d.pdf_file_id
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
}

async function loadTree() {
  try {
    tree.value = await getTree(props.docId)
  } catch {
    tree.value = null
  }
}

async function loadChunks() {
  try {
    // Pull the full set so chunk-by-page filtering / cross-panel
    // navigation has every entry available client-side.
    const all = []
    let off = 0
    const BATCH = 500
    // Hard cap loop iterations as a defensive measure against a
    // malformed total field (shouldn't happen, but keeps us safe).
    for (let i = 0; i < 100; i++) {
      const r = await listChunks(props.docId, { limit: BATCH, offset: off })
      all.push(...(r.items || []))
      if (all.length >= (r.total || 0) || (r.items || []).length < BATCH) break
      off += BATCH
    }
    chunks.value = all
  } catch {
    chunks.value = []
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
  if (expandedNodes.has(nodeId)) expandedNodes.delete(nodeId)
  else expandedNodes.add(nodeId)
}

// Track chunk-row DOM refs so we can scroll the chunks pane to the
// chunk a PDF click resolved to.
const chunkRefs = new Map()
function setChunkRef(chunkId, el) {
  if (el) chunkRefs.set(chunkId, el)
  else chunkRefs.delete(chunkId)
}
function scrollToChunk(chunkId) {
  const el = chunkRefs.get(chunkId)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}

async function onPdfClick({ page_no, x, y }) {
  // Hit-test against the block list cached at load time. Bbox is in
  // PDF coordinates (origin bottom-left). Same logic Repository.vue
  // has — kept inline (no shared util) until a third caller appears.
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
  if (!doc.value) return
  try {
    await reparseDocument(doc.value.doc_id)
    await loadAll()
  } catch (e) {
    console.error('reparse failed:', e)
  }
}

watch(() => props.docId, loadAll, { immediate: true })
</script>

<template>
  <div class="doc-detail">
    <!-- ═══════════════════════════════════════════════════════════
         TOP BAR — same shape as Workspace toolbar / KG topbar
         ═══════════════════════════════════════════════════════════ -->
    <header class="doc-detail__top">
      <!-- Breadcrumb. Clicking ``/`` exits to workspace root; the
           filename segment is just a label (last crumb). -->
      <nav class="doc-detail__crumbs">
        <button class="crumb" @click="emit('close')">/</button>
        <template v-for="(seg, i) in breadcrumb" :key="i">
          <span class="crumb-sep">›</span>
          <button
            class="crumb"
            :class="{ 'crumb--active': i === breadcrumb.length - 1 }"
            @click="emit('close')"
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
        <span class="chip-text" :class="`chip-status--${doc.status}`">{{ doc.status }}</span>
      </div>

      <!-- Right action cluster. Reparse is the only context-specific
           action that always belongs here (read the doc, see something
           wrong, reparse). Other actions stay in the workspace's
           context menu so the detail page reads as a viewer. -->
      <div class="doc-detail__actions">
        <button
          class="toolbar-btn"
          @click="onReparse"
          title="Reparse this document"
        >
          <ArrowPathIcon class="w-3.5 h-3.5" />
          <span>Reparse</span>
        </button>
        <button
          class="toolbar-btn ml-2"
          @click="emit('close')"
          title="Back to workspace"
        >
          <ArrowLeftIcon class="w-3.5 h-3.5" />
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
        <div class="pane-body">
          <div v-if="loading && !tree" class="pane-empty">Loading…</div>
          <div v-else-if="!tree" class="pane-empty">No tree</div>
          <TreeNode
            v-else
            :node="tree.nodes[tree.root_id]"
            :nodes="tree.nodes"
            :depth="0"
            :highlight="highlightNodeIds"
            :filterNodeId="activeNodeId"
            :expanded="expandedNodes"
            @toggle="toggleNode"
            @select="onClickTreeNode"
          />
        </div>
      </aside>

      <!-- CENTER: PDF -->
      <main class="pane pane--pdf">
        <PdfViewer
          v-if="doc && isPdf && pdfUrl"
          :url="pdfUrl"
          :page="pdfPage"
          :highlightBlocks="pdfHighlightBlocks"
          :maxScale="1.0"
          :downloadUrl="pdfDownloadUrl"
          :sourceDownloadUrl="sourceDownloadUrl"
          :sourceLabel="sourceLabel"
          @pdf-click="onPdfClick"
        />
        <div v-else class="pane-empty pane-empty--center">
          <span v-if="loading">Loading…</span>
          <span v-else>No PDF preview</span>
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
              {{ kgCounts?.entities ?? doc?.kg_entity_count ?? 0 }} entities ·
              {{ kgCounts?.relations ?? doc?.kg_relation_count ?? 0 }} relations
            </span>
          </div>
          <div class="pane-body pane-body--canvas">
            <DocKgMini
              v-if="doc"
              :doc-id="doc.doc_id"
              :active-chunk-id="activeChunkId || ''"
              @counts-change="kgCounts = $event"
            />
          </div>
        </section>

        <!-- Chunks list -->
        <section class="pane pane--chunks">
          <div class="pane-hdr">
            <span class="pane-title">Chunks</span>
            <span class="pane-meta">{{ chunks.length }} total</span>
          </div>
          <div class="pane-body pane-body--scroll">
            <div v-if="loading && !chunks.length" class="pane-empty">Loading…</div>
            <div v-else-if="!chunks.length" class="pane-empty">No chunks</div>
            <div
              v-for="c in chunks"
              :key="c.chunk_id"
              :ref="(el) => setChunkRef(c.chunk_id, el)"
              class="chunk-row group"
              :class="{
                'chunk-row--active': activeChunkId === c.chunk_id,
                'chunk-row--expanded': expandedChunks[c.chunk_id],
              }"
              @click="onClickChunk(c)"
            >
              <div class="chunk-row__hdr">
                <span class="chunk-row__page">p.{{ c.page_start }}<template v-if="c.page_end && c.page_end !== c.page_start">–{{ c.page_end }}</template></span>
                <span v-if="c.content_type && c.content_type !== 'text'" class="chunk-row__type">{{ c.content_type }}</span>
                <span class="chunk-row__tok">{{ c.token_count }}t</span>
              </div>
              <div
                class="chunk-row__body"
                :class="{ 'chunk-row__body--clamp': !expandedChunks[c.chunk_id] }"
              >{{ c.content }}</div>

              <!-- Inline figure preview when expanded. Same
                   ``blockImageUrl`` endpoint Repository.vue uses
                   for the standalone view. -->
              <div
                v-if="expandedChunks[c.chunk_id] && c.content_type === 'figure'"
                class="chunk-row__figs"
              >
                <img
                  v-for="url in chunkImageUrls(c)"
                  :key="url"
                  :src="url"
                  class="chunk-row__fig"
                  loading="lazy"
                  @error="$event.target.style.display = 'none'"
                />
              </div>

              <!-- Hover-revealed expand / collapse affordance.
                   Stays out of the way at rest; click reveals the
                   full chunk content + any attached figure crops. -->
              <div class="chunk-row__action">
                <button
                  v-show="!expandedChunks[c.chunk_id]"
                  class="chunk-row__view-btn"
                  @click.stop="toggleChunkExpand(c.chunk_id)"
                >view detail</button>
                <button
                  v-show="expandedChunks[c.chunk_id]"
                  class="chunk-row__close-btn"
                  @click.stop="toggleChunkExpand(c.chunk_id)"
                >collapse</button>
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
.chip-status--pending,
.chip-status--processing,
.chip-status--parsing,
.chip-status--converting,
.chip-status--structuring { color: #f59e0b; }

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

/* Inline figure previews, only visible when the chunk is expanded
   (and only emitted for ``content_type === 'figure'``). */
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
