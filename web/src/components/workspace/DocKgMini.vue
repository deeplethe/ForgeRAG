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
import FA2Layout from 'graphology-layout-forceatlas2/worker'
import { circular } from 'graphology-layout'
import { getGraphByDoc } from '@/api'

const props = defineProps({
  docId: { type: String, required: true },
  activeChunkId: { type: String, default: '' },
})
const emit = defineEmits(['entity-click'])

// ── DOM + sigma refs ─────────────────────────────────────────────
const containerRef = ref(null)
const graph = shallowRef(null)
let sigma = null
let fa2 = null
const loading = ref(false)
const error = ref('')

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

// ── Load + build ────────────────────────────────────────────────
async function load() {
  if (!props.docId) return
  loading.value = true
  error.value = ''
  try {
    const r = await getGraphByDoc(props.docId)
    counts.value = {
      entities: r.nodes?.length || 0,
      relations: r.edges?.length || 0,
    }
    buildGraph(r.nodes || [], r.edges || [])
  } catch (e) {
    error.value = e?.message || String(e)
    console.error('DocKgMini load failed:', e)
  } finally {
    loading.value = false
  }
}

function buildGraph(rawNodes, rawEdges) {
  destroySigma()
  if (!rawNodes.length || !containerRef.value) {
    graph.value = null
    return
  }

  const g = new Graph()
  for (const n of rawNodes) {
    const degree = n.degree || 0
    g.addNode(n.id, {
      label: n.name,
      entityType: (n.type || 'UNKNOWN').toUpperCase(),
      sourceChunkIds: n.source_chunk_ids || [],
      x: (Math.random() - 0.5) * 100,
      y: (Math.random() - 0.5) * 100,
      // Smaller nodes than the full KG view because the pane is small.
      size: Math.max(2.5, Math.min(8, 2.5 + degree * 0.4)),
      color: typeFill(n.type),
    })
  }
  // Spread evenly first so forceAtlas2 has a non-degenerate start.
  circular.assign(g, { scale: 100 })

  const edgeKeys = new Set()
  for (const e of rawEdges) {
    if (!g.hasNode(e.source) || !g.hasNode(e.target)) continue
    const k = `${e.source}->${e.target}`
    if (edgeKeys.has(k)) continue
    edgeKeys.add(k)
    // Stash relation metadata so the reducer can surface keywords as
    // an edge label only when filter mode is active (otherwise labels
    // on every edge clutter the small pane).
    g.addEdge(e.source, e.target, {
      type: 'arrow',
      size: 0.6,
      color: 'rgba(120, 120, 120, 0.35)',
      keywords: e.keywords || '',
      description: e.description || '',
    })
  }

  graph.value = g
  initSigma(g)
}

function initSigma(g) {
  sigma = new Sigma(g, containerRef.value, {
    defaultNodeColor: '#6b7280',
    defaultEdgeColor: 'rgba(120, 120, 120, 0.35)',
    defaultEdgeType: 'arrow',
    // Edge labels are rendered conditionally — kept off by default
    // so the unfiltered view stays readable, flipped on by the
    // chunk-filter reducer below.
    renderEdgeLabels: false,
    edgeLabelFont: 'Geist, Inter, system-ui, sans-serif',
    edgeLabelSize: 8,
    edgeLabelColor: { color: '#9aa0a6' },
    labelFont: 'Geist, Inter, system-ui, sans-serif',
    labelSize: 9,
    labelWeight: '500',
    labelColor: { color: '#cccccc' },
    labelDensity: 0.5,
    labelRenderedSizeThreshold: 4,
    hideEdgesOnMove: false,
    hideLabelsOnMove: false,
    zIndex: true,
  })

  // Click → emit
  sigma.on('clickNode', ({ node }) => {
    emit('entity-click', node)
  })

  // Auto-layout: small forceAtlas2 burst then settle. Runs in a
  // worker so the main thread stays responsive even on bigger
  // graphs — the loading overlay below covers initial render until
  // the first frame paints.
  fa2 = new FA2Layout(g, {
    settings: {
      gravity: 1,
      scalingRatio: 4,
      slowDown: 8,
      barnesHutOptimize: g.order > 100,
    },
  })
  fa2.start()
  // Stop after a short settle window — small graphs converge fast.
  // Re-apply the chunk filter once after settle so the camera focus
  // (which depends on final node positions) is correct.
  setTimeout(() => {
    if (fa2) {
      fa2.stop()
      sigma?.refresh()
      applyDimming()
    }
  }, 1500)

  applyDimming()
}

