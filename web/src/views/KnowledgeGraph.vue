<script setup>
// Explicit name so <KeepAlive :exclude> in App.vue can match this component.
// `<script setup>` infers from filename in dev, but bundlers can mangle that
// in prod — defineOptions makes it deterministic.
defineOptions({ name: 'KnowledgeGraph' })

import { ref, computed, watch, onActivated, onDeactivated, onMounted, onUnmounted, nextTick, shallowRef } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { getGraphStats, getFullGraph, searchEntities, getEntityDetail, getDocument, getChunk, blockImageUrl } from '@/api'
import { Search, X, RefreshCw, Maximize2 } from 'lucide-vue-next'
import Spinner from '@/components/Spinner.vue'
import { useTheme } from '@/composables/useTheme'

// Theme-aware sigma colors. Sigma uses canvas/WebGL so CSS vars don't apply
// directly; we read tokens once + refresh on theme change.
const { isDark } = useTheme()
function graphColors() {
  return isDark.value
    ? {
        defaultNode: '#71717a',
        defaultEdge: '#3f3f46',
        label:       '#a1a1a1',
        dimNode:     '#1f1f1f',
        focusEdge:   '#ededed',
        dimEdge:     '#1f1f1f',
      }
    : {
        defaultNode: '#9ca3af',
        defaultEdge: '#d1d5db',
        label:       '#374151',
        dimNode:     '#d0d0d0',
        focusEdge:   '#3d3d3d',
        dimEdge:     '#e2e2e2',
      }
}

const router = useRouter()
const route = useRoute()
import Graph from 'graphology'
import Sigma from 'sigma'
import FA2Layout from 'graphology-layout-forceatlas2/worker'
import { circular, circlepack, random } from 'graphology-layout'
import noverlap from 'graphology-layout-noverlap'
import forceLayout from 'graphology-layout-force'

/* ════════════════════════════════════════════════════════════════════════
   State
   ════════════════════════════════════════════════════════════════════ */

const stats = ref({ entities: 0, relations: 0, backend: '' })
const loading = ref(false)
const error = ref('')
const nodeCount = ref(0)
const edgeCount = ref(0)

// Detail panel
const selectedNode = ref(null)
const selectedDetail = ref(null)
const detailLoading = ref(false)

// Source documents & chunks for detail panel
const sourceDocs = ref({})          // { doc_id: { ...docMeta } }
const sourceChunks = ref([])        // all chunk objects for the entity
const sourceChunksLoading = ref(false)

// Chunk panel (right of detail panel)
const chunkPanelDocId = ref(null)   // which doc's chunks to show; null = closed
const chunkRenderLimit = ref(50)    // scroll-load in increments of 50
const expandedChunks = ref({})      // { chunk_id: true/false }
const chunkPanelRef = ref(null)     // scroll container ref

// Search
const searchQuery = ref('')
// `showSearch` was a toggle for the popup-style panel; the inline
// search-wrap is always visible now, so the only state we need is
// the query + the results.
const searchResults = ref([])
const searching = ref(false)
const searchInput = ref(null)

// DOM refs
const containerRef = ref(null)

// Sigma / graphology (non-reactive via shallowRef — never deep-tracked)
const graph = shallowRef(null)
let sigma = null
let fa2 = null

// State for highlight
let hoveredNode = null
let selectedId = null
let neighborSet = new Set()

/* ════════════════════════════════════════════════════════════════════════
   Entity type colors
   ════════════════════════════════════════════════════════════════════ */

const TYPE_COLORS = {
  PERSON:       '#555555',
  ORGANIZATION: '#0891b2',
  LOCATION:     '#059669',
  CONCEPT:      '#d97706',
  EVENT:        '#dc2626',
  TECHNOLOGY:   '#7c3aed',
  PRODUCT:      '#db2777',
  DOCUMENT:     '#0d9488',
  DATE:         '#ea580c',
  UNKNOWN:      '#6b7280',
}

function typeFill(type) {
  return TYPE_COLORS[(type || '').toUpperCase()] || '#6b7280'
}

const entityTypes = ref([])

function updateEntityTypes() {
  if (!graph.value) { entityTypes.value = []; return }
  const types = new Set()
  graph.value.forEachNode((_, attr) => types.add((attr.entityType || 'UNKNOWN').toUpperCase()))
  entityTypes.value = [...types].sort()
}

/* ════════════════════════════════════════════════════════════════════════
   Helpers
   ════════════════════════════════════════════════════════════════════ */

function getNodeName(entityId) {
  if (graph.value?.hasNode(entityId)) return graph.value.getNodeAttribute(entityId, 'label') || entityId
  return entityId
}

/* ════════════════════════════════════════════════════════════════════════
   Data loading
   ════════════════════════════════════════════════════════════════════ */

async function loadStats() {
  try { stats.value = await getGraphStats() } catch {}
}

async function loadGraph() {
  loading.value = true
  error.value = ''
  try {
    const data = await getFullGraph(500)
    const rawNodes = data.nodes || []
    const rawEdges = data.edges || []
    nodeCount.value = rawNodes.length
    edgeCount.value = rawEdges.length
    buildGraph(rawNodes, rawEdges)
  } catch (e) {
    if (e.message?.includes('404')) {
      error.value = 'Knowledge graph not configured. Enable retrieval.kg_extraction.enabled in forgerag.yaml and restart.'
    } else {
      error.value = e.message || 'Failed to load graph'
    }
  } finally {
    loading.value = false
  }
}

