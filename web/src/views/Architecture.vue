<script setup>
import { ref, reactive, computed, onMounted, markRaw } from 'vue'
import { VueFlow, Position, Handle } from '@vue-flow/core'
import { Controls } from '@vue-flow/controls'
import { Background } from '@vue-flow/background'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import { getStats, getRetrievalStatus, getAllSettings, updateSetting, uploadAndIngest, getInfrastructure, listLLMProviders, createLLMProvider, updateLLMProvider, deleteLLMProvider } from '@/api'
import { request } from '@/api/client'
import { TrashIcon, ClipboardDocumentIcon, ClipboardDocumentCheckIcon, ArrowPathIcon } from '@heroicons/vue/24/outline'
import Spinner from '@/components/Spinner.vue'

/* ── State ── */
const stats = ref({ documents: 0, chunks: 0, files: 0, bm25_indexed: 0 })
const status = ref({})
const settings = ref({})
const infra = ref({ storage_mode: '', storage_root: '', relational_backend: '', relational_path: '', vector_backend: '', vector_detail: '', graph_backend: '', graph_detail: '' })
const activeNode = ref(null)
const restarting = ref(false)
const loading = ref(true)

async function restartServer() {
  restarting.value = true
  try { await request('/api/v1/system/restart', { method: 'POST' }) } catch {}
  const poll = setInterval(async () => {
    try { const r = await fetch('/api/v1/health'); if (r.ok) { clearInterval(poll); restarting.value = false; await load() } } catch {}
  }, 2000)
  setTimeout(() => { clearInterval(poll); restarting.value = false }, 30000)
}

/* ── Drag-drop ingest ── */
const tasks = ref([])
const dragging = ref(false)
function onDragOver(e) { e.preventDefault(); dragging.value = true }
function onDragLeave(e) { if (!e.currentTarget.contains(e.relatedTarget)) dragging.value = false }
async function onDrop(e) {
  e.preventDefault(); dragging.value = false
  for (const file of Array.from(e.dataTransfer?.files || []).filter(f => /\.(pdf|docx|pptx|xlsx)$/i.test(f.name))) {
    const id = Date.now().toString(36); tasks.value.push({ id, name: file.name, s: 'run' })
    try { await uploadAndIngest(file); tasks.value = tasks.value.map(t => t.id === id ? { ...t, s: 'ok' } : t) }
    catch { tasks.value = tasks.value.map(t => t.id === id ? { ...t, s: 'err' } : t) }
  }
  await load(); setTimeout(() => { tasks.value = tasks.value.filter(t => t.s !== 'ok') }, 2000)
}

/* ── LLM Providers ── */
const providers = ref([])
const showNewProvider = ref(false)
const newProv = reactive({ name: '', provider_type: 'chat', api_base: '', model_name: '', api_key: '' })

async function loadProviders() { try { providers.value = await listLLMProviders() } catch {} }
function providersByType(type) { return providers.value.filter(p => p.provider_type === type) }
function providerName(id) { const p = providers.value.find(x => x.id === id); return p ? p.name : id || '\u2014' }
function providerTypeForKey(key) {
  if (key.includes('embedder') || key.includes('embedding')) return 'embedding'
  if (key.includes('rerank')) return 'reranker'
  if (key.includes('image_enrichment')) return 'vlm'
  return 'chat'
}
function providersForKey(key) { return providersByType(providerTypeForKey(key)) }
async function doCreateProvider() {
  if (!newProv.name || !newProv.model_name) return
  try {
    await createLLMProvider({ name: newProv.name, providerType: newProv.provider_type, apiBase: newProv.api_base || null, modelName: newProv.model_name, apiKey: newProv.api_key || null })
    newProv.name = ''; newProv.provider_type = 'chat'; newProv.api_base = ''; newProv.model_name = ''; newProv.api_key = ''
    showNewProvider.value = false; await loadProviders()
  } catch (e) { console.error('doCreateProvider failed:', e) }
}
const editProv = reactive({ show: false, id: null, name: '', provider_type: 'chat', api_base: '', model_name: '', api_key: '', x: 0, y: 0 })
function openEditProvider(p, e) {
  e.stopPropagation()
  if (editProv.show && editProv.id === p.id) { editProv.show = false; return }
  const r = e.currentTarget.getBoundingClientRect()
  editProv.id = p.id; editProv.name = p.name; editProv.provider_type = p.provider_type
  editProv.model_name = p.model_name; editProv.api_base = p.api_base || ''; editProv.api_key = ''
  editProv.x = r.right + 8; editProv.y = Math.min(r.top, window.innerHeight - 360)
  editProv.show = true
}
async function doSaveProvider() {
  if (!editProv.name || !editProv.model_name) return
  try {
    await updateLLMProvider(editProv.id, { name: editProv.name, providerType: editProv.provider_type, modelName: editProv.model_name, apiBase: editProv.api_base || null, ...(editProv.api_key ? { apiKey: editProv.api_key } : {}) })
    editProv.show = false; await loadProviders()
  } catch (e) { console.error('doSaveProvider failed:', e) }
}
async function doDeleteProvider(id) {
  try { await deleteLLMProvider(id); editProv.show = false; await loadProviders() }
  catch (e) { console.error('doDeleteProvider failed:', e) }
}