// Truncate keyword/description text used as an edge label so even a
// dense neighbourhood stays legible in the small pane.
function truncate(s, n = 18) {
  if (!s) return ''
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

// Apply / remove the chunk-filter pass.  When ``activeChunkId`` is
// set, only entities whose ``source_chunk_ids`` includes it stay
// fully rendered; everything else fades into the background and
// drops below them in z-order, and the camera animates to the
// active sub-graph's centroid. Edges between two active nodes
// surface their keywords as labels so the user can see how the
// chunk's entities relate.
function applyDimming() {
  if (!sigma || !graph.value) return
  const cid = props.activeChunkId
  if (!cid) {
    // Reset to base view — no reducers, no edge labels.
    sigma.setSetting('renderEdgeLabels', false)
    sigma.setSetting('nodeReducer', (n, d) => d)
    sigma.setSetting('edgeReducer', (e, d) => d)
    sigma.refresh()
    return
  }
  // Gather active node IDs once so the reducers stay O(1) per element.
  const active = new Set()
  graph.value.forEachNode((nid, attr) => {
    if ((attr.sourceChunkIds || []).includes(cid)) active.add(nid)
  })
  sigma.setSetting('renderEdgeLabels', true)
  sigma.setSetting('nodeReducer', (node, data) => {
    if (active.has(node)) return { ...data, zIndex: 1 }
    // Dim non-active nodes to a near-background color and drop
    // them below the active set so the filtered subgraph reads
    // clearly even when it's spatially mixed in.
    return {
      ...data,
      color: 'rgba(40, 40, 40, 0.35)',
      label: null,
      zIndex: 0,
    }
  })
  sigma.setSetting('edgeReducer', (edge, data) => {
    const s = graph.value.source(edge)
    const t = graph.value.target(edge)
    if (active.has(s) && active.has(t)) {
      return {
        ...data,
        color: 'rgba(180, 180, 180, 0.65)',
        label: truncate(data.keywords || data.description),
        zIndex: 1,
      }
    }
    return {
      ...data,
      color: 'rgba(40, 40, 40, 0.18)',
      label: null,
      zIndex: 0,
    }
  })
  sigma.refresh()
  focusOnNodes(active)
}

// Animate the camera to the centroid of a node set, zooming in to
// roughly fit the cluster. ``getNodeDisplayData`` returns
// camera-space coords that ``camera.animate`` can consume directly.
function focusOnNodes(nodeIds) {
  if (!sigma || !nodeIds.size) return
  let sumX = 0
  let sumY = 0
  let count = 0
  for (const nid of nodeIds) {
    const dd = sigma.getNodeDisplayData(nid)
    if (!dd) continue
    sumX += dd.x
    sumY += dd.y
    count += 1
  }
  if (!count) return
  sigma.getCamera().animate(
    { x: sumX / count, y: sumY / count, ratio: 0.55 },
    { duration: 400 },
  )
}

function destroySigma() {
  try { fa2?.stop() } catch {}
  fa2 = null
  try { sigma?.kill() } catch {}
  sigma = null
}

watch(() => props.docId, load, { immediate: true })
watch(() => props.activeChunkId, applyDimming)

onBeforeUnmount(destroySigma)
</script>

<template>
  <div class="kg-mini">
    <div ref="containerRef" class="kg-mini__canvas" />
    <div v-if="loading" class="kg-mini__overlay">Loading…</div>
    <div v-else-if="error" class="kg-mini__overlay">Failed to load graph</div>
    <div v-else-if="!counts.entities" class="kg-mini__overlay">No entities for this document</div>
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
