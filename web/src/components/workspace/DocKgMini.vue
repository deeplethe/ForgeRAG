<!--
  DocKgMini — slim sigma graph for the workspace doc-detail KG pane.

  Loads only the entities sourced from this doc (via the new
  ``/api/v1/graph/by-doc/{doc_id}`` endpoint) plus the relations
  among them; renders a scaled-down version of the same
  graphology + sigma stack KnowledgeGraph.vue uses, without the
  layout selector, search panel, or legend.

  - Click a node → emits ``entity-click`` with the entity_id.
  - ``activeChunkId`` (optional) fades nodes whose
    ``source_chunk_ids`` doesn't contain the active chunk, so
    selecting a chunk in the workspace highlights the entities
    that came from it.
-->
<script setup>
import { onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import Graph from 'graphology'
import Sigma from 'sigma'
import { EdgeDoubleArrowProgram, drawDiscNodeHover } from 'sigma/rendering'
import FA2Layout from 'graphology-layout-forceatlas2/worker'
import { circlepack, circular } from 'graphology-layout'
import { getGraphByDoc } from '@/api'
import { useTheme } from '@/composables/useTheme'

const props = defineProps({
  docId: { type: String, required: true },
  activeChunkId: { type: String, default: '' },
})
const emit = defineEmits(['entity-click', 'counts-change'])

// ── DOM + sigma refs ─────────────────────────────────────────────
const containerRef = ref(null)
const graph = shallowRef(null)
let sigma = null
let fa2 = null
const loading = ref(false)
const error = ref('')

// ── Click-focus state (mirrors KnowledgeGraph.vue) ──────────────
// Clicking an entity dims everyone outside its 1-hop neighbourhood.
// Held outside the reactive system because it's read by sigma's
// reducers on every frame and we don't want Vue tracking to run
// each time — a manual ``sigma.refresh()`` is cheaper.
let selectedNodeId = null
let neighborSet = new Set()
let hoveredNode = null

// ── Theme-aware palette (mirror KnowledgeGraph.vue) ──────────────
const { isDark } = useTheme()
function graphColors() {
  return isDark.value
    ? {
        defaultEdge: '#3f3f46',
        label:       '#a1a1a1',
        // Edge labels (relation keywords) brighter than node labels
        // — same trade-off as KnowledgeGraph.vue: the keyword text
        // sits on top of dim grey edge lines and needs extra
        // contrast to read against the canvas background.
        edgeLabel:   '#d4d4d4',
        dimNode:     '#1f1f1f',
        focusEdge:   '#ededed',
        dimEdge:     '#1f1f1f',
      }
    : {
        defaultEdge: '#d1d5db',
        label:       '#374151',
        edgeLabel:   '#1f2937',
        dimNode:     '#d0d0d0',
        focusEdge:   '#3d3d3d',
        dimEdge:     '#e2e2e2',
      }
}

// ── Type → color (mirror KnowledgeGraph.vue) ─────────────────────
const TYPE_COLORS = {
  PERSON: '#555555',
  ORGANIZATION: '#0891b2',
  LOCATION: '#059669',
  CONCEPT: '#d97706',
  EVENT: '#dc2626',
  TECHNOLOGY: '#7c3aed',
  PRODUCT: '#db2777',
  DOCUMENT: '#0d9488',
  DATE: '#ea580c',
  UNKNOWN: '#6b7280',
}
function typeFill(type) {
  return TYPE_COLORS[(type || '').toUpperCase()] || '#6b7280'
}

const counts = ref({ entities: 0, relations: 0 })
// Raw fetch cached so we can rebuild a filtered subgraph cheaply
// when the active chunk changes (no extra network round-trip).
const rawNodes = ref([])
const rawEdges = ref([])

// ── Load + build ────────────────────────────────────────────────
async function load() {
  if (!props.docId) return
  loading.value = true
  error.value = ''
  try {
    const r = await getGraphByDoc(props.docId)
    rawNodes.value = r.nodes || []
    rawEdges.value = r.edges || []
    rebuildForCurrentFilter()
  } catch (e) {
    error.value = e?.message || String(e)
    console.error('DocKgMini load failed:', e)
  } finally {
    loading.value = false
  }
}

// Default-view sizing knobs. Anchors are the high-degree "structural"
// entities; halo expands the rendered set to one hop out from the
// anchor set so anchors actually have neighbours on the canvas to
// light up when clicked. Without halo, capping by degree alone
// shreds the edge set (high-degree nodes mostly connect to
// low-degree ones in scale-free networks), and click-focus has
// nothing to highlight.
const DEFAULT_ANCHORS = 200
const DEFAULT_TOTAL_CAP = 800

// Filter the cached raw graph for the current view mode:
//
//   • No chunk selected → top-N anchors by degree, plus 1-hop halo
//     reachable through any edge (capped to ``DEFAULT_TOTAL_CAP``
//     by halo degree). Keep every edge whose endpoints are in the
//     visible set. Density now matches the main /knowledge-graph
//     page closely enough that the reducer-based click-focus
//     actually has neighbours to light up.
//
//   • Chunk selected → strict filter: only entities whose
//     ``source_chunk_ids`` contains the active chunk + relations.
function rebuildForCurrentFilter() {
  const cid = props.activeChunkId
  let nodes = rawNodes.value
  let edges = rawEdges.value

  if (cid) {
    // Strict chunk filter: nodes whose extraction sourced this chunk,
    // AND edges whose extraction sourced this chunk too. The old
    // version only filtered nodes — every relation between them was
    // kept regardless of provenance, so the panel showed dozens of
    // cross-doc relations that had nothing to do with the chunk.
    // Now that the API exposes ``source_chunk_ids`` on edges, we can
    // be honest: only show what was actually extracted from here.
    const activeIds = new Set(
      nodes
        .filter((n) => (n.source_chunk_ids || []).includes(cid))
        .map((n) => n.id),
    )
    nodes = nodes.filter((n) => activeIds.has(n.id))
    edges = edges.filter(
      (e) =>
        activeIds.has(e.source) &&
        activeIds.has(e.target) &&
        (e.source_chunk_ids || []).includes(cid),
    )
  } else if (nodes.length > DEFAULT_ANCHORS) {
    // Pick anchor set: top by degree, ties broken by ownership.
    const ranked = [...nodes].sort((a, b) => {
      const da = a.degree || 0
      const db = b.degree || 0
      if (db !== da) return db - da
      const oa = (a.source_doc_ids || []).includes(props.docId) ? 1 : 0
      const ob = (b.source_doc_ids || []).includes(props.docId) ? 1 : 0
      return ob - oa
    })
    const anchors = new Set(
      ranked.slice(0, DEFAULT_ANCHORS).map((n) => n.id),
    )
    // Halo: any node connected to an anchor via at least one edge.
    const halo = new Set()
    for (const e of edges) {
      if (anchors.has(e.source) && !anchors.has(e.target)) halo.add(e.target)
      else if (anchors.has(e.target) && !anchors.has(e.source)) halo.add(e.source)
    }
    // Cap halo by degree so the panel doesn't drown in 5k nodes.
    const cap = Math.max(0, DEFAULT_TOTAL_CAP - anchors.size)
    let haloKept = halo
    if (halo.size > cap) {
      const haloRanked = nodes
        .filter((n) => halo.has(n.id))
        .sort((a, b) => (b.degree || 0) - (a.degree || 0))
        .slice(0, cap)
      haloKept = new Set(haloRanked.map((n) => n.id))
    }
    const keep = new Set([...anchors, ...haloKept])
    nodes = nodes.filter((n) => keep.has(n.id))
    edges = edges.filter(
      (e) => keep.has(e.source) && keep.has(e.target),
    )
  }

  // Drop click-focus state so the reducer doesn't dim against a
  // node id that may not even be in the new visible set.
  selectedNodeId = null
  neighborSet = new Set()

  counts.value = {
    entities: nodes.length,
    relations: edges.length,
    total: rawNodes.value.length,
  }
  emit('counts-change', counts.value)
  buildGraph(nodes, edges)
}

function buildGraph(rawNodes, rawEdges) {
  destroySigma()
  if (!rawNodes.length || !containerRef.value) {
    graph.value = null
    return
  }

  const filtered = !!props.activeChunkId
  const g = new Graph()
  for (const n of rawNodes) {
    const degree = n.degree || 0
    g.addNode(n.id, {
      label: n.name,
      entityType: (n.type || 'UNKNOWN').toUpperCase(),
      sourceChunkIds: n.source_chunk_ids || [],
      x: (Math.random() - 0.5) * 100,
      y: (Math.random() - 0.5) * 100,
      // Slightly bigger than before so labels read at this scale —
      // the pane is small but the labels still need to be legible.
      size: Math.max(4, Math.min(12, 3.5 + degree * 0.5)),
      color: typeFill(n.type),
    })
  }
  // Default (no chunk selected): D3-style circle pack via
  // ``graphology-layout``'s ``circlepack``, hierarchically grouped
  // by ``entityType``. Same layout the main /knowledge-graph page
  // uses — each type gets its own packed circle of nodes, then the
  // type-circles pack against each other. Visually separable
  // type-clusters with no force-settle wait. Force-directed layouts
  // (FA2) collapse 200+ entities into an unreadable blob.
  if (!filtered) {
    circlepack.assign(g, {
      hierarchyAttributes: ['entityType'],
      scale: 200,
    })
  } else {
    // Filtered subgraph (5-30 nodes) — circular seed, FA2 takes over.
    circular.assign(g, { scale: 100 })
  }

  // Edge build with bidirectional folding: when both A→B and B→A
  // exist (with possibly different keywords), we collapse them into
  // a single edge rendered with arrowheads on both ends so the two
  // directions don't visually overlap on the same line. Keywords
  // are joined; provenance (source_chunk_ids / source_doc_ids) is
  // unioned so the chunk-strict filter still finds the merged edge
  // when either original direction sourced this chunk.
  const edgeColor = graphColors().defaultEdge
  const pairKey = (a, b) => (a < b ? `${a}|${b}` : `${b}|${a}`)
  const buckets = new Map() // pairKey -> { a, b, fwdKw, bwdKw, srcChunks, srcDocs }
  for (const e of rawEdges) {
    if (!g.hasNode(e.source) || !g.hasNode(e.target)) continue
    const k = pairKey(e.source, e.target)
    let entry = buckets.get(k)
    if (!entry) {
      entry = {
        a: e.source < e.target ? e.source : e.target,
        b: e.source < e.target ? e.target : e.source,
        fwdKw: '', // a → b
        bwdKw: '', // b → a
        srcChunks: new Set(),
        srcDocs: new Set(),
        fwdDesc: '',
        bwdDesc: '',
      }
      buckets.set(k, entry)
    }
    const isForward = e.source === entry.a
    if (isForward) {
      entry.fwdKw = e.keywords || entry.fwdKw
      entry.fwdDesc = e.description || entry.fwdDesc
    } else {
      entry.bwdKw = e.keywords || entry.bwdKw
      entry.bwdDesc = e.description || entry.bwdDesc
    }
    for (const cid of e.source_chunk_ids || []) entry.srcChunks.add(cid)
    for (const did of e.source_doc_ids || []) entry.srcDocs.add(did)
  }
  for (const entry of buckets.values()) {
    const bidirectional = entry.fwdKw !== '' && entry.bwdKw !== ''
    // For bidirectional, join with " ↔ " so the label reads cleanly;
    // single-direction edges keep their original keyword unchanged.
    const mergedKw = bidirectional
      ? `${entry.fwdKw} ↔ ${entry.bwdKw}`
      : entry.fwdKw || entry.bwdKw
    const mergedDesc = bidirectional
      ? `${entry.fwdDesc} | ${entry.bwdDesc}`.trim()
      : entry.fwdDesc || entry.bwdDesc
    g.addEdge(entry.a, entry.b, {
      type: bidirectional ? 'doubleArrow' : 'arrow',
      size: 0.6,
      color: edgeColor,
      // Always carry the full keyword as ``label`` — the edgeReducer
      // gates per-render whether sigma should actually render it
      // (only on focus edges when an entity is selected, or on all
      // edges when chunk-filtered into a small subgraph).
      // ``drawStraightEdgeLabel`` then ellipsizes at the real edge
      // pixel length; our old hard-truncate was double-clipping.
      label: mergedKw,
      keywords: mergedKw,
      description: mergedDesc,
      source_chunk_ids: [...entry.srcChunks],
      source_doc_ids: [...entry.srcDocs],
    })
  }

  graph.value = g
  initSigma(g)
}

function initSigma(g) {
  // Edge labels are always allowed at the renderer level; the
  // edgeReducer below decides per edge whether to show the label
  // (focus edge during entity selection, OR every edge when in the
  // small chunk-filtered subgraph). On the default top-N view with
  // no entity selected, labels are stripped entirely to avoid
  // clutter on a 1500-edge canvas.
  // Kill any prior sigma instance first — every ``new Sigma()``
  // grabs a WebGL context, and browsers cap concurrent contexts
  // (~16 in Chrome). Without this, repeated chunk / doc / theme
  // rebuilds leak contexts until ``getContext('webgl')`` returns
  // null and the next init throws "Cannot read properties of null
  // (reading 'blendFunc')".
  destroySigma()
  const filtered = !!props.activeChunkId
  const c = graphColors()
  sigma = new Sigma(g, containerRef.value, {
    defaultNodeColor: '#6b7280',
    defaultEdgeColor: c.defaultEdge,
    defaultEdgeType: 'arrow',
    // Register the bidirectional renderer so edges of ``type:
    // 'doubleArrow'`` (used after pair-folding above) draw with
    // arrowheads on both ends. Sigma merges this with its built-in
    // edge program map (arrow/line/rectangle stay available).
    edgeProgramClasses: {
      doubleArrow: EdgeDoubleArrowProgram,
    },
    renderEdgeLabels: true,
    edgeLabelFont: 'Geist, Inter, system-ui, sans-serif',
    edgeLabelSize: 11,
    edgeLabelColor: { color: c.edgeLabel },
    edgeLabelWeight: '500',
    labelFont: 'Geist, Inter, system-ui, sans-serif',
    labelSize: 12,
    labelWeight: '500',
    labelColor: { color: c.label },
    // Show labels even on small / dim nodes — the small pane needs
    // every node labeled to be useful.
    labelDensity: 1,
    labelRenderedSizeThreshold: 1,
    hideEdgesOnMove: false,
    hideLabelsOnMove: false,
    zIndex: true,
    // Custom hover painter — sigma calls this for every
    // ``highlighted: true`` node onto the 2D ``hovers`` canvas
    // (separate pass from the WebGL hover-canvas re-render).
    // We only want the halo + label-pill on nodes flagged
    // ``withHalo`` (selected anchor / mouse-hovered); neighbours
    // get the silent WebGL re-render only.
    defaultDrawNodeHover: (ctx, data, settings) => {
      if (data.withHalo) drawDiscNodeHover(ctx, data, settings)
    },
  })

  // ── Reducers: dim non-neighbours when an entity is selected ──
  // Same focus pattern as /knowledge-graph. Non-neighbours keep
  // their full size + label so the surrounding context stays
  // readable; they only get the dim colour. The edges canvas is
  // moved above the nodes canvas (see ``liftEdgesAboveNodes``) so
  // focusEdge lines from selected to neighbours are no longer
  // occluded by these dim circles.
  // ``withHalo`` is a custom flag distinguishing nodes that should
  // get the 2D halo + label-pill (selected / mouse-hovered) from
  // nodes that just need the WebGL hover-canvas re-render to sit
  // above edges (neighbours). The custom ``defaultDrawNodeHover``
  // below reads it and skips the halo for plain neighbours.
  sigma.setSetting('nodeReducer', (node, data) => {
    const res = { ...data }
    if (selectedNodeId) {
      if (node === selectedNodeId) {
        res.highlighted = true
        res.withHalo = true
        res.zIndex = 2
      } else if (neighborSet.has(node)) {
        // Highlight neighbours so they render via sigma's
        // ``hoverNodes`` canvas (top of stack, above the lifted
        // edges canvas). Without this the edge body would run
        // visibly through the neighbour disc on short edges —
        // EdgeArrowProgram only offsets the arrowhead to the node
        // edge, the line itself goes center-to-center. ``withHalo``
        // is intentionally NOT set so neighbours don't get the
        // bright halo / label-pill.
        res.highlighted = true
        res.zIndex = 1
      } else {
        res.color = graphColors().dimNode
        res.label = null
        res.zIndex = 0
      }
    }
    if (hoveredNode && node === hoveredNode) {
      res.highlighted = true
      res.withHalo = true
    }
    return res
  })
  // Edge label gating:
  //   • selectedNodeId set + edge touches it → keep label (merged
  //     keyword from buildGraph)
  //   • selectedNodeId set + edge doesn't touch it → hidden, label
  //     null (won't render anyway)
  //   • no selection but chunk-filtered → small subgraph, keep all
  //     labels so the structural reading "X — verb-phrase — Y" works
  //   • no selection and no chunk filter → 1500-edge default view,
  //     strip every label (would be unreadable clutter otherwise)
  const filteredView = !!props.activeChunkId
  sigma.setSetting('edgeReducer', (edge, data) => {
    const res = { ...data }
    if (selectedNodeId) {
      const src = g.source(edge)
      const tgt = g.target(edge)
      if (src === selectedNodeId || tgt === selectedNodeId) {
        res.color = graphColors().focusEdge
        res.size = 2
        res.zIndex = 1
        // keep res.label = merged keyword
      } else {
        res.color = graphColors().dimEdge
        res.hidden = true
        res.label = null
      }
    } else if (!filteredView) {
      res.label = null
    }
    return res
  })

  // Hover (matches main KG page).
  sigma.on('enterNode', ({ node }) => {
    hoveredNode = node
    sigma.refresh()
  })
  sigma.on('leaveNode', () => {
    hoveredNode = null
    sigma.refresh()
  })
  // Click on a node selects it; click on empty stage clears.
  sigma.on('clickNode', ({ node }) => {
    selectNode(node)
    emit('entity-click', node)
  })
  sigma.on('clickStage', () => {
    clearSelection()
  })

  if (!filtered) {
    // Default view: positions were assigned synchronously in
    // buildGraph (circular-pack + noverlap). Skip force layout
    // entirely — for hundreds of entities, FA2 collapses them into
    // an unreadable blob and never settles in the small pane.
    return
  }

  // Filtered subgraph (~5-30 nodes): forceAtlas2 in a worker for a
  // tight, organic cluster. Strong gravity packs nodes near center
  // so the few entities don't fling to the canvas corners.
  fa2 = new FA2Layout(g, {
    settings: {
      gravity: 8,
      strongGravityMode: true,
      scalingRatio: 1.5,
      slowDown: 8,
      barnesHutOptimize: g.order > 100,
    },
  })
  fa2.start()
  const settleMs = Math.min(2500, 500 + g.order * 12)
  setTimeout(() => {
    if (fa2) {
      fa2.stop()
      sigma?.refresh()
      sigma?.getCamera().setState({ x: 0.5, y: 0.5, ratio: 0.85, angle: 0 })
    }
  }, settleMs)
}

// Sigma renders to 7 stacked <canvas> layers. Default order
// (bottom→top): edges, edgeLabels, nodes, labels, hovers,
// hoverNodes, mouse — both edges AND edgeLabels sit BELOW nodes,
// so dim non-neighbour discs occlude both focus edges and their
// labels during selection. Fix: reorder the edges + edgeLabels
// canvases above nodes while a selection is active. Sigma's
// per-edge / per-node zIndex setting only orders within a single
// layer, never across them.
function liftEdgesAboveNodes() {
  if (!sigma) return
  const c = sigma.getCanvases()
  if (c.labels && c.edges) c.labels.before(c.edges)
  if (c.labels && c.edgeLabels) c.labels.before(c.edgeLabels)
}
function restoreEdgesBelowNodes() {
  if (!sigma) return
  const c = sigma.getCanvases()
  // Order matters: prepend edgeLabels first, then edges. Each
  // ``prepend`` puts the element at idx 0; the second one pushes
  // the first to idx 1. End state: [edges, edgeLabels, nodes, ...].
  if (c.edgeLabels && sigma.container) sigma.container.prepend(c.edgeLabels)
  if (c.edges && sigma.container) sigma.container.prepend(c.edges)
}

// Click a node → set selection state, sigma reducers handle the
// dim/highlight. Mirrors /knowledge-graph; no graph rebuild because
// the halo expansion in ``rebuildForCurrentFilter`` already keeps
// 1-hop neighbours on the canvas.
function selectNode(nodeId) {
  selectedNodeId = nodeId
  neighborSet = new Set()
  if (graph.value?.hasNode(nodeId)) {
    graph.value.forEachNeighbor(nodeId, (n) => neighborSet.add(n))
  }
  neighborSet.add(nodeId)
  liftEdgesAboveNodes()
  sigma?.refresh()
}

function clearSelection() {
  if (!selectedNodeId) return
  selectedNodeId = null
  neighborSet = new Set()
  restoreEdgesBelowNodes()
  sigma?.refresh()
}

function destroySigma() {
  // Drop selection state along with the sigma instance — otherwise
  // the next graph build inherits a selection pointing at a node
  // that no longer exists, and the reducer dims everything.
  selectedNodeId = null
  neighborSet = new Set()
  hoveredNode = null
  try { fa2?.stop() } catch {}
  fa2 = null
  try { sigma?.kill() } catch {}
  sigma = null
}

watch(() => props.docId, load, { immediate: true })
// Chunk-change rebuilds the subgraph (drops irrelevant nodes/edges
// and re-runs forceAtlas2) instead of dimming. Cheaper conceptually
// and makes the camera land tightly on the filtered cluster.
watch(() => props.activeChunkId, rebuildForCurrentFilter)
// Theme toggle: rebuild so edge/label colours pick up the new
// palette. Cheaper than the main KG page's reducer-tweak path
// because the small panel rebuilds quickly anyway.
watch(isDark, rebuildForCurrentFilter)

onBeforeUnmount(destroySigma)
</script>

<template>
  <div class="kg-mini">
    <div ref="containerRef" class="kg-mini__canvas" />
    <div v-if="loading" class="kg-mini__overlay">Loading…</div>
    <div v-else-if="error" class="kg-mini__overlay">Failed to load graph</div>
    <div v-else-if="!counts.entities && activeChunkId" class="kg-mini__overlay">
      No entities for this chunk
    </div>
    <div v-else-if="!counts.entities" class="kg-mini__overlay">
      No entities for this document
    </div>
  </div>
</template>

<style scoped>
.kg-mini {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 0;
}
.kg-mini__canvas {
  position: absolute;
  inset: 0;
}
.kg-mini__overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  color: var(--color-t3);
  pointer-events: none;
}
</style>