/* ── Module descriptions ── */
const moduleDesc = {
  file_upload: { title: 'File Upload', desc: 'Accepts PDF, DOCX, PPTX, XLSX, HTML, Markdown, and TXT. Non-PDF formats are converted to PDF for unified parsing. Files are content-addressed by SHA-256 for deduplication.' },
  parser: { title: 'Document Parser', desc: 'Multi-format document parsing with automatic backend routing. PyMuPDF for fast text extraction, MinerU for layout-aware parsing (tables, formulas, complex layouts), VLM for scanned or visually complex documents.' },
  chunker: { title: 'Chunker', desc: 'Tree-aware chunk generation that respects document structure. Walks the tree in preorder, packing blocks into chunks within section boundaries. Tables, figures, and formulas can be isolated into standalone chunks.' },
  tree_builder: { title: 'Tree Builder', desc: 'Builds a document\'s hierarchical structure using LLM-based page-group analysis. The resulting tree with per-node summaries powers PageIndex-style tree navigation during retrieval.' },
  embedding: { title: 'Embedder', desc: 'Generates dense vector representations for semantic search. Uses LiteLLM for unified access to any embedding provider. Includes an on-disk cache keyed by content hash.' },
  kg_extraction: { title: 'KG Extraction', desc: 'LLM-powered entity and relation extraction during ingestion. Entities are deduplicated and merged across chunks, building a document-spanning knowledge graph.' },
  database: { title: 'Relational DB', desc: 'Stores documents, chunks, tree structures, conversations, traces, and runtime settings. Settings stored in DB override YAML config and take effect immediately.' },
  vector_store: { title: 'Vector Store', desc: 'Persists dense vector embeddings for semantic similarity retrieval. Supports ChromaDB, pgvector, Qdrant, Milvus, and Weaviate backends.' },
  graph_store: { title: 'Graph Store', desc: 'Persists knowledge graph entities, relations, and community structures. Supports NetworkX (in-memory) and Neo4j backends.' },
  user_query: { title: 'User Query', desc: 'Natural language question from the user, entering the retrieval pipeline.' },
  qu: { title: 'Query Understanding', desc: 'A single LLM call that performs intent classification, retrieval routing, and query expansion (synonym/translation variants for broader recall).' },
  vector: { title: 'Vector Search', desc: 'Semantic similarity retrieval using dense embeddings. Finds chunks whose meaning is closest to the query regardless of exact keyword overlap.' },
  bm25: { title: 'BM25 Search', desc: 'Sparse keyword retrieval using the BM25 ranking function. Excels at exact-match and terminology-heavy queries.' },
  tree: { title: 'Tree Navigation', desc: 'PageIndex-style LLM reasoning over document hierarchy. BM25 and vector hits are annotated onto the tree outline as heat-map markers. The LLM verifies and expands relevant sections.' },
  kg: { title: 'KG Path', desc: 'Multi-hop entity-relation reasoning. Extracts entities from the query, then traverses the graph. Dual-level retrieval: local (direct neighbors) and global (community summaries).' },
  fusion: { title: 'RRF Merge', desc: 'Combines results from tree navigation and knowledge graph paths using Reciprocal Rank Fusion. BM25 and vector serve as pre-filters; they enter RRF as fallback when tree is unavailable.' },
  expansion: { title: 'Context Expansion', desc: 'Enriches retrieved chunks with surrounding context. Descendant expansion pulls child content. Sibling expansion adds neighboring sections. Cross-reference expansion follows document links.' },
  rerank: { title: 'Rerank', desc: 'LLM-powered re-scoring of candidate chunks. Reads each chunk against the query and assigns a fine-grained relevance score.' },
  prompt_builder: { title: 'Prompt Builder', desc: 'Assembles the final prompt from top-ranked chunks, KG context, conversation history, and system instructions.' },
  generator: { title: 'LLM Generation', desc: 'Produces grounded answers by feeding top-ranked context to an LLM. Supports SSE streaming for real-time token delivery and multi-turn conversations.' },
  citation_builder: { title: 'Citation Builder', desc: 'Maps inline [c_N] citation tags to original PDF bounding-box coordinates, section paths, and file references.' },
  answer: { title: 'Answer', desc: 'The pipeline output: a natural-language answer with pixel-precise bbox citations. Each citation enables highlight-on-click in the built-in PDF viewer.' },
  filestore: { title: 'Storage & Cache', desc: 'Content-addressed blob storage for uploaded documents. Manages on-disk caches for BM25 index and embedding vectors.' },
}

/* ── Data loading ── */
onMounted(async () => { await load(); loading.value = false })
async function load() {
  const [s, st, se, inf] = await Promise.allSettled([
    getStats(), getRetrievalStatus(), getAllSettings(), getInfrastructure(),
  ])
  if (s.status === 'fulfilled') stats.value = s.value
  if (st.status === 'fulfilled') status.value = st.value
  if (se.status === 'fulfilled') settings.value = se.value.groups || {}
  if (inf.status === 'fulfilled') infra.value = inf.value
  await loadProviders()
}

function gv(key) {
  for (const items of Object.values(settings.value)) {
    const f = items.find(s => s.key === key); if (f) return f.value_json
  }
  return null
}

