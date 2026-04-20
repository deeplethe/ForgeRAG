/**
 * LLM Providers API — pluggable model registry
 *
 * GET    /api/v1/llm-providers              List providers (optional ?provider_type=chat)
 * GET    /api/v1/llm-providers/{id}         Get provider
 * POST   /api/v1/llm-providers              Create provider
 * PUT    /api/v1/llm-providers/{id}         Update provider
 * DELETE /api/v1/llm-providers/{id}         Delete provider
 */

import { get, del, request } from './client'

export const listLLMProviders = (providerType) =>
  get('/api/v1/llm-providers', providerType ? { provider_type: providerType } : {})

export const getLLMProvider = (id) =>
  get(`/api/v1/llm-providers/${id}`)

export const createLLMProvider = ({ name, providerType, apiBase, modelName, apiKey, isDefault }) =>
  request('/api/v1/llm-providers', {
    method: 'POST',
    body: {
      name,
      provider_type: providerType,
      api_base: apiBase || null,
      model_name: modelName,
      api_key: apiKey || null,
      is_default: isDefault || false,
    },
  })

export const updateLLMProvider = (id, updates) =>
  request(`/api/v1/llm-providers/${id}`, {
    method: 'PUT',
    body: {
      ...(updates.name != null && { name: updates.name }),
      ...(updates.providerType != null && { provider_type: updates.providerType }),
      ...(updates.apiBase !== undefined && { api_base: updates.apiBase }),
      ...(updates.modelName != null && { model_name: updates.modelName }),
      ...(updates.apiKey !== undefined && { api_key: updates.apiKey }),
      ...(updates.isDefault != null && { is_default: updates.isDefault }),
    },
  })

export const deleteLLMProvider = (id) =>
  del(`/api/v1/llm-providers/${id}`)

/**
 * List curated provider presets (one-click templates).
 * @param {string} [providerType] - filter by type (chat/embedding/reranker/vlm)
 * @returns {Promise<{presets: Array<{
 *   id: string, label: string, provider_type: string,
 *   model_name: string, api_base: string, note: string,
 *   requires_api_key: boolean, badge: string|null
 * }>}>}
 */
export const listProviderPresets = (providerType) =>
  get('/api/v1/llm-providers/presets', providerType ? { provider_type: providerType } : {})

/**
 * Send a probe request to the provider's endpoint and return connectivity status.
 * @returns {Promise<{
 *   ok: boolean, latency_ms: number,
 *   response_preview?: string,
 *   error_type?: string, message?: string, suggested_fix?: string|null
 * }>}
 */
export const testLLMProvider = (id) =>
  request(`/api/v1/llm-providers/${id}/test`, { method: 'POST' })
