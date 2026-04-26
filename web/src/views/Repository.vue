<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { listDocuments, listChunks, listBlocks, getTree, getDocument, getChunk, getChunkByBlock, deleteDocument, reparseDocument, uploadAndIngest, filePreviewUrl, fileDownloadUrl, blockImageUrl } from '@/api'
import { ArrowUpTrayIcon, ArrowPathIcon, TrashIcon, CheckIcon, ExclamationTriangleIcon } from '@heroicons/vue/24/outline'
import Spinner from '@/components/Spinner.vue'
import TreeNode from '@/components/TreeNode.vue'
import PdfViewer from '@/components/PdfViewer.vue'

/**
 * `inline=true` is the "embedded-in-Workspace" mode:
 * - hides the left document list column
 * - hides upload / drag-drop / status tabs (Workspace owns uploads now)
 * - receives the focused doc via `initialDocId` prop
 * - emits `close` when user clicks the back arrow
 *
 * `inline=false` keeps the original standalone Repository page layout for
 * backwards compatibility with any direct /repository links.
 */
const props = defineProps({
  inline: { type: Boolean, default: false },
  initialDocId: { type: String, default: '' },
})
const emit = defineEmits(['close'])

const router = useRouter()
const route = useRoute()

/* ══════════════════════════════════════
   State
   ══════════════════════════════════════ */
const docs = ref([])
const docsTotal = ref(0)
const docsOffset = ref(0)
const docsSearch = ref('')
const docsLoading = ref(false)
const loadingMore = ref(false)
const error = ref('')

const selDoc = ref(null)
const chunks = ref([])
const chunksTotal = ref(0)
const chunksPage = ref(0)
const chunksLoading = ref(false)

const tree = ref(null)
const treeLoading = ref(false)

const selChunkId = ref(null)
const filterNodeId = ref(null)
const expandedChunks = reactive({})

const PAGE_SIZE = 50

/* ── status tabs ── */
const activeTab = ref('all')
const countPending = ref(0)
const countProcessing = ref(0)
const countFailed = ref(0)

const STATUS_FILTERS = {
  all: null,
  ready: 'ready',
  pending: 'pending',
  processing: 'parsing,parsed,structuring,embedding',
  failed: 'error',
}

const tabs = [
  { key: 'all', label: 'All' },
  { key: 'ready', label: 'Ready' },
  { key: 'pending', label: 'Pending', count: countPending },
  { key: 'processing', label: 'Processing', count: countProcessing },
  { key: 'failed', label: 'Failed', count: countFailed },
]

async function loadCounts() {
  try {
    const [p, proc, f] = await Promise.all([
      listDocuments({ limit: 1, offset: 0, status: 'pending' }),
      listDocuments({ limit: 1, offset: 0, status: 'parsing,parsed,structuring,embedding' }),
      listDocuments({ limit: 1, offset: 0, status: 'error' }),
    ])
    countPending.value = p.total
    countProcessing.value = proc.total
    countFailed.value = f.total
  } catch {}
}

/* ── pipeline / chunks toggle ── */
const showPipeline = ref(false)

const isReady = computed(() => selDoc.value?.status === 'ready')
const isInProgress = computed(() => {
  if (!selDoc.value) return false
  return !['ready', 'error'].includes(selDoc.value.status)
})
const isFailed = computed(() => selDoc.value?.status === 'error')

/* ── URL sync ── */
function syncQuery() {
  const q = {}
  if (selDoc.value) q.doc = selDoc.value.doc_id
  if (activeTab.value !== 'all') q.tab = activeTab.value
  if (filterNodeId.value) q.node = filterNodeId.value
  if (selChunkId.value) q.chunk = selChunkId.value
  if (!showPdf.value) q.pdf = '0'
  if (showPipeline.value) q.pipeline = '1'
  router.replace({ query: q })
}

/* ── PDF click hint toast ── */
const pdfClickHint = ref('')
let _hintTimer = null
function showPdfClickHint(msg = 'No chunk here') {
  pdfClickHint.value = msg
  clearTimeout(_hintTimer)
  _hintTimer = setTimeout(() => { pdfClickHint.value = '' }, 1500)
}

/* ── PDF viewer state ── */
const showPdf = ref(true)
watch(showPdf, () => syncQuery())
const pdfPage = ref(1)
const pdfHighlightBlocks = ref([])
const pdfNoScroll = ref(false)
const allBlocks = ref([])

const isPdf = computed(() => {
  const d = selDoc.value
  if (!d) return false
  return d.format === 'pdf' || !!d.pdf_file_id
})

const pdfUrl = computed(() => {
  const d = selDoc.value
  if (!d || !isPdf.value) return ''
  const fid = d.pdf_file_id || d.file_id
  return fid ? filePreviewUrl(fid) : ''
})

const pdfDownloadUrl = computed(() => {
  const d = selDoc.value
  if (!d) return ''
  const fid = d.pdf_file_id || d.file_id
  return fid ? fileDownloadUrl(fid) : ''
})

const sourceDownloadUrl = computed(() => {
  const d = selDoc.value
  if (!d || !d.pdf_file_id || !d.file_id) return ''
  return fileDownloadUrl(d.file_id)
})

const sourceLabel = computed(() => {
  const d = selDoc.value
  if (!d?.pdf_file_id) return ''
  const name = d.file_name || d.filename || ''
  const ext = name.split('.').pop()?.toUpperCase()
  return ext || d.format?.toUpperCase() || 'Source'
})

async function loadBlocks() {
  if (!selDoc.value) { allBlocks.value = []; return }
  try {
    // Load all blocks (paginate if > 2000) for bbox highlighting
    const all = []
    let offset = 0
    const PAGE = 2000
    while (true) {
      const r = await listBlocks(selDoc.value.doc_id, { limit: PAGE, offset })
      all.push(...(r.items || []))
      if (all.length >= r.total || (r.items || []).length < PAGE) break
      offset += PAGE
    }
    allBlocks.value = all
  } catch { allBlocks.value = [] }
}

function highlightChunkBlocks(c, { skipScroll = false } = {}) {
  if (!c?.block_ids?.length) {
    pdfHighlightBlocks.value = []
    if (!skipScroll && c?.page_start) pdfPage.value = c.page_start
    return
  }
  const blockMap = new Map(allBlocks.value.map(b => [b.block_id, b]))
  const bidSet = new Set(c.block_ids)
  const highlights = []
  for (const bid of c.block_ids) {
    const b = blockMap.get(bid)
    if (b?.bbox) highlights.push({ page_no: b.page_no, bbox: b.bbox })
  }
  // Also highlight blocks that were merged into one of the chunk's blocks
  // (excluded with reason "merged_into:<block_id>")
  for (const b of allBlocks.value) {
    if (!b.excluded || !b.excluded_reason?.startsWith('merged_into:')) continue
    const targetBid = b.excluded_reason.slice('merged_into:'.length)
    if (bidSet.has(targetBid) && b.bbox) {
      highlights.push({ page_no: b.page_no, bbox: b.bbox })
    }
  }
  pdfHighlightBlocks.value = highlights
  if (!skipScroll) {
    if (highlights.length) pdfPage.value = highlights[0].page_no
    else if (c.page_start) pdfPage.value = c.page_start
  }
}