function toggle(key) {
  const map = { vector: 'retrieval.vector.enabled', bm25: 'retrieval.bm25.enabled', tree: 'retrieval.tree_path.enabled', rerank: 'retrieval.rerank.enabled', qu: 'retrieval.query_understanding.enabled', kg: 'retrieval.kg_path.enabled', kg_extraction: 'retrieval.kg_extraction.enabled' }
  if (!map[key]) return
  const newVal = !gv(map[key])
  // Optimistic: patch local state immediately
  for (const items of Object.values(settings.value)) {
    const f = items.find(s => s.key === map[key])
    if (f) { f.value_json = newVal; break }
  }
  const statusMap = { vector: 'vector_enabled', bm25: 'bm25_enabled', tree: 'tree_enabled', rerank: 'rerank_enabled', qu: 'query_understanding_enabled', kg: 'kg_enabled', kg_extraction: 'kg_extraction_enabled' }
  status.value = { ...status.value, [statusMap[key]]: newVal }
  // Fire-and-forget PUT
  saveCount++
  saving.value = true
  updateSetting(map[key], newVal)
    .catch(e => console.error('toggle failed:', e))
    .finally(() => { if (--saveCount <= 0) { saveCount = 0; saving.value = false } })
}
function isOn(key) {
  const map = { vector: 'vector_enabled', bm25: 'bm25_enabled', tree: 'tree_enabled', rerank: 'rerank_enabled', qu: 'query_understanding_enabled', kg: 'kg_enabled', kg_extraction: 'kg_extraction_enabled' }
  return status.value[map[key]] === true
}

/* ── Settings panel ── */
const groupMap = {
  filestore: ['cache'], file_upload: [],
  parser: ['parser', 'images'], tree_builder: ['tree_builder'], chunker: ['chunker'],
  embedding: ['embedding'], kg_extraction: ['kg_extraction'],
  database: [], vector_store: [], graph_store: [],
  user_query: [], qu: ['query_understanding', 'prompts_qu'],
  vector: ['retrieval_vector'], bm25: ['retrieval_bm25'],
  tree: ['retrieval_tree', 'prompts_tree'], kg: ['kg'],
  fusion: ['retrieval_fusion'], expansion: ['context_expansion'],
  rerank: ['rerank', 'prompts_rerank'],
  prompt_builder: [], generator: ['llm', 'prompts_gen'],
  citation_builder: [], answer: [],
}

// Keys already shown as the header toggle — exclude from settings list
const TOGGLE_KEYS = new Set(Object.values({ vector: 'retrieval.vector.enabled', bm25: 'retrieval.bm25.enabled', tree: 'retrieval.tree_path.enabled', rerank: 'retrieval.rerank.enabled', qu: 'retrieval.query_understanding.enabled', kg: 'retrieval.kg_path.enabled', kg_extraction: 'retrieval.kg_extraction.enabled' }))

// Parent-child hide rules: when parent key is off, hide children matching prefix
const PARENT_CHILD = [
  { parent: 'parser.tree_builder.llm_enabled', prefix: 'parser.tree_builder.provider_id' },
  { parent: 'parser.tree_builder.llm_enabled', prefix: 'parser.tree_builder.summary_max_workers' },
  { parent: 'parser.backends.mineru.enabled', prefix: 'parser.backends.mineru.backend' },
  { parent: 'parser.backends.mineru.enabled', prefix: 'parser.backends.mineru.device' },
  { parent: 'image_enrichment.enabled', prefix: 'image_enrichment.provider_id' },
  { parent: 'image_enrichment.enabled', prefix: 'image_enrichment.max_workers' },
  { parent: 'retrieval.tree_path.llm_nav_enabled', prefix: 'retrieval.tree_path.nav.' },
  { parent: 'retrieval.kg_extraction.embed_relations', prefix: 'retrieval.kg_path.relation_weight' },
  { parent: 'graph.community_detection.enabled', prefix: 'retrieval.kg_path.community_weight' },
]

const panelItems = computed(() => {
  const node = activeNode.value
  if (!node) return []
  const gs = groupMap[node] || []
  const config = [], prompts = []
  for (const g of gs) {
    if (!settings.value[g]) continue
    for (const s of settings.value[g]) {
      if (TOGGLEABLE.has(node) && TOGGLE_KEYS.has(s.key)) continue
      if (s.value_type === 'textarea') prompts.push(s)
      else config.push(s)
    }
  }
  // Config items first (preserve backend order), then prompts at the end
  return [...config, ...prompts]
})

function isParentDisabled(item) {
  for (const rule of PARENT_CHILD) {
    if (item.key === rule.prefix || item.key.startsWith(rule.prefix)) {
      const parentVal = gv(rule.parent)
      if (!parentVal) return true
    }
  }
  return false
}

const saving = ref(false)
let saveCount = 0
function saveSetting(key, val) {
  // Optimistic local update — immediate, no await
  for (const items of Object.values(settings.value)) {
    const f = items.find(s => s.key === key)
    if (f) { f.value_json = val; break }
  }
  // Fire-and-forget PUT — never blocks UI
  saveCount++
  saving.value = true
  updateSetting(key, val)
    .catch(e => console.error('saveSetting failed:', e))
    .finally(() => { if (--saveCount <= 0) { saveCount = 0; saving.value = false } })
}

const copiedKey = ref(null)
function copyPrompt(it) {
  const text = it.value_json || it.default_value || ''
  navigator.clipboard.writeText(text)
  copiedKey.value = it.key
  setTimeout(() => { if (copiedKey.value === it.key) copiedKey.value = null }, 1500)
}