async function loadEntityDetail(entityId) {
  detailLoading.value = true
  sourceDocs.value = {}
  sourceChunks.value = []
  expandedChunks.value = {}
  closeChunkPanel()
  try {
    const detail = await getEntityDetail(entityId)
    selectedDetail.value = detail
    // Load source doc metadata + source chunks in background
    loadSourceData(detail.entity)
  } catch {
    selectedDetail.value = null
  } finally {
    detailLoading.value = false
  }
}

/** Load document metadata and chunk content for the entity's sources */
async function loadSourceData(entity) {
  if (!entity) return
  const docIds = entity.source_doc_ids || []
  const chunkIds = entity.source_chunk_ids || []

  // Fetch doc metadata (parallel, swallow individual errors)
  const docPromises = docIds.map(async (id) => {
    try {
      const doc = await getDocument(id)
      sourceDocs.value = { ...sourceDocs.value, [id]: doc }
    } catch {}
  })

  // Fetch chunk data (parallel, in background)
  sourceChunksLoading.value = true
  const chunkPromises = chunkIds.map(async (id) => {
    try { return await getChunk(id) } catch { return null }
  })

  await Promise.all([
    Promise.all(docPromises),
    Promise.all(chunkPromises).then(results => {
      sourceChunks.value = results.filter(Boolean)
    }),
  ])
  sourceChunksLoading.value = false
}

/** Count of source chunks per doc */
function docChunkCount(docId) {
  return sourceChunks.value.filter(c => c.doc_id === docId).length
}

/** Chunks for the open chunk panel doc, sorted by page */
const panelDocChunks = computed(() => {
  if (!chunkPanelDocId.value) return []
  return sourceChunks.value
    .filter(c => c.doc_id === chunkPanelDocId.value)
    .sort((a, b) => (a.page_start || 0) - (b.page_start || 0) || a.chunk_id.localeCompare(b.chunk_id))
})

/** Only render up to chunkRenderLimit for performance */
const renderedPanelChunks = computed(() => panelDocChunks.value.slice(0, chunkRenderLimit.value))

/** Open chunk panel for a doc */
function openChunkPanel(docId) {
  if (chunkPanelDocId.value === docId) { closeChunkPanel(); return }
  chunkPanelDocId.value = docId
  chunkRenderLimit.value = 50
  expandedChunks.value = {}
}

function closeChunkPanel() {
  chunkPanelDocId.value = null
  chunkRenderLimit.value = 50
  expandedChunks.value = {}
}

/** Scroll handler — load more chunks when near bottom */
function onChunkPanelScroll(e) {
  const el = e.target
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 80) {
    if (chunkRenderLimit.value < panelDocChunks.value.length) {
      chunkRenderLimit.value += 50
    }
  }
}

/** Get image URLs for an image chunk */
function chunkImageUrls(c) {
  if (c.content_type !== 'image' || !c.block_ids?.length) return []
  return c.block_ids.map(bid => blockImageUrl(bid))
}

function toggleChunk(chunkId) {
  expandedChunks.value = { ...expandedChunks.value, [chunkId]: !expandedChunks.value[chunkId] }
}

function shortId(id) { return id ? id.slice(0, 12) : '' }

/* ════════════════════════════════════════════════════════════════════════
   Search
   ════════════════════════════════════════════════════════════════════ */

let searchTimer = null
watch(searchQuery, (q) => {
  clearTimeout(searchTimer)
  if (!q || q.length < 2) { searchResults.value = []; return }
  searchTimer = setTimeout(async () => {
    searching.value = true
    try {
      const r = await searchEntities(q, 20)
      searchResults.value = r.items || []
    } catch { searchResults.value = [] }
    finally { searching.value = false }
  }, 300)
})

function focusNode(entityId) {
  searchQuery.value = ''
  if (!sigma) return
  if (graph.value?.hasNode(entityId)) {
    selectNode(entityId)
    const dd = sigma.getNodeDisplayData(entityId)
    if (!dd) return
    sigma.getCamera().animate(
      { x: dd.x, y: dd.y, ratio: 0.15 },
      { duration: 400 },
    )
  } else {
    // Entity not in the visible graph — just load its detail panel
    selectedNode.value = { id: entityId, label: entityId }
    loadEntityDetail(entityId)
  }
}

/* ════════════════════════════════════════════════════════════════════════
   Graph construction + Sigma init
   ════════════════════════════════════════════════════════════════════ */

function buildGraph(rawNodes, rawEdges) {
  // Destroy previous
  destroySigma()

  const g = new Graph()

  for (const n of rawNodes) {
    const degree = n.degree || 0
    g.addNode(n.id, {
      label: n.name,
      entityType: n.type || 'UNKNOWN',
      description: n.description || '',
      degree,
      sourceDocIds: n.source_doc_ids || [],
      // Visual attrs
      x: (Math.random() - 0.5) * 200,
      y: (Math.random() - 0.5) * 200,
      size: Math.max(3, Math.min(15, 3 + degree * 0.5)),
      color: typeFill(n.type),
    })
  }

  // Deduplicate edges (sigma doesn't allow duplicate keys)
  const edgeSet = new Set()
  for (const e of rawEdges) {
    if (!g.hasNode(e.source) || !g.hasNode(e.target)) continue
    const key = `${e.source}->${e.target}`
    if (edgeSet.has(key)) continue
    edgeSet.add(key)
    g.addEdge(e.source, e.target, {
      keywords: e.keywords || '',
      description: e.description || '',
      weight: e.weight || 1,
      type: 'arrow',
      size: 1,
      color: graphColors().defaultEdge,
    })
  }

  graph.value = g
  updateEntityTypes()
  initSigma(g)
}