function highlightNodeBlocks(nodeId) {
  if (!tree.value?.nodes) { pdfHighlightBlocks.value = []; return }
  const node = tree.value.nodes[nodeId]
  if (!node?.block_ids?.length) {
    if (node?.page_start) pdfPage.value = node.page_start
    pdfHighlightBlocks.value = []
    return
  }
  const blockMap = new Map(allBlocks.value.map(b => [b.block_id, b]))
  const bidSet = new Set(node.block_ids)
  const highlights = []
  for (const bid of node.block_ids) {
    const b = blockMap.get(bid)
    if (b?.bbox) highlights.push({ page_no: b.page_no, bbox: b.bbox })
  }
  for (const b of allBlocks.value) {
    if (!b.excluded || !b.excluded_reason?.startsWith('merged_into:')) continue
    const targetBid = b.excluded_reason.slice('merged_into:'.length)
    if (bidSet.has(targetBid) && b.bbox) {
      highlights.push({ page_no: b.page_no, bbox: b.bbox })
    }
  }
  pdfHighlightBlocks.value = highlights
  if (highlights.length) pdfPage.value = highlights[0].page_no
  else if (node.page_start) pdfPage.value = node.page_start
}

/* ══════════════════════════════════════
   Load documents
   ══════════════════════════════════════ */
async function loadDocs({ append = false, silent = false } = {}) {
  if (!append) {
    if (!silent) docsLoading.value = true
    docsOffset.value = 0
  } else {
    loadingMore.value = true
  }
  try {
    const params = { limit: PAGE_SIZE, offset: append ? docsOffset.value : 0 }
    const statusFilter = STATUS_FILTERS[activeTab.value]
    if (statusFilter) params.status = statusFilter
    if (docsSearch.value) params.search = docsSearch.value
    const r = await listDocuments(params)
    if (append) {
      docs.value = [...docs.value, ...r.items]
    } else {
      docs.value = r.items
    }
    docsTotal.value = r.total
    docsOffset.value = (append ? docsOffset.value : 0) + r.items.length
  } catch (e) { error.value = e?.message || 'Failed to load documents' }
  docsLoading.value = false
  loadingMore.value = false
}

/* ── search ── */
let searchTimer = null
function isIdLike(q) { return /^doc_|:/.test(q) }

function onSearch() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(async () => {
    const q = docsSearch.value.trim()
    docsOffset.value = 0

    if (q && isIdLike(q)) {
      // ID-like query: skip doc-name search, go straight to direct match
      await tryDirectMatch(q)
    } else {
      // Text search: search by file name
      await loadDocs()
    }
  }, 300)
}

function ensureDocVisible(doc) {
  // Put doc into the current list (at top) so it's visible in the left panel
  if (!doc?.doc_id) return
  const idx = docs.value.findIndex(d => d.doc_id === doc.doc_id)
  if (idx === -1) {
    docs.value = [doc, ...docs.value]
    docsTotal.value++
  }
}

async function tryDirectMatch(q) {
  if (!q) return

  // 1. Doc ID exact match (local)
  let localDoc = docs.value.find(d => d.doc_id === q)
  if (localDoc) { await selectDoc(localDoc); return }

  // 2. Doc ID exact match (API)
  try {
    const doc = await getDocument(q)
    if (doc?.doc_id) { ensureDocVisible(doc); await selectDoc(doc); return }
  } catch {}

  // 3. Node ID match in current tree
  if (tree.value?.nodes?.[q]) { onClickTreeNode(q); return }

  // 4. Chunk ID match (local)
  const localChunk = chunks.value.find(c => c.chunk_id === q)
  if (localChunk) { onClickChunk(localChunk); return }

  // 5. Chunk ID match (API) — extract doc_id and load doc first
  try {
    const chunk = await getChunk(q)
    if (chunk?.chunk_id) {
      const docId = chunk.doc_id
      let doc = docs.value.find(d => d.doc_id === docId)
      if (!doc) { try { doc = await getDocument(docId) } catch {} }
      if (doc) {
        ensureDocVisible(doc)
        await selectDoc(doc)
        // Now chunks are loaded — find, highlight, and scroll to the target chunk
        const c = chunks.value.find(c => c.chunk_id === chunk.chunk_id)
        if (c) { onClickChunk(c); scrollToChunk(c.chunk_id) }
      }
    }
  } catch {}
}

/* ── tab change ── */
function switchTab(key) {
  activeTab.value = key
  selDoc.value = null
  docsSearch.value = ''
  loadDocs()
  syncQuery()
}

/* ── infinite scroll ── */
const hasMore = computed(() => docsOffset.value < docsTotal.value)
function onListScroll(e) {
  const el = e.target
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 60 && hasMore.value && !loadingMore.value) {
    loadDocs({ append: true })
  }
}

/* ══════════════════════════════════════
   Select document & load detail
   ══════════════════════════════════════ */
async function selectDoc(doc) {
  selDoc.value = doc
  selChunkId.value = null
  filterNodeId.value = null
  chunksPage.value = 0
  pdfPage.value = 1
  pdfHighlightBlocks.value = []
  Object.keys(expandedChunks).forEach(k => delete expandedChunks[k])

  // Auto-decide which view to show
  showPipeline.value = doc.status !== 'ready'

  await Promise.all([loadChunks(), loadTree(), loadBlocks()])
  syncQuery()
}

async function refreshDetail() {
  if (!selDoc.value) return
  try {
    selDoc.value = await getDocument(selDoc.value.doc_id)
  } catch {}
}

/* ── load chunks ── */
async function loadChunks() {
  if (!selDoc.value) return
  chunksLoading.value = true
  try {
    if (filterNodeId.value) {
      // When filtering by tree node, load ALL chunks (paginated)
      // so displayChunks can filter correctly.
      const all = []
      let off = 0
      const BATCH = 500
      while (true) {
        const r = await listChunks(selDoc.value.doc_id, { limit: BATCH, offset: off })
        all.push(...(r.items || []))
        chunksTotal.value = r.total
        if (all.length >= r.total || (r.items || []).length < BATCH) break
        off += BATCH
      }
      chunks.value = all
    } else {
      const r = await listChunks(selDoc.value.doc_id, { limit: PAGE_SIZE, offset: chunksPage.value * PAGE_SIZE })
      chunks.value = r.items
      chunksTotal.value = r.total
    }
  } catch {}
  chunksLoading.value = false
}

// When tree node selection changes, reload chunks (may need full set for filtering)
watch(filterNodeId, () => { loadChunks() })

/* ── load tree ── */
async function loadTree() {
  if (!selDoc.value) return
  treeLoading.value = true
  try {
    tree.value = await getTree(selDoc.value.doc_id)
  } catch { tree.value = null }
  treeLoading.value = false
}

/* ── filtered chunks ── */
const displayChunks = computed(() => {
  if (!filterNodeId.value) return chunks.value
  return chunks.value.filter(c =>
    c.node_id === filterNodeId.value ||
    (c.ancestor_node_ids && c.ancestor_node_ids.includes(filterNodeId.value))
  )
})

/* ── highlight node set ── */
const highlightNodeIds = computed(() => {
  const s = new Set()
  if (selChunkId.value) {
    const c = chunks.value.find(x => x.chunk_id === selChunkId.value)
    if (c) {
      (c.ancestor_node_ids || []).forEach(id => s.add(id))
      s.add(c.node_id)
    }
  }
  if (filterNodeId.value && tree.value?.nodes) {
    s.add(filterNodeId.value)
    let cur = tree.value.nodes[filterNodeId.value]
    while (cur) {
      const parent = Object.values(tree.value.nodes).find(
        n => n.children && n.children.includes(cur.node_id)
      )
      if (parent) { s.add(parent.node_id); cur = parent } else { cur = null }
    }
  }
  return s
})

/* ── breadcrumb ── */
const docName = computed(() => {
  if (!selDoc.value) return ''
  return selDoc.value.file_name || selDoc.value.filename || selDoc.value.doc_id
})

