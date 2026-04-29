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

export const getGraphByDoc = (docId) =>
  get(`/api/v1/graph/by-doc/${docId}`)
