/**
 * Architecture graph — static node + edge definitions.
 *
 * Extracted from Architecture.vue so the main SFC stays focused on
 * state + interaction and the graph topology is reviewable on its
 * own. Layout constants (columns / rows) are exported so the view
 * can compute positions without depending on the Vue Flow instance.
 */

// Columns (x positions, 210px apart) — 6 columns total.
// Col 5 (1050px) is used for the post-merge chain (RRF / Expansion / Rerank)
// so they can sit on the same row as Tree Navigation and flow horizontally
// instead of zig-zagging.
export const COLS = [0, 210, 420, 630, 840, 1050]

// Rows (y positions for each section)
export const ROWS = {
  a: 0,    // Document Ingestion
  b: 190,  // Persistence
  c: 310,  // Multi-Modal Retrieval
  d: 680,  // Answer Generation
}

export const LAYER_COLORS = {
  a: '#3b82f6',
  b: '#8b5cf6',
  c: '#f59e0b',
  d: '#10b981',
}

export const LAYER_LABELS = [
  { id: 'label_a', label: 'Document Ingestion',  row: 'a', color: LAYER_COLORS.a },
  { id: 'label_b', label: 'Persistence',         row: 'b', color: LAYER_COLORS.b },
  { id: 'label_c', label: 'Multi-Modal Retrieval', row: 'c', color: LAYER_COLORS.c },
  { id: 'label_d', label: 'Answer Generation',   row: 'd', color: LAYER_COLORS.d },
]

const C = COLS
const R = ROWS

export const NODES = [
  // (a) Document Ingestion
  { id: 'file_upload',   label: 'File Upload',     desc: 'PDF, DOCX, PPTX, HTML...', layer: 'a', pos: [C[0], R.a] },
  { id: 'parser',        label: 'Document Parser', desc: 'PyMuPDF · MinerU · VLM',   layer: 'a', pos: [C[1], R.a] },
  { id: 'chunker',       label: 'Chunker',         desc: 'Token-based · tree-aware', layer: 'a', pos: [C[2], R.a] },
  { id: 'tree_builder',  label: 'Tree Builder',    desc: 'LLM page-group inference',layer: 'a', pos: [C[3], R.a] },
  { id: 'embedding',     label: 'Embedder',        desc: 'Dense vector encoding',   layer: 'a', pos: [C[2], R.a + 110] },
  { id: 'kg_extraction', label: 'KG Extraction',   desc: 'Entity + relation extraction', layer: 'a', pos: [C[3], R.a + 110] },

  // (b) Persistence
  { id: 'filestore',    label: 'Blob Storage',  desc: 'Local · S3 · OSS',              layer: 'b', pos: [C[1], R.b] },
  { id: 'database',     label: 'Relational DB', desc: 'SQLite · PostgreSQL · MySQL',   layer: 'b', pos: [C[2], R.b] },
  { id: 'vector_store', label: 'Vector Store',  desc: 'ChromaDB · pgvector · Qdrant',  layer: 'b', pos: [C[3], R.b] },
  { id: 'graph_store',  label: 'Graph Store',   desc: 'NetworkX · Neo4j',              layer: 'b', pos: [C[4], R.b] },

  // (c) Retrieval
  { id: 'user_query', label: 'User Query',          desc: 'Natural language question',  layer: 'c', pos: [C[0], R.c + 90] },
  { id: 'qu',         label: 'Query Understanding', desc: 'Expand & classify intent',   layer: 'c', pos: [C[1], R.c + 90] },
  { id: 'bm25',       label: 'BM25',                desc: 'Keyword matching',           layer: 'c', pos: [C[2], R.c] },
  { id: 'vector',     label: 'Vector Search',       desc: 'Semantic similarity',        layer: 'c', pos: [C[2], R.c + 90] },
  { id: 'tree',       label: 'Tree Navigation',     desc: 'LLM structure reasoning',    layer: 'c', pos: [C[2], R.c + 180] },
  { id: 'kg',         label: 'KG Path',             desc: 'Multi-hop traversal',        layer: 'c', pos: [C[2], R.c + 270] },
  { id: 'fusion',     label: 'RRF Merge',           desc: 'Reciprocal rank fusion',     layer: 'c', pos: [C[3], R.c + 180] },
  { id: 'expansion',  label: 'Context Expansion',   desc: 'Descendant · sibling · xref',layer: 'c', pos: [C[4], R.c + 180] },
  { id: 'rerank',     label: 'Rerank',              desc: 'LLM relevance scoring',      layer: 'c', pos: [C[5], R.c + 180] },

  // (d) Answer Generation
  { id: 'prompt_builder',   label: 'Prompt Builder',   desc: 'Context + chunks + KG', layer: 'd', pos: [C[1], R.d] },
  { id: 'generator',        label: 'LLM Generation',   desc: 'Streaming response',    layer: 'd', pos: [C[2], R.d] },
  { id: 'citation_builder', label: 'Citation Builder', desc: 'Bbox + page mapping',   layer: 'd', pos: [C[3], R.d] },
  { id: 'answer',           label: 'Answer',           desc: 'Pixel-precise citations', layer: 'd', pos: [C[4], R.d] },
]