function getNodePath(nodeId) {
  if (!tree.value || !tree.value.nodes) return []
  const path = []
  let cur = tree.value.nodes[nodeId]
  while (cur) {
    path.unshift(cur.title || cur.node_id)
    const parentEntry = Object.values(tree.value.nodes).find(
      n => n.children && n.children.includes(cur.node_id)
    )
    cur = parentEntry || null
  }
  return path
}

const breadcrumb = computed(() => {
  if (selChunkId.value) {
    const c = chunks.value.find(x => x.chunk_id === selChunkId.value)
    const label = c ? c.chunk_id.slice(0, 12) : '?'
    if (c && c.section_path?.length) return [...c.section_path, label]
    if (c) return [docName.value, label]
  }
  if (filterNodeId.value) {
    const path = getNodePath(filterNodeId.value)
    if (path.length) path[0] = docName.value
    return path
  }
  return selDoc.value ? [docName.value] : []
})

/* ── tree expand/collapse ── */
const expanded = reactive({})
function toggleNode(nodeId) { expanded[nodeId] = !expanded[nodeId] }

function onClickTreeNode(nodeId) {
  // If in pipeline view, switch to chunks first
  if (showPipeline.value && isReady.value) showPipeline.value = false

  if (filterNodeId.value === nodeId) {
    filterNodeId.value = null
    selChunkId.value = null
    pdfHighlightBlocks.value = []
  } else {
    filterNodeId.value = nodeId
    selChunkId.value = null
    if (tree.value?.nodes) {
      const path = []
      let cur = tree.value.nodes[nodeId]
      while (cur) {
        path.push(cur.node_id)
        const parent = Object.values(tree.value.nodes).find(
          n => n.children && n.children.includes(cur.node_id)
        )
        cur = parent || null
      }
      path.forEach(id => { expanded[id] = true })
    }
    if (showPdf.value) highlightNodeBlocks(nodeId)
  }
  syncQuery()
}

const chunkRefs = {}
function setChunkRef(id, el) { if (el) chunkRefs[id] = el; else delete chunkRefs[id] }