function initSigma(g) {
  if (!containerRef.value) return

  const c = graphColors()
  sigma = new Sigma(g, containerRef.value, {
    // Rendering
    defaultNodeColor: c.defaultNode,
    defaultEdgeColor: c.defaultEdge,
    defaultEdgeType: 'arrow',
    renderEdgeLabels: false,
    labelFont: 'Geist, Inter, system-ui, sans-serif',
    labelSize: 11,
    labelWeight: '500',
    labelColor: { color: c.label },
    labelDensity: 0.8,
    labelGridCellSize: 100,
    labelRenderedSizeThreshold: 5,
    // Edges
    edgeLabelFont: 'Geist, Inter, system-ui, sans-serif',
    edgeLabelSize: 9,
    // Performance
    hideEdgesOnMove: false,
    hideLabelsOnMove: false,
    // zIndex to layer selected on top
    zIndex: true,
  })

  // ── Node reducers for highlight / dim ──
  sigma.setSetting('nodeReducer', (node, data) => {
    const res = { ...data }
    if (selectedId) {
      if (node === selectedId) {
        res.highlighted = true
        res.zIndex = 2
      } else if (neighborSet.has(node)) {
        res.zIndex = 1
      } else {
        res.color = graphColors().dimNode
        res.label = null
        res.zIndex = 0
      }
    }
    if (hoveredNode) {
      if (node === hoveredNode) {
        res.highlighted = true
      }
    }
    return res
  })

  sigma.setSetting('edgeReducer', (edge, data) => {
    const res = { ...data }
    if (selectedId) {
      const src = g.source(edge)
      const tgt = g.target(edge)
      if (src === selectedId || tgt === selectedId) {
        res.color = graphColors().focusEdge
        res.size = 2
        res.zIndex = 1
      } else {
        res.color = graphColors().dimEdge
        res.hidden = true
      }
    }
    return res
  })

  // ── Events ──
  sigma.on('enterNode', ({ node }) => {
    hoveredNode = node
    sigma.refresh()
  })
  sigma.on('leaveNode', () => {
    hoveredNode = null
    sigma.refresh()
  })
  sigma.on('clickNode', ({ node }) => {
    selectNode(node)
  })
  sigma.on('clickStage', () => {
    clearSelection()
  })

  // ── Node dragging ──
  let draggedNode = null
  let isDragging = false

  sigma.on('downNode', (e) => {
    isDragging = false
    draggedNode = e.node
    // Disable camera panning while dragging a node
    sigma.getCamera().disable()
  })

  sigma.getMouseCaptor().on('mousemovebody', (e) => {
    if (!draggedNode) return
    isDragging = true
    // Convert viewport coords to graph coords
    const pos = sigma.viewportToGraph(e)
    g.setNodeAttribute(draggedNode, 'x', pos.x)
    g.setNodeAttribute(draggedNode, 'y', pos.y)
    // Prevent sigma from treating this as a click
    e.preventSigmaDefault()
    e.original.preventDefault()
    e.original.stopPropagation()
  })

  sigma.getMouseCaptor().on('mouseup', () => {
    if (draggedNode && isDragging) {
      // Was a real drag, not a click — don't trigger selection
    }
    draggedNode = null
    isDragging = false
    sigma.getCamera().enable()
  })

  // ── Start ForceAtlas2 layout in WebWorker ──
  fa2 = new FA2Layout(g, {
    settings: {
      gravity: 1,
      scalingRatio: 6,
      strongGravityMode: false,
      barnesHutOptimize: g.order > 100,
      barnesHutTheta: 0.5,
      slowDown: 5,
      outboundAttractionDistribution: true,
    },
  })
  fa2.start()

  // Stop FA2 after convergence
  setTimeout(() => { if (fa2?.isRunning()) fa2.stop() }, 5000)
}

function selectNode(nodeId) {
  selectedId = nodeId
  // Build neighbor set
  neighborSet = new Set()
  if (graph.value?.hasNode(nodeId)) {
    graph.value.forEachNeighbor(nodeId, (neighbor) => neighborSet.add(neighbor))
  }
  neighborSet.add(nodeId)

  const attrs = graph.value?.getNodeAttributes(nodeId)
  selectedNode.value = {
    id: nodeId,
    name: attrs?.label || nodeId,
    type: attrs?.entityType || 'UNKNOWN',
    degree: attrs?.degree || 0,
    description: attrs?.description || '',
    color: attrs?.color || '#6b7280',
    source_doc_ids: attrs?.sourceDocIds || [],
  }
  loadEntityDetail(nodeId)
  sigma?.refresh()
  // Sync to URL
  router.replace({ query: { node: nodeId } })
}

function clearSelection() {
  selectedId = null
  neighborSet = new Set()
  selectedNode.value = null
  selectedDetail.value = null
  closeChunkPanel()
  sigma?.refresh()
  router.replace({ query: {} })
}

function destroySigma() {
  if (fa2) { try { fa2.kill() } catch {} fa2 = null }
  if (sigma) { try { sigma.kill() } catch {} sigma = null }
  hoveredNode = null
  selectedId = null
  neighborSet = new Set()
}

