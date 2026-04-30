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
const emit = defineEmits(['entity-click', 'counts-change'])

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

// Truncate keyword/description text used as an edge label so even a
// dense subgraph stays legible in the small pane.
function truncate(s, n = 18) {
  if (!s) return ''
  return s.length > n ? s.slice(0, n - 1) + '…' : s
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

// Filter the cached raw graph to either:
//   (a) every entity from this doc — when no chunk is selected, or
//   (b) only entities whose ``source_chunk_ids`` contains the active
//       chunk + the relations among them.
//
// Then rebuild graphology + sigma from scratch. Cheaper to throw
// out the old sigma instance and rebuild than to mutate the graph
// in place — forceAtlas2 has to re-settle either way once the node
// set changes, and the small filtered subgraph (5–30 nodes) is fast
// to rebuild.
function rebuildForCurrentFilter() {
  let nodes = rawNodes.value
  let edges = rawEdges.value
  const cid = props.activeChunkId
  if (cid) {
    const activeIds = new Set(
      nodes
        .filter((n) => (n.source_chunk_ids || []).includes(cid))
        .map((n) => n.id),
    )
    nodes = nodes.filter((n) => activeIds.has(n.id))
    edges = edges.filter(
      (e) => activeIds.has(e.source) && activeIds.has(e.target),
    )
  }
  counts.value = { entities: nodes.length, relations: edges.length }
  emit('counts-change', counts.value)
  buildGraph(nodes, edges)
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
      // Slightly bigger than before so labels read at this scale —
      // the pane is small but the labels still need to be legible.
      size: Math.max(4, Math.min(12, 3.5 + degree * 0.5)),
      color: typeFill(n.type),
    })
  }
  // Spread evenly first so forceAtlas2 has a non-degenerate start.
  circular.assign(g, { scale: 100 })

  // When the filter is active we surface relation keywords as edge
  // labels so the tiny subgraph reads as "X — verb-phrase — Y"
  // instead of just "two dots and a line". Labels are off in the
  // full-doc view (would clutter on a 50+ edge graph).
  const filtered = !!props.activeChunkId
  const edgeKeys = new Set()
  for (const e of rawEdges) {
    if (!g.hasNode(e.source) || !g.hasNode(e.target)) continue
    const k = `${e.source}->${e.target}`
    if (edgeKeys.has(k)) continue
    edgeKeys.add(k)
    g.addEdge(e.source, e.target, {
      type: 'arrow',
      size: 0.6,
      color: filtered
        ? 'rgba(180, 180, 180, 0.65)'
        : 'rgba(120, 120, 120, 0.35)',
      label: filtered ? truncate(e.keywords || e.description) : '',
      keywords: e.keywords || '',
      description: e.description || '',
    })
  }

  graph.value = g
  initSigma(g)
}

function initSigma(g) {
  // Edge labels: on when filtering (a small subgraph benefits from
  // seeing relation keywords), off on the full doc view (would clutter).
  const filtered = !!props.activeChunkId
  sigma = new Sigma(g, containerRef.value, {
    defaultNodeColor: '#6b7280',
    defaultEdgeColor: 'rgba(120, 120, 120, 0.35)',
    defaultEdgeType: 'arrow',
    renderEdgeLabels: filtered,
    edgeLabelFont: 'Geist, Inter, system-ui, sans-serif',
    edgeLabelSize: 10,
    edgeLabelColor: { color: '#9aa0a6' },
    labelFont: 'Geist, Inter, system-ui, sans-serif',
    labelSize: 12,
    labelWeight: '500',
    labelColor: { color: '#cccccc' },
    // Show labels even on small / dim nodes — the small pane needs
    // every node labeled to be useful.
    labelDensity: 1,
    labelRenderedSizeThreshold: 1,
    hideEdgesOnMove: false,
    hideLabelsOnMove: false,
    zIndex: true,
  })

  // Click → emit
  sigma.on('clickNode', ({ node }) => {
    emit('entity-click', node)
  })

  // Auto-layout: forceAtlas2 burst then settle, in a worker so the
  // main thread stays responsive. We use TWO different parameter
  // sets:
  //   - filtered subgraph (chunk selected, ~10-30 nodes): strong
  //     gravity + low scalingRatio packs the cluster tight so the
  //     few nodes don't stay flung at the canvas corners.
  //   - full-doc view (100+ nodes): plain forceAtlas2 defaults so
  //     the layout reads as a normal force-directed graph instead
  //     of a single dense blob.
  fa2 = new FA2Layout(g, {
    settings: filtered
      ? {
          gravity: 8,
          strongGravityMode: true,
          scalingRatio: 1.5,
          slowDown: 8,
          barnesHutOptimize: g.order > 100,
        }
      : {
          // Defaults — let community structure emerge naturally.
          barnesHutOptimize: g.order > 100,
        },
  })
  fa2.start()
  // Settle window scales with node count: tiny filtered subgraphs
  // converge in <500ms, full-doc graphs (100+ nodes) take longer.
  const settleMs = Math.min(2500, 500 + g.order * 12)
  setTimeout(() => {
    if (fa2) {
      fa2.stop()
      sigma?.refresh()
      // Only snap the camera tight on the filtered subgraph (the
      // small cluster benefits from a closer fit). For the full
      // doc, leave sigma at its auto-fit ratio so the whole graph
      // is visible end-to-end.
      if (filtered) {
        sigma?.getCamera().setState({ x: 0.5, y: 0.5, ratio: 0.85, angle: 0 })
      }
    }
  }, settleMs)
}

function destroySigma() {
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