function scrollToChunk(chunkId) {
  // Double nextTick + rAF: selectDoc sets data → nextTick flushes v-for render
  // → rAF ensures paint is done and refs are registered
  nextTick(() => {
    requestAnimationFrame(() => {
      const el = chunkRefs[chunkId]
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  })
}

function onClickChunk(c) {
  if (selChunkId.value === c.chunk_id) {
    selChunkId.value = null
    pdfHighlightBlocks.value = []
  } else {
    selChunkId.value = c.chunk_id
    if (tree.value?.nodes) {
      const ids = [...(c.ancestor_node_ids || []), c.node_id]
      ids.forEach(id => { expanded[id] = true })
    }
    if (showPdf.value) highlightChunkBlocks(c)
  }
  syncQuery()
}

/** Click on PDF → reverse-select chunk whose block bbox contains the point */
async function onPdfClick({ page_no, x, y }) {
  if (!allBlocks.value.length || !selDoc.value) return
  // Find the block whose bbox contains the click point
  // bbox is in PDF coords (origin bottom-left)
  let hitBlock = null
  for (const b of allBlocks.value) {
    if (b.page_no !== page_no || !b.bbox) continue
    const { x0, y0, x1, y1 } = b.bbox
    const minX = Math.min(x0, x1), maxX = Math.max(x0, x1)
    const minY = Math.min(y0, y1), maxY = Math.max(y0, y1)
    if (x >= minX && x <= maxX && y >= minY && y <= maxY) {
      hitBlock = b
      break
    }
  }
  if (!hitBlock) {
    showPdfClickHint()
    return
  }

  // If this block was merged into another block, follow the pointer
  let bid = hitBlock.block_id
  if (hitBlock.excluded && hitBlock.excluded_reason?.startsWith('merged_into:')) {
    bid = hitBlock.excluded_reason.slice('merged_into:'.length)
  }

  // Try current page chunks first (fast, no network)
  let target = chunks.value.find(c => c.block_ids?.includes(bid))
  let needPageJump = false

  // Cross-page fallback: query backend by block_id → chunk + position
  if (!target) {
    try {
      const resp = await getChunkByBlock(bid, selDoc.value.doc_id)
      target = resp.chunk
      // Jump chunk list to the correct page
      if (target && resp.position >= 0) {
        const targetPage = Math.floor(resp.position / PAGE_SIZE)
        if (targetPage !== chunksPage.value) {
          chunksPage.value = targetPage
          await loadChunks()
          needPageJump = true
        }
      }
    } catch {
      showPdfClickHint(hitBlock.excluded ? 'Header/footer — no chunk' : 'No chunk here')
      return
    }
  }
  if (!target) {
    showPdfClickHint(hitBlock.excluded ? 'Header/footer — no chunk' : 'No chunk here')
    return
  }

  // Select the chunk and highlight
  selChunkId.value = target.chunk_id
  if (tree.value?.nodes) {
    const ids = [...(target.ancestor_node_ids || []), target.node_id]
    ids.forEach(id => { expanded[id] = true })
  }
  pdfNoScroll.value = true
  highlightChunkBlocks(target, { skipScroll: true })
  syncQuery()
  // Reset after highlights are applied
  nextTick(() => { pdfNoScroll.value = false })

  // Scroll chunk list to the selected chunk
  scrollToChunk(target.chunk_id)
}

function onBreadcrumbClick(idx) {
  if (idx === 0 && breadcrumb.value.length > 0) {
    filterNodeId.value = null
    selChunkId.value = null
    pdfPage.value = 1
    pdfHighlightBlocks.value = []
    return
  }
  if (filterNodeId.value) {
    const path = getNodePath(filterNodeId.value)
    if (idx < path.length) {
      let cur = tree.value.nodes[filterNodeId.value]
      const nodePath = []
      while (cur) {
        nodePath.unshift(cur.node_id)
        const parent = Object.values(tree.value.nodes).find(
          n => n.children && n.children.includes(cur.node_id)
        )
        cur = parent || null
      }
      if (idx < nodePath.length) onClickTreeNode(nodePath[idx])
    }
  }
}

/* ── chunk expand/collapse ── */
function toggleChunk(chunkId) {
  expandedChunks[chunkId] = !expandedChunks[chunkId]
}

function chunkImageUrls(c) {
  if (c.content_type !== 'figure' || !c.block_ids?.length) return []
  return c.block_ids.map(bid => blockImageUrl(bid))
}

/* ── pagination ── */
const chunksTotalPages = computed(() => Math.ceil(chunksTotal.value / PAGE_SIZE))
function prevChunksPage() { if (chunksPage.value > 0) { chunksPage.value--; loadChunks() } }
function nextChunksPage() { if (chunksPage.value < chunksTotalPages.value - 1) { chunksPage.value++; loadChunks() } }

/* ══════════════════════════════════════
   Document actions (from Ingestion)
   ══════════════════════════════════════ */
async function doRetry() {
  if (!selDoc.value) return
  try {
    await reparseDocument(selDoc.value.doc_id)
    showPipeline.value = true
    await loadDocs({ silent: true })
    await refreshDetail()
    loadCounts()
    startPoll()
  } catch (e) { error.value = e?.message || 'Retry failed' }
}

async function doDelete() {
  if (!selDoc.value) return
  try {
    await deleteDocument(selDoc.value.doc_id)
    selDoc.value = null
    syncQuery()
    await loadDocs()
    loadCounts()
  } catch (e) { error.value = e?.message || 'Delete failed' }
}

async function doRetryAllFailed() {
  try {
    const r = await listDocuments({ limit: 200, offset: 0, status: 'error' })
    const failed = r.items || []
    for (const d of failed) {
      try { await reparseDocument(d.doc_id) } catch {}
    }
    await loadDocs()
    loadCounts()
    startPoll()
  } catch (e) { error.value = e?.message || 'Retry all failed' }
}

/* ══════════════════════════════════════
   Format helpers
   ══════════════════════════════════════ */
function fmtDate(d) { if (!d) return ''; return new Date(d).toLocaleDateString() }
function fmtSize(bytes) {
  if (!bytes) return ''
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1048576).toFixed(1) + ' MB'
}

function fmtAgo(ts) {
  if (!ts) return ''
  const diff = Date.now() - new Date(ts).getTime()
  if (diff < 60000) return 'just now'
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`
  return `${Math.floor(diff / 86400000)}d ago`
}

function fmtTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts)
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function fmtDurationSec(startTs, endTs) {
  if (!startTs || !endTs) return null
  const ms = new Date(endTs) - new Date(startTs)
  return (ms / 1000).toFixed(1) + 's'
}

function docStatusType(st) {
  if (!st || st === 'pending') return 'pending'
  if (st === 'ready') return 'ready'
  if (st === 'error') return 'error'
  return 'processing'
}

function displayStatus(st) {
  if (st === 'error') return 'failed'
  return st || 'pending'
}

/* ══════════════════════════════════════
   Upload
   ══════════════════════════════════════ */
const dragging = ref(false)
const uploadTasks = ref([])
const fileInput = ref(null)

const ACCEPT_STR = '.pdf,.docx,.pptx,.xlsx,.html,.htm,.md,.markdown,.txt'
const ACCEPT_RE = /\.(pdf|docx?|pptx?|xlsx?|html?|md|markdown|txt|png|jpe?g|tiff?)$/i

function onDragOver(e) { e.preventDefault(); dragging.value = true }
function onDragLeave(e) { if (!e.currentTarget.contains(e.relatedTarget)) dragging.value = false }

async function uploadFiles(files) {
  const valid = files.filter(f => ACCEPT_RE.test(f.name))
  if (!valid.length) return
  for (const file of valid) {
    const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 4)
    uploadTasks.value.push({ id, name: file.name, s: 'run' })
    try {
      await uploadAndIngest(file)
      uploadTasks.value = uploadTasks.value.map(t => t.id === id ? { ...t, s: 'ok' } : t)
    } catch (e) {
      uploadTasks.value = uploadTasks.value.map(t => t.id === id ? { ...t, s: 'err' } : t)
      error.value = e?.message || 'Upload failed'
    }
    await loadDocs({ silent: true })
    loadCounts()
  }
  setTimeout(() => { uploadTasks.value = uploadTasks.value.filter(t => t.s !== 'ok') }, 2000)
  startPoll()
}

async function onDrop(e) {
  e.preventDefault(); dragging.value = false
  await uploadFiles(Array.from(e.dataTransfer?.files || []))
}

function openFilePicker() { fileInput.value?.click() }
async function onFileSelected(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  await uploadFiles(files)
}

/* ══════════════════════════════════════
   Polling — refresh list + detail while processing
   ══════════════════════════════════════ */
let pollTimer = null
function startPoll() {
  if (pollTimer) return
  pollTimer = setInterval(async () => {
    if (!props.inline) {
      await loadDocs({ silent: true })
      loadCounts()
    }
    if (selDoc.value) await refreshDetail()
    // Terminate condition differs: inline only cares about the selected doc's
    // status; standalone looks at the whole list.
    if (props.inline) {
      const st = selDoc.value?.status
      if (!st || ['ready', 'error'].includes(st)) {
        clearInterval(pollTimer); pollTimer = null
      }
    } else {
      const hasActive = docs.value.some(d =>
        d.status && !['ready', 'error'].includes(d.status)
      )
      if (!hasActive) { clearInterval(pollTimer); pollTimer = null }
    }
  }, 3000)
}

watch(docs, (list) => {
  const hasActive = list.some(d => d.status && !['ready', 'error'].includes(d.status))
  if (hasActive) startPoll()
}, { immediate: true })

// In inline mode, start polling when we pick a doc that isn't yet terminal.
watch(() => selDoc.value?.status, (st) => {
  if (!props.inline || !st) return
  if (!['ready', 'error'].includes(st)) startPoll()
})

// Auto-switch from pipeline to chunks when processing completes
watch(() => selDoc.value?.status, (st, prev) => {
  if (st === 'ready' && prev && prev !== 'ready' && showPipeline.value) {
    // Reload chunks/tree now that doc is ready, then switch view
    Promise.all([loadChunks(), loadTree(), loadBlocks()]).then(() => {
      showPipeline.value = false
    })
  }
})

onUnmounted(() => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } })

/* ══════════════════════════════════════
   Mount — restore from URL
   ══════════════════════════════════════ */
onMounted(async () => {
  const q = route.query
  if (q.pdf === '0') showPdf.value = false
  if (q.pipeline === '1') showPipeline.value = true

  if (props.inline) {
    // Embedded in Workspace: no doc-list, select by prop directly.
    const docId = props.initialDocId || q.doc
    if (docId) {
      try {
        const d = await getDocument(docId)
        if (d) await selectDoc(d)
        if (q.pipeline === '1') showPipeline.value = true
        if (q.node && tree.value?.nodes?.[q.node]) onClickTreeNode(q.node)
        if (q.chunk) {
          const c = chunks.value.find(c => c.chunk_id === q.chunk)
          if (c) onClickChunk(c)
        }
      } catch (e) {
        error.value = e?.message || 'Failed to load document'
      }
    }
    return
  }

  // Standalone mode — load full doc list + tabs
  if (q.tab && STATUS_FILTERS[q.tab] !== undefined) activeTab.value = q.tab
  await loadDocs()
  loadCounts()

  if (q.doc) {
    const doc = docs.value.find(d => d.doc_id === q.doc)
    if (doc) {
      await selectDoc(doc)
      if (q.pipeline === '1') showPipeline.value = true
      if (q.node && tree.value?.nodes?.[q.node]) onClickTreeNode(q.node)
      if (q.chunk) {
        const c = chunks.value.find(c => c.chunk_id === q.chunk)
        if (c) onClickChunk(c)
      }
    }
  }
})

// React to initialDocId changing while embedded (e.g., user clicks a
// different doc in Workspace without navigating away).
watch(() => props.initialDocId, async (docId) => {
  if (!props.inline || !docId) return
  if (selDoc.value?.doc_id === docId) return
  try {
    const d = await getDocument(docId)
    if (d) await selectDoc(d)
  } catch (e) {
    error.value = e?.message || 'Failed to load document'
  }
})

/* ══════════════════════════════════════
   Pipeline steps (from Ingestion)
   ══════════════════════════════════════ */
function _afterPhase(d, ...phases) {
  const checks = {
    parse:     () => d.parse_completed_at,
    structure: () => d.structure_completed_at,
    enrich:    () => d.enrich_status && d.enrich_status !== 'pending',
    kg:        () => d.kg_status && d.kg_status !== 'pending' && d.kg_status !== null,
    chunk:     () => d.kg_started_at || d.kg_status || d.embed_started_at || ['embedding', 'ready'].includes(d.status),
    embed:     () => d.embed_at || d.status === 'ready',
  }
  return phases.some(p => checks[p]?.())
}

function pipelineSteps(d) {
  if (!d) return []
  const isFinished = d.status === 'ready'
  const isError = d.status === 'error'

  const upload = {
    type: 'step', label: 'Upload', status: 'done',
    data: [d.file_name || d.filename, d.format?.toUpperCase(), fmtSize(d.file_size_bytes)].filter(Boolean).join(' · '),
    startTime: d.created_at, endTime: d.parse_started_at || null,
    duration: fmtDurationSec(d.created_at, d.parse_started_at),
  }

  const needsConvert = d.format !== 'pdf' && d.format !== 'image'
  let convertStep = null
  if (needsConvert) {
    let cs = 'pending'
    if (d.pdf_file_id || d.parse_started_at || _afterPhase(d, 'structure', 'enrich', 'embed')) cs = 'done'
    else if (d.status === 'converting') cs = 'running'
    else if (isError && !d.parse_started_at) cs = 'error'
    convertStep = { type: 'step', label: 'Converting to PDF', status: cs, data: d.pdf_file_id ? 'PDF ready' : null }
  }

  let ps = 'pending'
  if (d.parse_completed_at || _afterPhase(d, 'structure', 'enrich', 'embed')) ps = 'done'
  else if (d.status === 'parsing') ps = 'running'
  else if (isError && d.parse_started_at && !d.parse_completed_at) ps = 'error'
  const parsing = { type: 'step', label: 'Parsing', status: ps, data: d.num_blocks ? `${d.num_blocks} blocks` : null, startTime: d.parse_started_at, endTime: d.parse_completed_at, duration: fmtDurationSec(d.parse_started_at, d.parse_completed_at) }

  let ss = 'pending'
  if (d.structure_completed_at || _afterPhase(d, 'enrich', 'embed')) ss = 'done'
  else if (d.status === 'structuring' && !d.structure_completed_at) ss = 'running'
  else if (isError && d.structure_started_at && !d.structure_completed_at) ss = 'error'
  const structData = []
  if (d.tree_method) structData.push(d.tree_method)
  if (d.tree_quality != null) structData.push(`quality ${d.tree_quality.toFixed(2)}`)
  if (d.tree_navigable === true) structData.push('navigable')
  else if (d.tree_navigable === false) structData.push('not navigable')
  const structuring = { type: 'step', label: 'Structuring', status: ss, data: structData.length ? structData.join(' · ') : null, startTime: d.structure_started_at, endTime: d.structure_completed_at, duration: fmtDurationSec(d.structure_started_at, d.structure_completed_at) }

  // Enrichment
  let es = 'pending'
  if (d.enrich_status === 'done' || d.enrich_status === 'partial') es = 'done'
  else if (d.enrich_status === 'skipped') es = 'skipped'
  else if (d.enrich_status === 'running') es = 'running'
  else if (isError && d.enrich_started_at && !d.enrich_at) es = 'error'
  else if (_afterPhase(d, 'chunk', 'embed')) es = d.enrich_status === 'skipped' ? 'skipped' : 'done'
  const enrichData = []
  if (d.enrich_summary_count) enrichData.push(`${d.enrich_summary_count} summaries`)
  if (d.enrich_image_count) enrichData.push(`${d.enrich_image_count} images`)
  if (d.enrich_model) enrichData.push(d.enrich_model)
  const enrichment = { type: 'step', label: 'Enrichment', status: es, data: enrichData.length ? enrichData.join(' · ') : (es === 'skipped' ? 'skipped' : null), startTime: d.enrich_started_at, endTime: d.enrich_at, duration: fmtDurationSec(d.enrich_started_at, d.enrich_at) }

  // Chunking
  let cks = 'pending'
  if (_afterPhase(d, 'chunk')) cks = 'done'
  else if ((d.enrich_status === 'done' || d.enrich_status === 'skipped' || d.enrich_status === 'partial') && !_afterPhase(d, 'kg', 'embed')) cks = 'running'
  else if (isError && _afterPhase(d, 'enrich') && !_afterPhase(d, 'chunk')) cks = 'error'
  const chunking = { type: 'step', label: 'Chunking', status: cks, data: d.num_chunks ? `${d.num_chunks} chunks` : null }

  // KG Extraction
  let ks = 'pending'
  if (d.kg_status === 'done') ks = 'done'
  else if (d.kg_status === 'skipped') ks = 'skipped'
  else if (d.kg_status === 'running') ks = 'running'
  else if (d.kg_status === 'error') ks = 'error'
  else if (isError && d.kg_started_at && !d.kg_completed_at) ks = 'error'
  else if (_afterPhase(d, 'embed')) ks = 'skipped'
  const kgData = []
  if (d.kg_entity_count) kgData.push(`${d.kg_entity_count} entities`)
  if (d.kg_relation_count) kgData.push(`${d.kg_relation_count} relations`)
  if (d.kg_model) kgData.push(d.kg_model)
  const kgExtraction = { type: 'step', label: 'KG Extraction', status: ks, data: kgData.length ? kgData.join(' · ') : (ks === 'skipped' ? 'skipped' : null), startTime: d.kg_started_at, endTime: d.kg_completed_at, duration: fmtDurationSec(d.kg_started_at, d.kg_completed_at) }

  const children = [enrichment, chunking, kgExtraction]
  let gs = 'pending'
  if (children.some(c => c.status === 'running')) gs = 'running'
  else if (children.some(c => c.status === 'error')) gs = 'error'
  else if (children.every(c => c.status === 'done' || c.status === 'skipped')) gs = children.some(c => c.status === 'done') ? 'done' : 'skipped'
  const processing = { type: 'group', label: 'Processing', status: gs, children }

  // Embedding
  let ebs = 'pending'
  if (d.embed_status === 'done' || isFinished) ebs = 'done'
  else if (d.embed_status === 'running' || d.status === 'embedding') ebs = 'running'
  else if (isError && d.embed_started_at && !d.embed_at) ebs = 'error'
  const ebData = []
  if (d.num_chunks) ebData.push(`${d.num_chunks} chunks`)
  if (d.embed_model) ebData.push(d.embed_model)
  const embedding = { type: 'step', label: 'Embedding', status: ebs, data: ebData.length ? ebData.join(' · ') : null, startTime: d.embed_started_at, endTime: d.embed_at, duration: fmtDurationSec(d.embed_started_at, d.embed_at) }

  const steps = [upload]
  if (convertStep) steps.push(convertStep)
  steps.push(parsing, structuring, processing, embedding)
  return steps
}

const steps = computed(() => pipelineSteps(selDoc.value))

const totalDuration = computed(() => {
  if (!selDoc.value) return null
  const d = selDoc.value
  const end = d.embed_at || d.kg_completed_at || d.enrich_at || d.structure_completed_at || d.parse_completed_at || d.updated_at
  return fmtDurationSec(d.created_at, end)
})
</script>

<template>
  <div class="h-full flex bg-bg2 relative"
    @dragover="inline ? null : onDragOver($event)"
    @dragleave="inline ? null : onDragLeave($event)"
    @drop="inline ? null : onDrop($event)">

    <!-- Hidden file input (standalone-only) -->
    <input v-if="!inline" ref="fileInput" type="file" :accept="ACCEPT_STR" multiple class="hidden" @change="onFileSelected" />

    <!-- Drag overlay (standalone-only; inline mode uses Workspace's upload) -->
    <Transition name="fade">
      <div v-if="!inline && dragging" class="absolute inset-0 z-50 flex items-center justify-center bg-bg/80 backdrop-blur-sm border-2 border-dashed border-brand rounded-lg pointer-events-none">
        <div class="flex flex-col items-center gap-2">
          <ArrowUpTrayIcon class="w-8 h-8 text-brand" />
          <span class="text-sm text-brand font-medium">Drop files to upload</span>
          <span class="text-[10px] text-t3">PDF, DOCX, PPTX, XLSX, HTML, Markdown, TXT, Images</span>
        </div>
      </div>
    </Transition>

    <!-- Upload tasks toast (standalone-only; inline mode uses global upload panel) -->
    <div v-if="!inline && uploadTasks.length" class="absolute top-3 right-3 z-40 space-y-1.5">
      <div v-for="t in uploadTasks" :key="t.id"
        class="flex items-center gap-2 px-3 py-1.5 rounded-md text-[10px] shadow-sm border border-line bg-bg">
        <Spinner v-if="t.s === 'run'" size="xs" />
        <span v-else-if="t.s === 'ok'" class="w-1.5 h-1.5 rounded-full bg-green-500"></span>
        <span v-else class="w-1.5 h-1.5 rounded-full bg-red-500"></span>
        <span class="text-t2 truncate max-w-[180px]">{{ t.name }}</span>
        <span class="text-t3">{{ t.s === 'run' ? 'uploading...' : t.s === 'ok' ? 'queued' : 'failed' }}</span>
      </div>
    </div>

    <!-- Error banner -->
    <div v-if="error" class="text-xs px-4 py-2 absolute top-0 left-0 right-0 z-30 flex items-center justify-between"
         style="color: var(--color-err-fg); background: var(--color-err-bg);">
      <span>{{ error }}</span>
      <button @click="error = ''" class="ml-2 opacity-70 hover:opacity-100">&#x2715;</button>
    </div>

    <!-- ═══════════════════════════════════
         COL 1: Document list (hidden in inline mode — Workspace is the list)
         ═══════════════════════════════════ -->
    <div v-if="!inline" class="shrink-0 flex flex-col border-r border-line transition-[width] duration-200"
         :class="selDoc && showPdf && isPdf ? 'w-48' : 'w-64'">
      <div class="px-3 pt-4 pb-2">
        <div class="flex items-center justify-between mb-2">
          <div class="text-[10px] text-t3 uppercase tracking-widest">Documents</div>
          <button v-if="activeTab === 'failed' && countFailed > 0"
            @click="doRetryAllFailed"
            class="flex items-center gap-1 text-[9px] text-t3 hover:text-t1 transition-colors">
            Retry all
            <ArrowPathIcon class="w-3 h-3" />
          </button>
        </div>
        <input
          v-model="docsSearch"
          @input="onSearch"
          type="text"
          placeholder="File name, doc ID, chunk ID..."
          class="w-full px-2.5 py-1.5 rounded-md border border-line bg-bg text-xs text-t1 outline-none focus:border-brand transition-colors mb-2"
        />
        <!-- Status tabs -->
        <div class="flex gap-1 flex-wrap">
          <button
            v-for="t in tabs" :key="t.key"
            @click="switchTab(t.key)"
            class="px-2 py-1 rounded text-[9px] transition-colors"
            :class="activeTab === t.key
              ? 'bg-t1 text-white'
              : 'text-t3 hover:bg-bg2'"
          >{{ t.label }}<template v-if="t.count?.value"> ({{ t.count.value }})</template></button>
        </div>
      </div>

      <div class="flex-1 overflow-y-auto px-2 relative" @scroll="onListScroll">
        <!-- Loading overlay — absolute so list doesn't jump -->
        <div v-if="docsLoading" class="absolute inset-0 flex items-start justify-center pt-[30%] z-10 bg-bg/80">
          <Spinner size="md" />
        </div>
        <div v-if="!docsLoading && !docs.length" class="text-[10px] text-t3 text-center py-4">No documents</div>
        <div
          v-for="d in docs" :key="d.doc_id"
          @click="selectDoc(d)"
          class="px-2.5 py-2 rounded-md cursor-pointer transition-colors mb-0.5 flex items-center gap-2"
          :class="selDoc?.doc_id === d.doc_id ? 'bg-bg2' : 'hover:bg-bg2'"
        >
          <div class="flex-1 min-w-0">
            <div class="text-[11px] text-t1 truncate">{{ d.file_name || d.filename || d.doc_id }}</div>
            <div class="text-[9px] text-t3 mt-0.5">
              {{ d.format }} · {{ d.status === 'ready' ? (d.num_chunks || 0) + ' chunks' : displayStatus(d.status) }} · {{ fmtAgo(d.updated_at || d.created_at) }}
            </div>
          </div>
          <Spinner v-if="docStatusType(d.status) === 'processing'" size="md" class="shrink-0" />
          <span v-else-if="docStatusType(d.status) === 'error'" class="relative group shrink-0"
            :title="d.error_message || 'Error'">
            <ExclamationTriangleIcon class="w-3.5 h-3.5 text-red-500" />
          </span>
        </div>
        <div v-if="loadingMore" class="text-[9px] text-t3 text-center py-3">Loading more...</div>
      </div>

      <div class="px-3 py-1.5 border-t border-line">
        <span class="text-[9px] text-t3">{{ docsTotal }} total</span>
      </div>
    </div>

    <!-- ═══════════════════════════════════
         Empty state (inline: small spinner only; standalone: upload prompt)
         ═══════════════════════════════════ -->
    <div v-if="inline && !selDoc" class="flex-1 flex items-center justify-center">
      <div class="text-[11px] text-t3 flex items-center gap-2"><Spinner size="md" /> Loading document…</div>
    </div>
    <div v-else-if="!selDoc" class="flex-1 flex items-center justify-center pl-12">
      <div class="flex flex-col items-center gap-3 text-center select-none">
        <button
          @click="openFilePicker"
          class="w-14 h-14 rounded-2xl border-2 border-dashed border-line flex items-center justify-center text-t3/40 hover:border-brand hover:text-brand transition-colors cursor-pointer"
        >
          <svg xmlns="http://www.w3.org/2000/svg" class="w-7 h-7" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
            <path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
          </svg>
        </button>
        <div>
          <div class="text-sm text-t2 font-medium">Drop files here to upload</div>
          <div class="text-[11px] text-t3 mt-1">or select a document from the list</div>
        </div>
        <div class="flex flex-wrap justify-center gap-1.5 mt-1">
          <span class="px-2 py-0.5 rounded text-[9px] bg-bg2 text-t3">PDF</span>
          <span class="px-2 py-0.5 rounded text-[9px] bg-bg2 text-t3">DOCX</span>
          <span class="px-2 py-0.5 rounded text-[9px] bg-bg2 text-t3">PPTX</span>
          <span class="px-2 py-0.5 rounded text-[9px] bg-bg2 text-t3">XLSX</span>
          <span class="px-2 py-0.5 rounded text-[9px] bg-bg2 text-t3">HTML</span>
          <span class="px-2 py-0.5 rounded text-[9px] bg-bg2 text-t3">Markdown</span>
        </div>
      </div>
    </div>

    <!-- ═══════════════════════════════════
         COL 2: Tree (always shown when doc selected)
         ═══════════════════════════════════ -->
    <div v-if="selDoc" class="shrink-0 flex flex-col border-r border-line transition-[width] duration-200"
         :class="showPdf && isPdf ? 'w-56' : 'w-72'">
      <div class="px-3 pt-4 pb-2">
        <div class="text-[10px] text-t3 uppercase tracking-widest">Structure</div>
        <div v-if="tree" class="text-[9px] text-t3 mt-0.5">{{ tree.generation_method }} · score {{ tree.quality_score?.toFixed(2) }}</div>
      </div>

      <div class="flex-1 overflow-y-auto px-2 py-2">
        <div v-if="treeLoading" class="text-[10px] text-t3 text-center py-8">Loading...</div>
        <div v-else-if="!tree" class="text-[10px] text-t3 text-center py-8">No tree data</div>
        <TreeNode
          v-else
          :node="tree.nodes[tree.root_id]"
          :nodes="tree.nodes"
          :depth="0"
          :highlight="highlightNodeIds"
          :filterNodeId="filterNodeId"
          :expanded="expanded"
          @toggle="toggleNode"
          @select="onClickTreeNode"
        />
      </div>
    </div>

    <!-- ═══════════════════════════════════
         COL 3: Chunks / Pipeline + PDF
         ═══════════════════════════════════ -->
    <div v-if="selDoc" class="flex-1 flex flex-col min-w-0">
      <!-- Header: breadcrumb + stats + toggles -->
      <div class="px-4 pt-3 pb-2 border-b border-line">
        <div class="flex items-center justify-between mb-1">
          <!-- Breadcrumb (with back arrow in inline mode) -->
          <div class="flex items-center gap-1 text-[10px] min-h-[18px] overflow-hidden flex-1 min-w-0">
            <button
              v-if="inline"
              @click="emit('close')"
              class="shrink-0 text-t3 hover:text-t1 transition-colors text-[12px] leading-none mr-1"
              title="Back to browser"
            >←</button>
            <template v-for="(seg, i) in breadcrumb" :key="i">
              <span v-if="i > 0" class="text-t3 mx-0.5 shrink-0">/</span>
              <button
                class="truncate max-w-[180px] transition-colors"
                :class="i < breadcrumb.length - 1 ? 'text-t3 hover:text-brand cursor-pointer' : 'text-t1 font-medium'"
                @click="onBreadcrumbClick(i)"
              >{{ seg }}</button>
            </template>
          </div>
          <!-- Source toggle -->
          <div v-if="isPdf" class="shrink-0 ml-3 flex items-center gap-1.5">
            <span class="text-[9px] text-t3">Source</span>
            <button @click="showPdf = !showPdf" class="toggle" :class="showPdf ? 'bg-brand' : 'bg-gray-300'">
              <div class="toggle-dot" :style="{ transform: showPdf ? 'translateX(13px)' : 'translateX(2px)' }"></div>
            </button>
          </div>
        </div>
        <!-- Stats row + Ingestion Track button + actions -->
        <div class="flex items-center gap-x-3 text-[9px] text-t3 flex-wrap gap-y-0.5">
          <!-- Ingestion Track toggle (for ready docs) -->
          <button v-if="isReady"
            @click="showPipeline = !showPipeline"
            class="px-1.5 py-0.5 rounded transition-colors"
            :class="showPipeline ? 'bg-t1 text-white' : 'bg-bg2 text-t3 hover:text-t1'"
          >Ingestion Track</button>
          <!-- Action buttons (always visible) -->
          <button @click="doRetry"
            class="p-0.5 rounded text-t3 hover:text-t1 hover:bg-bg2 transition-colors" title="Retry">
            <ArrowPathIcon class="w-3.5 h-3.5" />
          </button>
          <button @click="doDelete"
            class="p-0.5 rounded text-t3 hover:text-white hover:bg-red-500 transition-colors" title="Delete">
            <TrashIcon class="w-3.5 h-3.5" />
          </button>
          <span v-if="totalDuration && showPipeline" class="text-[9px] text-t3">{{ totalDuration }}</span>
          <!-- Stats (always shown) -->
          <template v-if="!showPipeline">
            <span>{{ chunksTotal }} chunks · {{ selDoc.num_blocks || 0 }} blocks</span>
            <span>status: <b class="text-t2">{{ selDoc.status }}</b></span>
            <span>embed: <b class="text-t2">{{ selDoc.embed_status }}</b><template v-if="selDoc.embed_model"> · {{ selDoc.embed_model }}</template></span>
            <span>enrich: <b class="text-t2">{{ selDoc.enrich_status }}</b><template v-if="selDoc.enrich_summary_count || selDoc.enrich_image_count"> · {{ selDoc.enrich_summary_count }}S · {{ selDoc.enrich_image_count }}I</template></span>
          </template>
        </div>
      </div>

      <!-- Content area: chunks/pipeline + optional PDF -->
      <div class="flex-1 flex min-h-0">

        <!-- ─── CHUNKS view ─── -->
        <div v-if="!showPipeline" class="flex flex-col min-w-0"
             :class="showPdf && isPdf ? 'w-[320px] shrink-0 border-r border-line' : 'flex-1'">
          <div class="flex-1 overflow-y-auto">
            <div v-if="chunksLoading" class="text-[10px] text-t3 text-center py-8">Loading...</div>
            <div v-else-if="!displayChunks.length" class="text-[10px] text-t3 text-center py-8">No chunks</div>
            <div
              v-for="c in displayChunks" :key="c.chunk_id"
              :ref="el => setChunkRef(c.chunk_id, el)"
              class="chunk-card px-4 py-3 border-b border-line cursor-pointer transition-colors group hover:bg-bg2"
              :class="selChunkId === c.chunk_id ? 'bg-bg2' : ''"
              @click="onClickChunk(c)"
            >
              <div class="flex items-center gap-2 mb-1">
                <span class="text-[9px] text-t3 font-mono">{{ c.chunk_id }}</span>
                <span v-if="c.content_type !== 'text'" class="chunk-type">{{ c.content_type }}</span>
                <span class="text-[9px] text-t3">{{ c.token_count }} tok</span>
                <span class="text-[9px] text-t3">p.{{ c.page_start }}{{ c.page_end !== c.page_start ? '-' + c.page_end : '' }}</span>
              </div>
              <div class="text-[10px] text-t2 leading-relaxed" :class="expandedChunks[c.chunk_id] ? '' : 'line-clamp-1'">{{ c.content }}</div>

              <!-- Inline figure images -->
              <div v-if="expandedChunks[c.chunk_id] && c.content_type === 'figure' && !(showPdf && isPdf)" class="mt-2 flex flex-wrap gap-2">
                <img
                  v-for="url in chunkImageUrls(c)" :key="url"
                  :src="url"
                  class="max-w-full max-h-64 rounded border border-line object-contain bg-white"
                  loading="lazy"
                  @error="$event.target.style.display='none'"
                />
              </div>

              <div class="flex justify-end mt-1">
                <button v-show="!expandedChunks[c.chunk_id]"
                  class="text-[9px] text-brand opacity-0 group-hover:opacity-100 transition-opacity"
                  @click.stop="toggleChunk(c.chunk_id)">view detail</button>
                <button v-show="expandedChunks[c.chunk_id]"
                  class="text-[9px] text-t3 hover:text-brand"
                  @click.stop="toggleChunk(c.chunk_id)">collapse</button>
              </div>
            </div>
          </div>

          <div v-if="chunksTotalPages > 1 && !filterNodeId" class="px-4 py-2 border-t border-line flex items-center justify-between">
            <button @click="prevChunksPage" :disabled="chunksPage === 0" class="text-[10px] text-t3 disabled:opacity-30">Prev</button>
            <span class="text-[9px] text-t3">{{ chunksPage + 1 }} / {{ chunksTotalPages }}</span>
            <button @click="nextChunksPage" :disabled="chunksPage >= chunksTotalPages - 1" class="text-[10px] text-t3 disabled:opacity-30">Next</button>
          </div>
        </div>

        <!-- ─── PIPELINE view ─── -->
        <div v-else class="overflow-y-auto"
             :class="showPdf && isPdf ? 'w-[320px] shrink-0 border-r border-line' : 'flex-1'">
          <div class="px-6 py-5">
            <div class="relative">
              <template v-for="(step, i) in steps" :key="i">
                <!-- Regular step -->
                <div v-if="step.type === 'step'" class="flex gap-4 pb-6 last:pb-0">
                  <div class="flex flex-col items-center">
                    <div class="w-5 h-5 rounded-full flex items-center justify-center shrink-0"
                         :class="{
                           'bg-t1': step.status === 'done',
                           'bg-bg3': step.status === 'pending' || step.status === 'skipped',
                           'border-2 border-t2': step.status === 'running',
                           'bg-[var(--color-err-fg)]': step.status === 'error',
                         }">
                      <CheckIcon v-if="step.status === 'done'" class="w-3 h-3 text-white" />
                      <Spinner v-else-if="step.status === 'running'" size="sm" />
                      <ExclamationTriangleIcon v-else-if="step.status === 'error'" class="w-3 h-3 text-white" />
                      <span v-else-if="step.status === 'skipped'" class="text-[8px] text-t3">&mdash;</span>
                      <span v-else class="text-[8px] text-t3">{{ i + 1 }}</span>
                    </div>
                    <div v-if="i < steps.length - 1" class="w-px flex-1 mt-1"
                         :class="step.status === 'done' ? 'bg-t1' : 'bg-line'"></div>
                  </div>
                  <div class="flex-1 min-w-0 pt-0.5">
                    <div class="flex items-center gap-2 mb-0.5">
                      <span class="text-[11px] font-medium"
                            :class="step.status === 'done' || step.status === 'running' ? 'text-t1' : 'text-t3'">{{ step.label }}</span>
                      <span v-if="step.duration" class="text-[9px] text-t3 bg-bg2 px-1.5 py-0.5 rounded">{{ step.duration }}</span>
                    </div>
                    <div v-if="step.data" class="text-[9px] text-t2 mb-0.5">{{ step.data }}</div>
                    <div class="text-[9px] text-t3">
                      <template v-if="step.startTime">{{ fmtTime(step.startTime) }}<template v-if="step.endTime"> &rarr; {{ fmtTime(step.endTime) }}</template></template>
                      <template v-else-if="step.status === 'pending'">waiting</template>
                    </div>
                  </div>
                </div>

                <!-- Group step (Processing) -->
                <div v-else-if="step.type === 'group'" class="flex gap-4 pb-6">
                  <div class="flex flex-col items-center">
                    <div class="w-5 h-5 rounded-full flex items-center justify-center shrink-0"
                         :class="{
                           'bg-t1': step.status === 'done',
                           'bg-bg3': step.status === 'pending' || step.status === 'skipped',
                           'border-2 border-t2': step.status === 'running',
                           'bg-[var(--color-err-fg)]': step.status === 'error',
                         }">
                      <CheckIcon v-if="step.status === 'done'" class="w-3 h-3 text-white" />
                      <Spinner v-else-if="step.status === 'running'" size="sm" />
                      <ExclamationTriangleIcon v-else-if="step.status === 'error'" class="w-3 h-3 text-white" />
                      <span v-else class="text-[8px] text-t3">{{ i + 1 }}</span>
                    </div>
                    <div v-if="i < steps.length - 1" class="w-px flex-1 mt-1"
                         :class="step.status === 'done' ? 'bg-t1' : 'bg-line'"></div>
                  </div>
                  <div class="flex-1 min-w-0 pt-0.5">
                    <div class="flex items-center gap-2 mb-2">
                      <span class="text-[11px] font-medium"
                            :class="step.status === 'done' || step.status === 'running' ? 'text-t1' : 'text-t3'">{{ step.label }}</span>
                    </div>
                    <div class="pl-1 border-l-2 border-line/40 ml-0.5 space-y-0">
                      <div v-for="(child, ci) in step.children" :key="ci" class="flex items-start gap-2.5 py-1.5">
                        <div class="w-3.5 h-3.5 rounded-full flex items-center justify-center shrink-0 mt-px"
                             :class="{
                               'bg-t1': child.status === 'done',
                               'bg-bg3': child.status === 'pending' || child.status === 'skipped',
                               'border-[1.5px] border-t2': child.status === 'running',
                               'bg-[var(--color-err-fg)]': child.status === 'error',
                             }">
                          <CheckIcon v-if="child.status === 'done'" class="w-2 h-2 text-white" />
                          <Spinner v-else-if="child.status === 'running'" size="xs" />
                          <ExclamationTriangleIcon v-else-if="child.status === 'error'" class="w-2 h-2 text-white" />
                          <span v-else-if="child.status === 'skipped'" class="text-[6px] text-t3">&mdash;</span>
                        </div>
                        <div class="min-w-0">
                          <div class="flex items-center gap-2">
                            <span class="text-[10px]"
                                  :class="child.status === 'done' || child.status === 'running' ? 'text-t1' : 'text-t3'">{{ child.label }}</span>
                            <span v-if="child.duration" class="text-[8px] text-t3 bg-bg2 px-1 py-0.5 rounded">{{ child.duration }}</span>
                          </div>
                          <div v-if="child.data" class="text-[8px] text-t2 mt-0.5">{{ child.data }}</div>
                          <div v-if="child.startTime" class="text-[8px] text-t3 mt-0.5">
                            {{ fmtTime(child.startTime) }}<template v-if="child.endTime"> &rarr; {{ fmtTime(child.endTime) }}</template>
                          </div>
                          <div v-else-if="child.status === 'pending'" class="text-[8px] text-t3 mt-0.5">waiting</div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </template>
            </div>
          </div>

          <details class="px-6 pb-6">
            <summary class="text-[9px] text-t3 cursor-pointer hover:text-t2">Raw data</summary>
            <pre class="mt-2 p-3 rounded bg-bg2 text-[8px] text-t3 overflow-x-auto leading-relaxed">doc_id:              {{ selDoc.doc_id }}
parse_version:       {{ selDoc.active_parse_version }}
status:              {{ selDoc.status }}
embed_status:        {{ selDoc.embed_status }}
enrich_status:       {{ selDoc.enrich_status }}
created_at:          {{ selDoc.created_at }}
parse_started_at:    {{ selDoc.parse_started_at }}
parse_completed_at:  {{ selDoc.parse_completed_at }}
structure_started_at:    {{ selDoc.structure_started_at }}
structure_completed_at:  {{ selDoc.structure_completed_at }}
enrich_started_at:   {{ selDoc.enrich_started_at }}
enrich_at:           {{ selDoc.enrich_at }}
kg_status:           {{ selDoc.kg_status }}
kg_started_at:       {{ selDoc.kg_started_at }}
kg_completed_at:     {{ selDoc.kg_completed_at }}
kg_entity_count:     {{ selDoc.kg_entity_count }}
kg_relation_count:   {{ selDoc.kg_relation_count }}
embed_started_at:    {{ selDoc.embed_started_at }}
embed_at:            {{ selDoc.embed_at }}
updated_at:          {{ selDoc.updated_at }}</pre>
          </details>
        </div>

        <!-- ─── PDF viewer ─── -->
        <div v-if="showPdf && isPdf" class="flex-1 flex flex-col min-w-0">
          <PdfViewer
            :url="pdfUrl"
            :page="pdfPage"
            :highlightBlocks="showPipeline ? [] : pdfHighlightBlocks"
            :noScroll="pdfNoScroll"
            :maxScale="1.15"
            :downloadUrl="pdfDownloadUrl"
            :sourceDownloadUrl="sourceDownloadUrl"
            :sourceLabel="sourceLabel"
            @pdf-click="onPdfClick"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.line-clamp-1 {
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.chunk-type {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 9px;
  background: var(--color-bg3);
  color: var(--color-t2);
}
.fade-enter-active, .fade-leave-active { transition: opacity .15s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