const isInfraNode = (id) => ['database', 'vector_store', 'graph_store'].includes(id)
const hasConfig = (id) => (groupMap[id] || []).length > 0 || isInfraNode(id)

/* ── Vue Flow graph definition ── */
const TOGGLEABLE = new Set(['vector', 'bm25', 'tree', 'rerank', 'qu', 'kg', 'kg_extraction'])

/*
 * Grid layout — nodes snapped to columns (x) and rows (y).
 * Columns: 0=0, 1=200, 2=420, 3=640, 4=860
 * Vertical gaps between layers: a=0, b=200, c=380, d=700
 */
const C = [0, 200, 420, 640, 860] // column x
const R = { a: 0, b: 230, c: 370, d: 750 } // layer y base

const nodesDef = [
  // (a) Document Ingestion — two rows with 110px gap
  { id: 'file_upload', label: 'File Upload', desc: 'PDF, DOCX, PPTX, HTML...', layer: 'a', pos: [C[0], R.a] },
  { id: 'parser', label: 'Document Parser', desc: 'PyMuPDF \u00b7 MinerU \u00b7 VLM', layer: 'a', pos: [C[1], R.a] },
  { id: 'chunker', label: 'Chunker', desc: 'Token-based \u00b7 tree-aware', layer: 'a', pos: [C[2], R.a] },
  { id: 'tree_builder', label: 'Tree Builder', desc: 'LLM page-group inference', layer: 'a', pos: [C[3], R.a] },
  { id: 'embedding', label: 'Embedder', desc: 'Dense vector encoding', layer: 'a', pos: [C[2], R.a + 110] },
  { id: 'kg_extraction', label: 'KG Extraction', desc: 'Entity + relation extraction', layer: 'a', pos: [C[3], R.a + 110] },

  // (b) Persistence — one row
  { id: 'database', label: 'Relational DB', desc: 'SQLite \u00b7 PostgreSQL \u00b7 MySQL', layer: 'b', pos: [C[1], R.b] },
  { id: 'vector_store', label: 'Vector Store', desc: 'ChromaDB \u00b7 pgvector \u00b7 Qdrant', layer: 'b', pos: [C[2], R.b] },
  { id: 'graph_store', label: 'Graph Store', desc: 'NetworkX \u00b7 Neo4j', layer: 'b', pos: [C[3], R.b] },

  // (c) Retrieval — 4 search paths with 90px gaps
  { id: 'user_query', label: 'User Query', desc: 'Natural language question', layer: 'c', pos: [C[0], R.c + 95] },
  { id: 'qu', label: 'Query Understanding', desc: 'Expand & classify intent', layer: 'c', pos: [C[1], R.c + 95] },
  { id: 'bm25', label: 'BM25', desc: 'Keyword matching', layer: 'c', pos: [C[2], R.c] },
  { id: 'vector', label: 'Vector Search', desc: 'Semantic similarity', layer: 'c', pos: [C[2], R.c + 90] },
  { id: 'tree', label: 'Tree Navigation', desc: 'LLM structure reasoning', layer: 'c', pos: [C[2], R.c + 180] },
  { id: 'kg', label: 'KG Path', desc: 'Multi-hop traversal', layer: 'c', pos: [C[2], R.c + 270] },
  { id: 'fusion', label: 'RRF Merge', desc: 'Reciprocal rank fusion', layer: 'c', pos: [C[3], R.c + 45] },
  { id: 'expansion', label: 'Context Expansion', desc: 'Descendant \u00b7 sibling \u00b7 xref', layer: 'c', pos: [C[3], R.c + 180] },
  { id: 'rerank', label: 'Rerank', desc: 'LLM relevance scoring', layer: 'c', pos: [C[4], R.c + 110] },

  // (d) Answer Generation — one row
  { id: 'prompt_builder', label: 'Prompt Builder', desc: 'Context + chunks + KG', layer: 'd', pos: [C[1], R.d] },
  { id: 'generator', label: 'LLM Generation', desc: 'Streaming response', layer: 'd', pos: [C[2], R.d] },
  { id: 'citation_builder', label: 'Citation Builder', desc: 'Bbox + page mapping', layer: 'd', pos: [C[3], R.d] },
  { id: 'answer', label: 'Answer', desc: 'Pixel-precise citations', layer: 'd', pos: [C[4], R.d] },
]

const LAYER_COLORS = { a: '#3b82f6', b: '#8b5cf6', c: '#f59e0b', d: '#10b981' }

const layerLabels = [
  { id: 'label_a', label: 'Document Ingestion', pos: [C[0], R.a - 32], color: LAYER_COLORS.a },
  { id: 'label_b', label: 'Persistence', pos: [C[0], R.b - 32], color: LAYER_COLORS.b },
  { id: 'label_c', label: 'Multi-Modal Retrieval', pos: [C[0], R.c - 32], color: LAYER_COLORS.c },
  { id: 'label_d', label: 'Answer Generation', pos: [C[0], R.d - 32], color: LAYER_COLORS.d },
]

const flowNodes = computed(() => {
  const pipeline = nodesDef.map(n => ({
    id: n.id,
    type: 'pipeline',
    position: { x: n.pos[0], y: n.pos[1] },
    data: {
      label: n.label, desc: n.desc, layer: n.layer,
      disabled: TOGGLEABLE.has(n.id) && !isOn(n.id),
      hasConfig: hasConfig(n.id),
    },
  }))
  const labels = layerLabels.map(l => ({
    id: l.id, type: 'label',
    position: { x: l.pos[0], y: l.pos[1] },
    data: { label: l.label, color: l.color },
    selectable: false, draggable: false,
  }))
  return [...pipeline, ...labels]
})