/**
 * Edges between nodes. Format: [source, target, sourceHandle?, targetHandle?, flags?]
 * Handles: 't'=top, 'b'=bottom, 'l'=left, 'r'=right (default: r→l horizontal).
 * Flags: 'noarrow' or 'dashed'.
 */
export const EDGES = [
  // (a) Ingestion
  ['file_upload', 'parser'],
  ['parser', 'chunker'],
  ['chunker', 'tree_builder'],
  ['chunker', 'embedding', 'b', 't'],
  ['chunker', 'kg_extraction', 'b', 't'],

  // (b) Persistence — visual association, no arrows
  ['filestore', 'database', null, null, 'noarrow'],
  ['database', 'vector_store', null, null, 'noarrow'],
  ['vector_store', 'graph_store', null, null, 'noarrow'],

  // (c) Retrieval
  ['user_query', 'qu'],
  ['qu', 'bm25'],
  ['qu', 'vector'],
  ['qu', 'kg'],
  ['bm25', 'tree', 'b', 't'],
  ['vector', 'tree', 'b', 't'],
  ['tree', 'fusion'],
  ['kg', 'fusion'],
  ['fusion', 'expansion'],
  ['expansion', 'rerank'],

  // (d) Generation
  ['prompt_builder', 'generator'],
  ['generator', 'citation_builder'],
  ['citation_builder', 'answer'],
]

// Which nodes can be toggled on/off from the side panel.
export const TOGGLEABLE = new Set(['vector', 'bm25', 'tree', 'rerank', 'qu', 'kg', 'kg_extraction'])

// Group map: which settings groups to show in the side panel for each node.
export const GROUP_MAP = {
  filestore:        ['blob_storage', 'cache'],
  file_upload:      [],
  parser:           ['parser', 'images'],
  tree_builder:     ['tree_builder'],
  chunker:          ['chunker'],
  embedding:        ['embedding'],
  kg_extraction:    ['kg_extraction'],
  database:         ['persistence_relational'],
  vector_store:     ['persistence_vector'],
  graph_store:      ['persistence_graph'],
  user_query:       [],
  qu:               ['query_understanding', 'prompts_qu'],
  vector:           ['retrieval_vector'],
  bm25:             ['retrieval_bm25'],
  tree:             ['retrieval_tree', 'prompts_tree'],
  kg:               ['kg'],
  fusion:           ['retrieval_fusion'],
  expansion:        ['context_expansion'],
  rerank:           ['rerank', 'prompts_rerank'],
  prompt_builder:   [],
  generator:        ['llm', 'prompts_gen'],
  citation_builder: [],
  answer:           [],
}

// Node → component health registry key (for status dot + tooltip).
export const NODE_TO_HEALTH = {
  rerank:        'reranker',
  embedding:     'embedder',
  vector:        'vector_path',
  bm25:          'bm25_path',
  tree:          'tree_path',
  kg:            'kg_path',
  kg_extraction: 'kg_extraction',
  qu:            'query_understanding',
  generator:     'answer_generator',
}