/* ════════════════════════════════════════════════════════════════════════
   Controls
   ════════════════════════════════════════════════════════════════════ */

function fitToScreen() {
  if (!sigma) return
  sigma.getCamera().animate({ x: 0.5, y: 0.5, ratio: 1 }, { duration: 300 })
}

function reheat() {
  if (!fa2 || !graph.value) return
  if (!fa2.isRunning()) {
    fa2.start()
    setTimeout(() => { if (fa2?.isRunning()) fa2.stop() }, 4000)
  }
}

/* ── Layout switcher ── */
const activeLayout = ref('Force Atlas')
const showLayoutMenu = ref(false)
const LAYOUTS = ['Force Atlas', 'Force Directed', 'Circular', 'Circle Pack', 'Random', 'Noverlap']

function applyLayout(name) {
  activeLayout.value = name
  showLayoutMenu.value = false
  const g = graph.value
  if (!g || !sigma) return

  // Stop FA2 if running
  if (fa2?.isRunning()) fa2.stop()

  if (name === 'Force Atlas') {
    // Re-randomize positions then start FA2
    g.forEachNode((node) => {
      g.setNodeAttribute(node, 'x', (Math.random() - 0.5) * 200)
      g.setNodeAttribute(node, 'y', (Math.random() - 0.5) * 200)
    })
    fa2.start()
    setTimeout(() => { if (fa2?.isRunning()) fa2.stop() }, 5000)
  } else if (name === 'Force Directed') {
    const positions = forceLayout(g, {
      maxIterations: 500,
      settings: { attraction: 0.0005, repulsion: 0.1, gravity: 0.0001 },
    })
    for (const [node, pos] of Object.entries(positions)) {
      g.setNodeAttribute(node, 'x', pos.x)
      g.setNodeAttribute(node, 'y', pos.y)
    }
  } else if (name === 'Circular') {
    circular.assign(g, { scale: 200 })
  } else if (name === 'Circle Pack') {
    circlepack.assign(g, {
      hierarchyAttributes: ['entityType'],
      scale: 200,
    })
  } else if (name === 'Random') {
    random.assign(g, { scale: 200 })
  } else if (name === 'Noverlap') {
    // Apply noverlap on top of current positions to remove overlaps
    noverlap.assign(g, {
      maxIterations: 200,
      settings: { ratio: 2, margin: 5, speed: 3 },
    })
  }

  sigma.refresh()
}

const zoomLevel = ref(100)
function updateZoom() {
  if (!sigma) return
  zoomLevel.value = Math.round((1 / sigma.getCamera().ratio) * 100)
}

/* ── Keyboard ── */

function onKeyDown(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    e.preventDefault()
    // Inline search is always mounted; just focus + select to mirror
    // the standard Ctrl+K = focus search behavior.
    nextTick(() => searchInput.value?.focus())
    searchInput.value?.select?.()
  }
  if (e.key === 'Escape') {
    if (showLayoutMenu.value) showLayoutMenu.value = false
    else if (searchQuery.value) searchQuery.value = ''
    else if (chunkPanelDocId.value) closeChunkPanel()
    else if (selectedNode.value) clearSelection()
  }
}

function onClickOutside(e) {
  if (showLayoutMenu.value && !e.target.closest('.relative')) {
    showLayoutMenu.value = false
  }
}

/* ════════════════════════════════════════════════════════════════════════
   Lifecycle
   ════════════════════════════════════════════════════════════════════ */

let cameraObserver = null

// Lifecycle split for <KeepAlive>:
//   onMounted          — first time only: load data, init sigma
//   onActivated        — every time visible (incl. first): bind window listeners
//   onDeactivated      — every time hidden: unbind listeners; sigma stays alive
//   onUnmounted        — cache evicted (rare): full teardown
//
// Sigma's WebGL context survives detachment because the canvas DOM
// node is preserved by KeepAlive (it's part of the component's
// rendered tree, just moved out of the active route view). This
// avoids the previous "kill on every leave" cycle that the
// onBeforeRouteLeave cover-flash hack was working around.
onMounted(async () => {
  await loadStats()
  if (stats.value.backend && stats.value.backend !== 'none') {
    await loadGraph()
    await nextTick()
    if (sigma) {
      updateZoom()
      cameraObserver = () => updateZoom()
      sigma.getCamera().on('updated', cameraObserver)
    }
    // Restore selection from URL query (?node=xxx) — let FA2 settle first
    const nodeId = route.query.node
    if (nodeId && graph.value?.hasNode(nodeId)) {
      setTimeout(() => focusNode(nodeId), 1500)
    }
  } else {
    error.value = 'Knowledge graph backend not initialized. Check that networkx is installed and restart the server.'
  }
})

onActivated(() => {
  // Re-bind window listeners only while visible — otherwise pressing
  // Escape on another tab would still close the KG entity panel.
  window.addEventListener('keydown', onKeyDown)
  window.addEventListener('click', onClickOutside)
  // Sigma may have stopped rendering while detached; nudge it.
  if (sigma) sigma.refresh()
})

onDeactivated(() => {
  window.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('click', onClickOutside)
  // Don't destroy sigma — keep the layout + cached graph alive so a
  // tab return is instant. Sigma stops drawing automatically when its
  // canvas is detached from the DOM tree.
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('click', onClickOutside)
  if (sigma && cameraObserver) {
    try { sigma.getCamera().removeListener('updated', cameraObserver) } catch {}
  }
  destroySigma()
})