/*
 * Edge definitions: [source, target, sourceHandle?, targetHandle?]
 * Handle positions: 't'=top, 'b'=bottom, 'l'=left, 'r'=right
 * Default: source=right, target=left (horizontal flow)
 * Cross-layer vertical: source=bottom, target=top
 */
const edgesDef = [
  // (a) Ingestion — within layer only
  ['file_upload', 'parser'],
  ['parser', 'chunker'],
  ['chunker', 'tree_builder'],
  ['chunker', 'embedding', 'b', 'l'],
  ['chunker', 'kg_extraction', 'b', 'l'],

  // (c) Retrieval — within layer only
  // QU fans out to all 4 paths
  ['user_query', 'qu'],
  ['qu', 'bm25'],
  ['qu', 'vector'],
  ['qu', 'kg'],
  // BM25 + Vector are pre-filters for Tree Navigation
  ['bm25', 'tree', 'b', 't'],
  ['vector', 'tree', 'b', 't'],
  // Tree + KG merge into RRF
  ['tree', 'fusion'],
  ['kg', 'fusion'],
  // RRF → Expansion → Rerank
  ['fusion', 'expansion', 'b', 't'],
  ['expansion', 'rerank'],

  // (d) Generation — within layer only
  ['prompt_builder', 'generator'],
  ['generator', 'citation_builder'],
  ['citation_builder', 'answer'],
]

const flowEdges = computed(() =>
  edgesDef.map((def, i) => {
    const [s, t, sh, th] = def
    const srcOff = TOGGLEABLE.has(s) && !isOn(s)
    const tgtOff = TOGGLEABLE.has(t) && !isOn(t)
    const dim = srcOff || tgtOff
    const handleMap = { t: Position.Top, b: Position.Bottom, l: Position.Left, r: Position.Right }
    return {
      id: `e${i}`,
      source: s,
      target: t,
      type: 'smoothstep',
      ...(sh ? { sourceHandle: handleMap[sh] } : {}),
      ...(th ? { targetHandle: handleMap[th] } : {}),
      style: dim
        ? { stroke: '#d1d5db', strokeDasharray: '5 5', opacity: 0.35, strokeWidth: 1 }
        : { stroke: '#c0c8d4', strokeWidth: 1.2 },
      markerEnd: { type: 'arrowclosed', color: dim ? '#d1d5db' : '#b0b8c4', width: 12, height: 12 },
    }
  })
)

function onNodeClick({ node }) {
  if (node.type === 'label') return
  activeNode.value = activeNode.value === node.id ? null : node.id
}
</script>

