import { get } from './client'

export const getGraphStats = () => get('/api/v1/graph/stats')

export const searchEntities = (query, topK = 20) =>
  get('/api/v1/graph/entities', { query, top_k: topK })

export const getEntityDetail = (entityId) =>
  get(`/api/v1/graph/entities/${entityId}`)

export const getSubgraph = (entityIds) =>
  get('/api/v1/graph/subgraph', { entity_ids: entityIds.join(',') })

export const getFullGraph = (limit = 500) =>
  get('/api/v1/graph/full', { limit })

// Anchor + halo subgraph — high-degree entities plus their 1-hop
// neighbours. The dense option for /knowledge-graph; ``getFullGraph``
// returns sparse top-N because edges between non-top nodes get
// dropped. ``opts`` keys: anchors, halo_cap, doc_id, entity_type.
export const getGraphExplore = (opts = {}) => {
  const params = {
    anchors: opts.anchors ?? 200,
    halo_cap: opts.halo_cap ?? 600,
  }
  if (opts.doc_id) params.doc_id = opts.doc_id
  if (opts.entity_type) params.entity_type = opts.entity_type
  return get('/api/v1/graph/explore', params)
}

export const getGraphByDoc = (docId) =>
  get(`/api/v1/graph/by-doc/${docId}`)