/* When the user toggles light/dark, re-apply node/edge colors to every
   element + ask sigma to repaint. Reducers already pull from graphColors()
   on each frame, so the refresh is enough for selected/dim states. */
watch(isDark, () => {
  if (!sigma || !graph.value) return
  const c = graphColors()
  // Replace the static edge colors set at construction
  graph.value.forEachEdge((e, attrs) => {
    if (!attrs._customColor) {
      graph.value.setEdgeAttribute(e, 'color', c.defaultEdge)
    }
  })
  sigma.setSetting('defaultNodeColor', c.defaultNode)
  sigma.setSetting('defaultEdgeColor', c.defaultEdge)
  sigma.setSetting('labelColor', { color: c.label })
  sigma.refresh()
})
</script>

<template>
  <div class="h-full flex flex-col bg-bg2 overflow-hidden">
    <!-- ═══════ Top bar ═══════ -->
    <!-- Single-row, matches the Workspace toolbar's height. The
         "Knowledge Graph" title used to sit above the stats line —
         redundant with the side-nav tab, and the two-line block made
         the bar visibly taller than other page headers. -->
    <div class="flex-none flex items-center justify-between px-5 py-3 border-b border-line bg-bg2">
      <!-- ``text-xs`` + ``text-t2`` matches the Workspace breadcrumb so
           page headers feel typographically consistent. Backend leads
           as the context label (which graph store is in use), then
           the counts — putting the system tag at the end made it
           dangle. ``font-mono`` dropped: the proportional-font numbers
           and a monospace ``networkx`` on the same line read as two
           different fonts. -->
      <div class="text-xs text-t2 min-h-[1em]">
        <template v-if="stats.entities">
          <span class="text-t3">{{ stats.backend }}</span>
          &middot;
          {{ stats.entities.toLocaleString() }} entities
          &middot;
          {{ stats.relations.toLocaleString() }} relations
        </template>
      </div>

      <div class="flex items-center gap-1">
        <!-- Search — inline input mirroring the workspace pattern.
             Results dropdown anchors below the input when the query
             length crosses the 2-char threshold; click a result to
             focus that node. ``Ctrl+K`` focuses the input. -->
        <div class="search-wrap">
          <Search class="search-icon" :size="14" :stroke-width="1.5" />
          <input
            ref="searchInput"
            v-model="searchQuery"
            type="text"
            placeholder="Search entities..."
            class="search-input"
            @keydown.escape="searchQuery = ''"
          />
          <button
            v-if="searchQuery"
            class="search-clear"
            @click="searchQuery = ''"
            title="Clear"
          >✕</button>
          <Transition name="fade">
            <div v-if="searchQuery.length >= 2" class="search-results">
              <div v-if="searching" class="p-3 text-center text-[10px] text-t3">Searching...</div>
              <div v-else-if="!searchResults.length"
                class="p-3 text-center text-[10px] text-t3">No entities found</div>
              <button v-for="r in searchResults" :key="r.entity_id"
                @click="focusNode(r.entity_id)"
                class="w-full text-left px-3 py-2 hover:bg-bg-hover transition-colors border-b border-line last:border-0">
                <div class="flex items-center gap-2">
                  <span class="w-2 h-2 rounded-full shrink-0"
                    :style="{ background: typeFill(r.entity_type) }"></span>
                  <span class="text-[11px] text-t1 font-medium truncate">{{ r.name }}</span>
                  <span class="text-[9px] text-t3 uppercase ml-auto shrink-0">{{ r.entity_type }}</span>
                </div>
              </button>
            </div>
          </Transition>
        </div>

        <div class="w-px h-4 bg-line mx-1"></div>

        <!-- Layout selector -->
        <div class="relative">
          <button @click="showLayoutMenu = !showLayoutMenu"
            class="flex items-center gap-1 px-2 py-1 rounded-md text-[10px] text-t3 hover:text-t1 hover:bg-bg-hover transition-colors">
            <span>{{ activeLayout }}</span>
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
              class="transition-transform" :class="showLayoutMenu ? 'rotate-180' : ''">
              <path d="M6 9l6 6 6-6"/>
            </svg>
          </button>
          <Transition name="fade">
            <div v-if="showLayoutMenu"
              class="absolute top-full left-0 mt-1 bg-bg border border-line rounded-lg shadow-lg py-1 z-30 min-w-[120px]">
              <button v-for="l in LAYOUTS" :key="l"
                @click="applyLayout(l)"
                class="w-full text-left px-3 py-1.5 text-[11px] transition-colors"
                :class="activeLayout === l ? 'text-t1 bg-bg3 font-medium' : 'text-t2 hover:bg-bg3'"
              >{{ l }}</button>
            </div>
          </Transition>
        </div>

        <div class="w-px h-4 bg-line mx-0.5"></div>

        <button @click="reheat" title="Re-layout"
          class="p-1.5 rounded-md text-t3 hover:text-t1 hover:bg-bg-hover transition-colors">
          <RefreshCw class="w-4 h-4" :stroke-width="1.5" />
        </button>
        <button @click="fitToScreen" title="Fit to screen"
          class="p-1.5 rounded-md text-t3 hover:text-t1 hover:bg-bg-hover transition-colors">
          <Maximize2 class="w-4 h-4" :stroke-width="1.5" />
        </button>
      </div>
    </div>

    <!-- ═══════ Main content ═══════ -->
    <div class="flex-1 flex min-h-0">
      <!-- ── Graph container ── -->
      <div class="kg-graph-area flex-1 relative overflow-hidden">

        <!-- Loading -->
        <Transition name="fade">
          <div v-if="loading" class="kg-loading-overlay absolute inset-0 flex items-center justify-center z-10">
            <div class="flex flex-col items-center gap-2">
              <Spinner size="lg" />
              <span class="text-[11px] text-t3">Loading graph data...</span>
            </div>
          </div>
        </Transition>

        <!-- Empty state -->
        <div v-if="!loading && !nodeCount" class="absolute inset-0 flex items-center justify-center pl-12 z-[5]">
          <div class="flex flex-col items-center gap-3 text-center select-none max-w-xs">
            <div class="w-14 h-14 rounded-2xl border-2 border-dashed border-line flex items-center justify-center text-t3/40">
              <svg class="w-7 h-7" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.2">
                <circle cx="7" cy="7" r="2.5" />
                <circle cx="17" cy="9" r="2" />
                <circle cx="11" cy="17" r="2" />
                <circle cx="19" cy="17" r="1.5" />
                <line x1="9" y1="8" x2="15.5" y2="9" stroke-linecap="round" opacity="0.4" />
                <line x1="8.5" y1="9" x2="10" y2="15.5" stroke-linecap="round" opacity="0.4" />
                <line x1="12.5" y1="17.5" x2="17.5" y2="17" stroke-linecap="round" opacity="0.4" />
              </svg>
            </div>
            <div>
              <div class="text-sm text-t2 font-medium">No graph data</div>
              <div class="text-[11px] text-t3 mt-1 leading-relaxed">
                {{ error || 'Enable retrieval.kg_extraction.enabled in forgerag.yaml, restart the server, and ingest documents to populate the knowledge graph.' }}
              </div>
            </div>
          </div>
        </div>

        <!-- Sigma renders into this div -->
        <div ref="containerRef" class="absolute inset-0" />
        <!-- (The pre-unload cover is injected via direct DOM in
             onBeforeRouteLeave — bypasses Vue's async reactivity so the
             cover is guaranteed to paint before sigma is killed.) -->

        <!-- Legend -->
        <div v-if="entityTypes.length"
          class="kg-legend absolute bottom-3 left-3 backdrop-blur-sm border border-line rounded-lg px-3 py-2 z-10 pointer-events-none">
          <div class="flex flex-wrap gap-x-3 gap-y-1">
            <div v-for="t in entityTypes" :key="t" class="flex items-center gap-1.5">
              <span class="w-[7px] h-[7px] rounded-full" :style="{ background: typeFill(t) }"></span>
              <span class="text-[9px] text-t3 uppercase tracking-wide font-medium">{{ t }}</span>
            </div>
          </div>
        </div>

        <!-- Zoom -->
        <div class="kg-overlay-pill absolute bottom-3 right-3 backdrop-blur-sm border border-line rounded-md px-2 py-1 z-10 pointer-events-none">
          <span class="text-[9px] text-t3 font-mono tabular-nums">{{ zoomLevel }}%</span>
        </div>

        <!-- Node count -->
        <div v-if="nodeCount"
          class="kg-overlay-pill absolute top-3 left-3 backdrop-blur-sm border border-line rounded-md px-2.5 py-1 z-10 pointer-events-none">
          <span class="text-[9px] text-t3">
            <span class="font-medium text-t2">{{ nodeCount }}</span> nodes &middot;
            <span class="font-medium text-t2">{{ edgeCount }}</span> edges
          </span>
        </div>
      </div>

      <!-- ── Detail panel ── -->
      <Transition name="slide">
        <div v-if="selectedNode"
          class="w-72 shrink-0 flex flex-col border-l border-line bg-bg overflow-hidden">
          <!-- Header -->
          <div class="flex-none px-4 py-3 border-b border-line">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2 min-w-0">
                <span class="w-3 h-3 rounded-full shrink-0 shadow-sm"
                  :style="{ background: selectedNode.color }"></span>
                <span class="text-[12px] font-semibold text-t1 truncate">{{ selectedNode.name }}</span>
              </div>
              <button @click="clearSelection" class="p-1 text-t3 hover:text-t1 transition-colors shrink-0 rounded hover:bg-bg-hover">
                <X class="w-3.5 h-3.5" :stroke-width="1.5" />
              </button>
            </div>
            <div class="flex items-center gap-2 mt-1.5">
              <span class="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded font-medium"
                :style="{
                  background: selectedNode.color + '18',
                  color: selectedNode.color
                }">
                {{ selectedNode.type }}
              </span>
              <span class="text-[9px] text-t3">{{ selectedNode.degree }} connections</span>
            </div>
          </div>

          <!-- Body -->
          <div class="flex-1 overflow-y-auto">
            <div v-if="detailLoading" class="p-6 text-center">
              <Spinner size="md" class="mx-auto" />
            </div>

            <template v-else-if="selectedDetail">
              <!-- Description -->
              <div v-if="selectedDetail.entity?.description" class="px-4 py-3 border-b border-line">
                <div class="text-[9px] text-t3 uppercase tracking-widest mb-1.5 font-medium">Description</div>
                <p class="text-[11px] text-t2 leading-relaxed whitespace-pre-wrap">{{ selectedDetail.entity.description }}</p>
              </div>

              <!-- Source documents (click to open chunk panel) -->
              <div v-if="selectedDetail.entity?.source_doc_ids?.length" class="px-4 py-3 border-b border-line">
                <div class="text-[9px] text-t3 uppercase tracking-widest mb-2 font-medium">
                  Sources
                  <span class="normal-case tracking-normal">({{ selectedDetail.entity.source_doc_ids.length }})</span>
                </div>

                <div class="space-y-1">
                  <button v-for="did in selectedDetail.entity.source_doc_ids" :key="did"
                    @click="openChunkPanel(did)"
                    class="w-full text-left rounded-md px-2.5 py-2 transition-colors group"
                    :class="chunkPanelDocId === did ? 'bg-brand/8 ring-1 ring-brand/20' : 'hover:bg-bg2'">
                    <template v-if="sourceDocs[did]">
                      <div class="text-[10px] text-t1 font-medium truncate">{{ sourceDocs[did].file_name || sourceDocs[did].filename || did }}</div>
                      <div class="flex flex-wrap items-center gap-x-2 gap-y-0 mt-0.5 text-[9px] text-t3">
                        <span class="font-mono">{{ shortId(did) }}</span>
                        <span v-if="sourceDocs[did].format">{{ sourceDocs[did].format }}</span>
                        <span v-if="sourceDocs[did].num_chunks">{{ sourceDocs[did].num_chunks }} chunks</span>
                        <span v-if="docChunkCount(did)" class="text-brand font-medium">{{ docChunkCount(did) }} related</span>
                      </div>
                    </template>
                    <template v-else>
                      <div class="text-[10px] text-t2 font-mono truncate">{{ did }}</div>
                      <div v-if="docChunkCount(did)" class="text-[9px] text-brand font-medium mt-0.5">{{ docChunkCount(did) }} related chunks</div>
                    </template>
                  </button>
                </div>

                <div v-if="sourceChunksLoading" class="mt-2 text-[9px] text-t3 text-center">Loading chunks...</div>
              </div>

              <!-- Relations -->
              <div v-if="selectedDetail.relations?.length" class="px-4 py-3">
                <div class="text-[9px] text-t3 uppercase tracking-widest mb-2 font-medium">
                  Relations
                  <span class="normal-case tracking-normal">({{ selectedDetail.relations.length }})</span>
                </div>
                <div class="space-y-0.5">
                  <button v-for="rel in selectedDetail.relations" :key="rel.relation_id"
                    @click="focusNode(rel.source_entity === selectedNode.id ? rel.target_entity : rel.source_entity)"
                    class="w-full text-left px-2 py-1.5 rounded-md hover:bg-bg2 transition-colors group">
                    <div class="text-[10px] text-t1 font-medium truncate group-hover:text-brand transition-colors">
                      {{ rel.source_entity === selectedNode.id ? (rel.target_entity_name || rel.target_entity) : (rel.source_entity_name || rel.source_entity) }}
                    </div>
                    <div v-if="rel.keywords" class="text-[9px] text-t3 mt-0.5 truncate">
                      {{ rel.keywords }}
                    </div>
                    <div v-if="rel.description" class="text-[9px] text-t3/60 mt-0.5 truncate">
                      {{ rel.description }}
                    </div>
                  </button>
                </div>
              </div>

              <div v-else class="px-4 py-6 text-center text-[10px] text-t3">No relations found.</div>
            </template>
          </div>
        </div>
      </Transition>

      <!-- ── Chunk panel (opens when a source doc is clicked) ── -->
      <Transition name="slide-chunk">
        <div v-if="chunkPanelDocId"
          class="w-96 shrink-0 flex flex-col border-l border-line bg-bg overflow-hidden">
          <!-- Header -->
          <div class="flex-none px-4 py-3 border-b border-line">
            <div class="flex items-center justify-between">
              <div class="min-w-0 flex-1">
                <div class="text-[10px] text-t1 font-medium truncate">
                  {{ sourceDocs[chunkPanelDocId]?.file_name || sourceDocs[chunkPanelDocId]?.filename || chunkPanelDocId }}
                </div>
                <div class="flex items-center gap-2 mt-0.5 text-[9px] text-t3">
                  <span class="font-mono">{{ shortId(chunkPanelDocId) }}</span>
                  <span>{{ panelDocChunks.length }} chunks</span>
                  <span v-if="panelDocChunks.length > chunkRenderLimit" class="text-t3/60">showing {{ chunkRenderLimit }}</span>
                </div>
              </div>
              <button @click="closeChunkPanel" class="p-1 text-t3 hover:text-t1 transition-colors shrink-0 rounded hover:bg-bg-hover ml-2">
                <X class="w-3.5 h-3.5" :stroke-width="1.5" />
              </button>
            </div>
          </div>

          <!-- Chunk list (scroll loads more) -->
          <div ref="chunkPanelRef" class="flex-1 overflow-y-auto" @scroll="onChunkPanelScroll">
            <div v-if="sourceChunksLoading && !sourceChunks.length" class="p-6 text-center">
              <Spinner size="md" class="mx-auto" />
              <div class="text-[10px] text-t3 mt-2">Loading chunks...</div>
            </div>

            <div v-else-if="!panelDocChunks.length" class="p-6 text-center text-[10px] text-t3">
              {{ sourceChunksLoading ? 'Loading...' : 'No related chunks found' }}
            </div>

            <template v-else>
              <div v-for="c in renderedPanelChunks" :key="c.chunk_id"
                class="px-4 py-3 border-b border-line transition-colors group hover:bg-bg2">
                <!-- Chunk header -->
                <div class="flex items-center gap-1.5 flex-wrap">
                  <span class="text-[9px] text-t3 font-mono shrink-0">{{ c.chunk_id }}</span>
                  <span v-if="c.content_type !== 'text'"
                    class="text-[8px] uppercase px-1 py-px rounded bg-brand/10 text-brand font-medium">{{ c.content_type }}</span>
                  <span class="text-[9px] text-t3">{{ c.token_count }} tok</span>
                  <span class="text-[9px] text-t3">p.{{ c.page_start }}{{ c.page_end !== c.page_start ? '-' + c.page_end : '' }}</span>
                </div>

                <!-- Content (collapsed = line-clamp-1, expanded = full) -->
                <div class="text-[10px] text-t2 leading-relaxed mt-1" :class="expandedChunks[c.chunk_id] ? 'whitespace-pre-wrap' : 'line-clamp-1'">{{ c.content }}</div>

                <!-- Expanded extras: images -->
                <div v-if="expandedChunks[c.chunk_id] && c.content_type === 'image' && chunkImageUrls(c).length" class="mt-2 flex flex-wrap gap-2">
                  <img v-for="url in chunkImageUrls(c)" :key="url"
                    :src="url"
                    class="max-w-full max-h-52 rounded border border-line object-contain bg-white"
                    loading="lazy"
                    @error="$event.target.style.display='none'" />
                </div>

                <!-- view detail / collapse -->
                <div class="flex justify-end mt-1">
                  <button v-show="!expandedChunks[c.chunk_id]"
                    class="text-[9px] text-brand opacity-0 group-hover:opacity-100 transition-opacity"
                    @click.stop="toggleChunk(c.chunk_id)">view detail</button>
                  <button v-show="expandedChunks[c.chunk_id]"
                    class="text-[9px] text-t3 hover:text-brand"
                    @click.stop="toggleChunk(c.chunk_id)">collapse</button>
                </div>
              </div>

              <!-- Load more indicator -->
              <div v-if="chunkRenderLimit < panelDocChunks.length" class="py-3 text-center text-[9px] text-t3">
                Scroll for more &middot; {{ panelDocChunks.length - chunkRenderLimit }} remaining
              </div>
            </template>
          </div>
        </div>
      </Transition>
    </div>
  </div>