<template>
  <div class="h-full flex bg-bg relative" @dragover="onDragOver" @dragleave="onDragLeave" @drop="onDrop">

    <!-- Loading overlay -->
    <transition name="fade">
      <div v-if="loading" class="absolute inset-0 z-50 flex items-center justify-center bg-bg">
        <div class="flex flex-col items-center gap-3">
          <Spinner size="md" />
          <span class="text-xs text-t3">Loading pipeline configuration...</span>
        </div>
      </div>
    </transition>

    <!-- Left: Vue Flow graph -->
    <div class="flex-1 relative" @click="editProv.show = false">
      <VueFlow
        :nodes="flowNodes"
        :edges="flowEdges"
        :fit-view-on-init="true"
        :nodes-draggable="false"
        :nodes-connectable="false"
        :zoom-on-scroll="true"
        :pan-on-scroll="false"
        :pan-on-drag="true"
        :min-zoom="0.3"
        :max-zoom="2"
        @node-click="onNodeClick"
      >
        <!-- Custom pipeline node -->
        <template #node-pipeline="{ data, id }">
          <div class="pipeline-node"
            :class="{
              'pipeline-node--active': activeNode === id,
              'pipeline-node--disabled': data.disabled,
              'pipeline-node--configurable': data.hasConfig,
            }">
            <Handle type="target" :position="Position.Left" />
            <Handle type="target" :position="Position.Top" />
            <Handle type="source" :position="Position.Right" />
            <Handle type="source" :position="Position.Bottom" />
            <div class="pipeline-node__title">{{ data.label }}</div>
            <div class="pipeline-node__desc">{{ data.desc }}</div>
            <div v-if="data.hasConfig" class="pipeline-node__indicator"></div>
          </div>
        </template>

        <!-- Layer label node -->
        <template #node-label="{ data }">
          <div class="layer-label" :style="{ '--lc': data.color }">
            <span class="layer-label__dot"></span>
            <span>{{ data.label }}</span>
          </div>
        </template>

        <Controls position="bottom-left" />
        <Background :gap="20" :size="0.5" />
      </VueFlow>

      <!-- Apply & Restart + saving indicator -->
      <div class="absolute top-3 right-3 z-10 flex items-center gap-2">
        <transition name="fade">
          <span v-if="saving" class="flex items-center gap-1 text-[10px] text-t3">
            <span class="saving-dot"></span> Saving...
          </span>
        </transition>
        <button @click="restartServer" :disabled="restarting"
          class="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[11px] font-medium transition-colors shadow-sm"
          :class="restarting ? 'bg-amber-100 text-amber-700 cursor-wait' : 'bg-white/90 text-brand hover:bg-white border border-line'">
          <ArrowPathIcon class="w-3.5 h-3.5" :class="restarting ? 'animate-spin' : ''" />
          {{ restarting ? 'Restarting...' : 'Apply & Restart' }}
        </button>
      </div>

      <!-- Drag-drop overlay -->
      <div v-if="dragging" class="absolute inset-0 bg-brand/5 border-2 border-dashed border-brand rounded-lg flex items-center justify-center z-20">
        <span class="text-brand font-medium">Drop documents here to ingest</span>
      </div>

      <!-- Ingestion tasks -->
      <div v-if="tasks.length" class="absolute bottom-12 left-3 z-10 space-y-1">
        <div v-for="t in tasks" :key="t.id" class="flex items-center gap-1.5 px-2 py-1 rounded bg-white/90 border border-line text-[10px] shadow-sm">
          <Spinner v-if="t.s==='run'" size="xs" />
          <span v-else-if="t.s==='ok'" class="text-green-600">done</span>
          <span v-else class="text-red-500">err</span>
          <span class="truncate max-w-32">{{ t.name }}</span>
        </div>
      </div>
    </div>

    <!-- Right: Config side panel -->
    <transition name="slide">
      <div v-if="activeNode" class="side-panel" @click.stop>
        <!-- Header -->
        <div class="side-panel__header">
          <div>
            <div class="text-sm font-medium text-t1">{{ moduleDesc[activeNode]?.title || activeNode }}</div>
            <div class="text-[10px] text-t3 mt-0.5 leading-relaxed">{{ moduleDesc[activeNode]?.desc }}</div>
          </div>
          <button @click="activeNode = null" class="shrink-0 w-6 h-6 flex items-center justify-center rounded hover:bg-bg2 text-t3 hover:text-t1">
            <span class="text-sm">&times;</span>
          </button>
        </div>

        <!-- Toggle (for toggleable nodes) -->
        <div v-if="TOGGLEABLE.has(activeNode)" class="px-4 py-2 border-b border-line flex items-center justify-between">
          <span class="text-[11px] text-t2">Enabled</span>
          <button @click="toggle(activeNode)" class="toggle" :class="isOn(activeNode) ? 'bg-brand' : 'bg-gray-300'">
            <div class="toggle-dot" :style="{ transform: isOn(activeNode) ? 'translateX(13px)' : 'translateX(2px)' }"></div>
          </button>
        </div>

        <!-- Infra info for persistence nodes -->
        <div v-if="isInfraNode(activeNode)" class="px-4 py-3 space-y-3">
          <template v-if="activeNode === 'database'">
            <div><div class="text-[10px] text-t3 mb-0.5">Backend</div><div class="text-xs text-t1">{{ infra.relational_backend }}</div><div class="text-[10px] text-t3">{{ infra.relational_path }}</div></div>
          </template>
          <template v-else-if="activeNode === 'vector_store'">
            <div><div class="text-[10px] text-t3 mb-0.5">Backend</div><div class="text-xs text-t1">{{ infra.vector_backend }}</div><div class="text-[10px] text-t3">{{ infra.vector_detail }}</div></div>
          </template>
          <template v-else-if="activeNode === 'graph_store'">
            <div><div class="text-[10px] text-t3 mb-0.5">Backend</div><div class="text-xs text-t1">{{ infra.graph_backend || 'networkx' }}</div><div class="text-[10px] text-t3">{{ infra.graph_detail }}</div></div>
          </template>
          <div class="text-[9px] text-t3 pt-2 border-t border-line">Configured in forgerag.yaml (restart required)</div>
        </div>

        <!-- Settings form -->
        <div v-if="panelItems.length" class="px-4 py-3 space-y-3 overflow-y-auto flex-1">
          <template v-for="it in panelItems" :key="it.key">
            <div :class="{ 'setting-disabled': isParentDisabled(it) }">
              <div class="text-[11px] mb-1 text-t2">{{ it.label }}</div>

              <!-- Bool toggle -->
              <div v-if="it.value_type==='bool'" class="flex items-center gap-2">
                <button @click="!isParentDisabled(it) && saveSetting(it.key, !it.value_json)"
                  class="toggle" :class="[it.value_json ? 'bg-brand' : 'bg-gray-300', isParentDisabled(it) && 'cursor-not-allowed']"
                  :disabled="isParentDisabled(it)">
                  <div class="toggle-dot" :style="{ transform: it.value_json ? 'translateX(13px)' : 'translateX(2px)' }"></div>
                </button>
                <span class="text-[10px] text-t3">{{ it.value_json ? 'on' : 'off' }}</span>
              </div>

              <!-- Number input -->
              <input v-else-if="it.value_type==='int'||it.value_type==='float'" :value="it.value_json" type="number" :step="it.value_type==='float'?0.1:1"
                class="setting-input" :disabled="isParentDisabled(it)"
                @change="{ const v = it.value_type==='int' ? parseInt($event.target.value) : parseFloat($event.target.value); if (!isNaN(v)) saveSetting(it.key, v) }" />

              <!-- Enum select -->
              <select v-else-if="it.value_type==='enum'" :value="it.value_json" class="setting-input" :disabled="isParentDisabled(it)" @change="saveSetting(it.key, $event.target.value)">
                <option v-for="o in (it.enum_options||[])" :key="o" :value="o">{{ o }}</option>
              </select>

              <!-- Provider select -->
              <template v-else-if="it.key.endsWith('.provider_id')">
                <select :value="it.value_json || ''" class="setting-input" :disabled="isParentDisabled(it)" @change="saveSetting(it.key, $event.target.value || null)">
                  <option value="">-- none --</option>
                  <option v-for="p in providersForKey(it.key)" :key="p.id" :value="p.id">{{ p.name }} ({{ p.model_name }})</option>
                </select>
              </template>

              <!-- Textarea -->
              <div v-else-if="it.value_type==='textarea'" class="relative">
                <textarea :value="it.value_json||''" rows="6" :disabled="isParentDisabled(it)"
                  class="setting-input font-mono leading-relaxed resize-y placeholder:text-t3/60 placeholder:font-sans"
                  :placeholder="it.default_value || ''"
                  @change="saveSetting(it.key, $event.target.value)" />
                <button @click="copyPrompt(it)"
                  class="absolute top-1.5 right-1.5 p-1 rounded text-t3 hover:text-t1 hover:bg-bg3 transition-colors"
                  :title="copiedKey===it.key ? 'Copied!' : 'Copy'">
                  <ClipboardDocumentCheckIcon v-if="copiedKey===it.key" class="w-3.5 h-3.5 text-brand" />
                  <ClipboardDocumentIcon v-else class="w-3.5 h-3.5" />
                </button>
              </div>

              <!-- String input -->
              <input v-else :value="it.value_json" type="text" class="setting-input" :disabled="isParentDisabled(it)" @change="saveSetting(it.key, $event.target.value)" />

              <div v-if="it.description" class="text-[9px] mt-0.5 text-t3">{{ it.description }}</div>
            </div>
          </template>
        </div>

        <!-- No config message -->
        <div v-else-if="!isInfraNode(activeNode)" class="px-4 py-6 text-center text-[11px] text-t3">
          No configurable settings for this node.
        </div>
      </div>
    </transition>

    <!-- LLM Providers floating panel (top-left) -->
    <div class="absolute top-3 left-12 z-10">
      <details class="provider-panel">
        <summary class="text-[10px] text-t2 cursor-pointer select-none px-3 py-1.5 rounded-md bg-white/90 border border-line shadow-sm hover:bg-white">
          LLM Providers ({{ providers.length }})
        </summary>
        <div class="mt-1 p-2 rounded-lg bg-white/95 border border-line shadow-lg backdrop-blur-sm space-y-1.5 max-h-[400px] overflow-y-auto" style="min-width: 200px" @click.stop>
          <div v-for="p in providers" :key="p.id"
            class="px-2 py-1.5 rounded cursor-pointer text-[10px] hover:bg-bg2 transition-colors"
            @click="openEditProvider(p, $event)">
            <div class="text-t1 font-medium truncate">{{ p.name }}</div>
            <div class="text-t3 truncate">{{ p.provider_type }} \u00b7 {{ p.model_name }}</div>
          </div>
          <div v-if="!providers.length" class="text-[9px] text-t3 px-2 py-1">No providers yet</div>

          <div v-if="showNewProvider" class="space-y-1 pt-1 border-t border-line" @click.stop>
            <input v-model="newProv.name" placeholder="Name" class="prov-input" />
            <select v-model="newProv.provider_type" class="prov-input">
              <option value="chat">chat</option><option value="embedding">embedding</option><option value="reranker">reranker</option><option value="vlm">vlm</option>
            </select>
            <input v-model="newProv.model_name" placeholder="Model (e.g. openai/gpt-4o)" class="prov-input" />
            <input v-model="newProv.api_base" placeholder="API Base (optional)" class="prov-input" />
            <input v-model="newProv.api_key" type="password" placeholder="API Key (optional)" class="prov-input" />
            <div class="flex gap-1">
              <button @click="doCreateProvider" class="flex-1 text-[9px] py-1 rounded bg-brand text-white hover:opacity-80">Save</button>
              <button @click="showNewProvider = false" class="flex-1 text-[9px] py-1 rounded border border-line text-t3 hover:bg-bg2">Cancel</button>
            </div>
          </div>
          <button v-else @click.stop="showNewProvider = true" class="w-full text-[10px] text-t3 px-2 py-1 rounded border border-dashed border-line hover:bg-bg2 text-left">+ New provider</button>
        </div>
      </details>
    </div>

    <!-- Edit provider popover -->
    <div v-if="editProv.show" class="popover fadein" :style="{ left: editProv.x + 'px', top: editProv.y + 'px', width: '260px' }" @click.stop>
      <div class="flex items-center justify-between px-3 py-2 border-b border-line">
        <span class="text-xs text-t1 font-medium">Edit Provider</span>
        <button @click="editProv.show = false" class="text-xs text-t3 hover:text-t1">&times;</button>
      </div>
      <div class="px-3 py-3 space-y-2">
        <div><div class="text-[11px] mb-1 text-t2">Name</div><input v-model="editProv.name" class="prov-input" /></div>
        <div><div class="text-[11px] mb-1 text-t2">Type</div>
          <select v-model="editProv.provider_type" class="prov-input"><option value="chat">chat</option><option value="embedding">embedding</option><option value="reranker">reranker</option><option value="vlm">vlm</option></select>
        </div>
        <div><div class="text-[11px] mb-1 text-t2">Model</div><input v-model="editProv.model_name" placeholder="e.g. openai/gpt-4o" class="prov-input" /></div>
        <div><div class="text-[11px] mb-1 text-t2">API Base</div><input v-model="editProv.api_base" placeholder="optional" class="prov-input" /></div>
        <div><div class="text-[11px] mb-1 text-t2">API Key</div><input v-model="editProv.api_key" type="password" placeholder="leave empty to keep unchanged" class="prov-input" /></div>
        <div class="flex gap-1.5 pt-1">
          <button @click="doSaveProvider" class="flex-1 text-[9px] py-1.5 rounded bg-brand text-white hover:opacity-80">Save</button>
          <button @click="doDeleteProvider(editProv.id)" class="w-7 h-7 flex items-center justify-center rounded border border-line text-t3 transition-colors hover:bg-red-500 hover:border-red-500 hover:text-white">
            <TrashIcon class="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── Pipeline nodes ── */
.pipeline-node {
  padding: 8px 14px;
  border-radius: 6px;
  border: 1px solid var(--color-line);
  background: var(--color-bg);
  cursor: pointer;
  transition: all 0.15s;
  min-width: 150px;
  max-width: 170px;
  position: relative;
}
.pipeline-node:hover {
  background: var(--color-bg2);
}
.pipeline-node--active {
  border-color: var(--color-brand);
  box-shadow: 0 0 0 1.5px color-mix(in srgb, var(--color-brand) 25%, transparent);
}
.pipeline-node--disabled {
  opacity: 0.3;
}

.pipeline-node__title {
  font-size: 11px;
  font-weight: 500;
  color: var(--color-t1);
  line-height: 1.3;
}
.pipeline-node__desc {
  font-size: 9px;
  color: var(--color-t3);
  margin-top: 2px;
  line-height: 1.3;
}
.pipeline-node__indicator {
  position: absolute;
  top: 5px;
  right: 5px;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--color-brand);
  opacity: 0.4;
}

/* ── Layer labels ── */
.layer-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 500;
  color: var(--lc);
  letter-spacing: 0.02em;
  user-select: none;
  pointer-events: none;
}
.layer-label__dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: var(--lc);
  opacity: 0.8;
}

/* ── Fade transition ── */
.fade-enter-active { transition: opacity 0.15s ease; }
.fade-leave-active { transition: opacity 0.3s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* ── Saving indicator ── */
.saving-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--color-brand);
  animation: pulse 0.8s ease-in-out infinite;
}
@keyframes pulse { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }

/* ── Side panel ── */
.side-panel {
  width: 380px;
  flex-shrink: 0;
  border-left: 1px solid var(--color-line);
  background: var(--color-bg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.side-panel__header {
  padding: 16px;
  border-bottom: 1px solid var(--color-line);
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

/* ── Slide transition ── */
.slide-enter-active { transition: all 0.2s ease-out; }
.slide-leave-active { transition: all 0.15s ease-in; }
.slide-enter-from { transform: translateX(100%); opacity: 0; }
.slide-leave-to { transform: translateX(100%); opacity: 0; }

/* ── Form inputs ── */
.setting-input {
  width: 100%;
  padding: 6px 10px;
  border-radius: 6px;
  border: 1px solid var(--color-line);
  background: var(--color-bg);
  font-size: 12px;
  color: var(--color-t1);
  outline: none;
  transition: border-color 0.15s;
}
.setting-input:focus {
  border-color: var(--color-brand);
}
.setting-input:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* ── Disabled setting row ── */
.setting-disabled {
  opacity: 0.45;
}

/* ── Toggle ── */
.toggle {
  width: 32px;
  height: 18px;
  border-radius: 9px;
  position: relative;
  cursor: pointer;
  transition: background 0.2s;
  border: none;
  padding: 0;
}
.toggle-dot {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: white;
  position: absolute;
  top: 2px;
  transition: transform 0.2s;
  box-shadow: 0 1px 2px rgba(0,0,0,0.15);
}

/* ── Provider input ── */
.prov-input {
  width: 100%;
  padding: 4px 8px;
  border-radius: 4px;
  border: 1px solid var(--color-line);
  background: var(--color-bg);
  font-size: 10px;
  color: var(--color-t1);
  outline: none;
}
.prov-input:focus { border-color: var(--color-brand); }

/* ── Popover (provider edit) ── */
.popover {
  position: fixed;
  z-index: 50;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 8px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.12);
}
.fadein { animation: fadeIn 0.12s ease-out; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }

/* ── Vue Flow overrides ── */
:deep(.vue-flow__node) {
  border: none !important;
  box-shadow: none !important;
  background: transparent !important;
  padding: 0 !important;
}
:deep(.vue-flow__handle) {
  width: 6px;
  height: 6px;
  background: var(--color-t3);
  border: 1px solid var(--color-bg);
  opacity: 0;
}
:deep(.vue-flow__edge-path) {
  stroke-width: 1.5;
}
:deep(.vue-flow__controls) {
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 6px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.08);
}
:deep(.vue-flow__controls-button) {
  background: var(--color-bg);
  border-color: var(--color-line);
  color: var(--color-t2);
}
:deep(.vue-flow__controls-button:hover) {
  background: var(--color-bg2);
}
:deep(.vue-flow__background) {
  --vf-bg: var(--color-bg);
}
</style>