</template>

<style scoped>
.slide-enter-active, .slide-leave-active {
  transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.2s ease;
}
.slide-enter-from, .slide-leave-to {
  transform: translateX(100%);
  opacity: 0;
}
.slide-chunk-enter-active, .slide-chunk-leave-active {
  transition: width 0.2s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.2s ease;
}
.slide-chunk-enter-from, .slide-chunk-leave-to {
  width: 0 !important;
  opacity: 0;
}
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.15s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}

/* ── KG-specific surfaces — token-based so they adapt to dark mode ── */
.kg-graph-area {
  /* Subtle gradient that respects the canvas. Using bg2 (canvas) at both
     stops with a tiny lift towards bg3 makes the graph feel "set into" the
     page in both light and dark. */
  background: linear-gradient(135deg, var(--color-bg2) 0%, var(--color-bg3) 100%);
  /* Force this entire region (including the WebGL canvas inside) onto its
     own GPU compositor layer. Without this, the canvas's GPU layer composes
     INDEPENDENTLY from the parent's CSS opacity transition — producing a
     one-frame mismatch (visible flash) on route leave. translateZ(0)
     promotes the layer atomically. */
  transform: translateZ(0);
  will-change: transform, opacity;
}
.kg-loading-overlay {
  background: color-mix(in srgb, var(--color-bg2) 88%, transparent);
  backdrop-filter: blur(4px);
}
.kg-legend,
.kg-overlay-pill {
  background: color-mix(in srgb, var(--color-bg) 85%, transparent);
}

/* Pre-unload cover — solid canvas color, masks the WebGL canvas teardown */
.kg-leaving-cover {
  background: var(--color-bg2);
}
.line-clamp-1 {
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* Inline entity-search input — same shape as the workspace toolbar's
   search so the two pages share a visual language. Results dropdown
   anchors below the input as a popover; click-outside dismisses
   naturally because the dropdown lives inside .search-wrap. */
.search-wrap {
  position: relative;
  display: flex;
  align-items: center;
  width: 240px;
}
.search-icon {
  position: absolute;
  left: 7px;
  width: 14px;
  height: 14px;
  color: var(--color-t3);
  pointer-events: none;
}
.search-input {
  width: 100%;
  padding: 5px 26px 5px 24px;
  font-size: 11px;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  outline: none;
  transition: border-color 0.12s, box-shadow 0.12s;
}
.search-input:hover { border-color: var(--color-line2); }
.search-input:focus { border-color: var(--color-line2); box-shadow: var(--ring-focus); }
.search-input::placeholder { color: var(--color-t3); }
.search-clear {
  position: absolute;
  right: 4px;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: 3px;
  cursor: pointer;
}
.search-clear:hover { background: var(--color-bg2); color: var(--color-t1); }
.search-results {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  max-height: 240px;
  overflow-y: auto;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
  z-index: 30;
}
</style>
