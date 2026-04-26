/**
 * Simulation page store.
 *
 * Source of values:
 *   - On page mount, we fetch GET /api/v1/settings (read-only resolved cfg
 *     snapshot) and seed `defaults` with the current backend values for every
 *     QueryOverrides knob.
 *   - `params.overrides` starts as a deep copy of `defaults`. The user edits
 *     directly — no "default" tri-state UI, no null sentinel. What you see
 *     IS what the backend will use.
 *   - `dirty` tracks fields the user actually changed; `requestBody` only
 *     ships those to /query so the backend's effective config stays the
 *     source of truth for unchanged knobs (and so per-request traces show
 *     accurate "from yaml" vs "overridden" provenance).
 */

import { defineStore } from 'pinia'
import { get } from '@/api/client'

const LS_KEY = 'forgerag.simulation.presets.v1'
const MAX_PRESETS = 20

// Map of QueryOverrides field → settings key in /api/v1/settings.
// Anything missing here is left as null until the user explicitly sets it.
const SETTINGS_KEY_MAP = {
  query_understanding:  'retrieval.query_understanding.enabled',
  kg_path:              'retrieval.kg_path.enabled',
  tree_path:            'retrieval.tree_path.enabled',
  tree_llm_nav:         'retrieval.tree_path.llm_nav_enabled',
  rerank:               'retrieval.rerank.enabled',
  bm25_top_k:           'retrieval.bm25.top_k',
  vector_top_k:         'retrieval.vector.top_k',
  tree_top_k:           'retrieval.tree_path.top_k',
  kg_top_k:             'retrieval.kg_path.top_k',
  rerank_top_k:         'retrieval.rerank.top_k',
  candidate_limit:      'retrieval.merge.candidate_limit',
  descendant_expansion: 'retrieval.merge.descendant_expansion_enabled',
  sibling_expansion:    'retrieval.merge.sibling_expansion_enabled',
  crossref_expansion:   'retrieval.merge.crossref_expansion_enabled',
  // allow_partial_failure has no first-class setting; backend default = true
}

const ALLOW_PARTIAL_FALLBACK = true
const FIELD_KEYS = Object.keys(SETTINGS_KEY_MAP).concat(['allow_partial_failure'])

function emptyOverrides() {
  return Object.fromEntries(FIELD_KEYS.map((k) => [k, null]))
}

function loadPresets() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr.slice(0, MAX_PRESETS) : []
  } catch {
    return []
  }
}

function savePresets(list) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(list.slice(0, MAX_PRESETS)))
  } catch {}
}

export const useSimulationStore = defineStore('simulation', {
  state: () => ({
    // Resolved backend defaults — populated by ensureDefaults().
    defaults: emptyOverrides(),
    defaultsLoaded: false,
    defaultsError: '',

    params: {
      query: '',
      path_filter: '/',  // root scope by default — searches the whole corpus
      overrides: emptyOverrides(),
    },
    // Per-key flag: did the user touch this field? Used to ship only modified
    // overrides in the request body.
    dirty: Object.fromEntries(FIELD_KEYS.map((k) => [k, false])),

    presets: loadPresets(),
    lastResult: null,
    running: false,
    error: '',
    selectedStageKey: null,
  }),

  getters: {
    // Send only fields the user explicitly modified; the backend uses yaml
    // defaults for everything else (and per-request traces accurately show
    // which knobs were overridden).
    requestBody(state) {
      const body = { query: state.params.query }
      // Only send path_filter when it's narrower than root — '/' = full corpus,
      // equivalent to not sending it at all, so we skip to keep the body clean.
      const pf = state.params.path_filter
      if (pf && pf !== '/') body.path_filter = pf
      const ov = {}
      for (const k of FIELD_KEYS) {
        if (state.dirty[k] && state.params.overrides[k] != null) {
          ov[k] = state.params.overrides[k]
        }
      }
      if (Object.keys(ov).length) body.overrides = ov
      return body
    },
  },

  actions: {
    /**
     * Fetch the resolved config from the backend and seed both `defaults`
     * and any not-yet-touched `params.overrides` entries. Called once on
     * page mount; cheap to call again (re-syncs without clobbering user
     * edits).
     */
    async ensureDefaults() {
      if (this.defaultsLoaded) return
      try {
        const res = await get('/api/v1/settings')
        const flat = {}
        for (const settings of Object.values(res.groups || {})) {
          for (const s of settings) flat[s.key] = s.value_json
        }
        const next = emptyOverrides()
        for (const [field, settingKey] of Object.entries(SETTINGS_KEY_MAP)) {
          if (settingKey in flat) next[field] = flat[settingKey]
        }
        next.allow_partial_failure = ALLOW_PARTIAL_FALLBACK
        this.defaults = next
        // Prime params for fields the user hasn't touched yet
        for (const k of FIELD_KEYS) {
          if (!this.dirty[k]) this.params.overrides[k] = next[k]
        }
        this.defaultsLoaded = true
      } catch (e) {
        this.defaultsError = e?.message || String(e)
      }
    },

    resetParams() {
      this.params.query = ''
      this.params.path_filter = ''
      // Reset overrides to backend defaults; clear dirty flags
      this.params.overrides = { ...this.defaults }
      this.dirty = Object.fromEntries(FIELD_KEYS.map((k) => [k, false]))
      this.selectedStageKey = null
    },

    /** Reset a single field back to the backend default (clears its dirty flag). */
    resetField(key) {
      if (!(key in this.params.overrides)) return
      this.params.overrides[key] = this.defaults[key]
      this.dirty[key] = false
    },

    setQuery(q) { this.params.query = q },
    setPathFilter(p) { this.params.path_filter = p },
    setOverride(key, value) {
      if (!(key in this.params.overrides)) return
      this.params.overrides[key] = value
      // Mark dirty if this differs from the resolved default
      this.dirty[key] = value !== this.defaults[key]
    },

    /** True iff this field's current value differs from the backend default. */
    isDirty(key) { return !!this.dirty[key] },

    savePreset(name) {
      if (!name || !name.trim()) return
      const snap = {
        id: 's_' + Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-4),
        name: name.trim(),
        params: JSON.parse(JSON.stringify(this.params)),
        // Snapshot dirty flags too — so loading a preset preserves
        // "what was modified" semantics.
        dirty: { ...this.dirty },
        created_at: Date.now(),
      }
      const idx = this.presets.findIndex((p) => p.name === snap.name)
      if (idx >= 0) this.presets.splice(idx, 1, snap)
      else this.presets.unshift(snap)
      savePresets(this.presets)
    },

    loadPreset(id) {
      const p = this.presets.find((x) => x.id === id)
      if (!p) return
      this.params = JSON.parse(JSON.stringify(p.params))
      this.dirty = p.dirty ? { ...p.dirty } : Object.fromEntries(
        FIELD_KEYS.map((k) => [k, this.params.overrides[k] !== this.defaults[k]]),
      )
      this.selectedStageKey = null
    },

    deletePreset(id) {
      this.presets = this.presets.filter((p) => p.id !== id)
      savePresets(this.presets)
    },

    startRun() {
      this.running = true
      this.error = ''
      this.lastResult = null
      this.selectedStageKey = null
    },

    setResult(res) {
      this.lastResult = res
      this.running = false
      this.selectedStageKey = 'forgerag.retrieve'
    },

    setError(msg) {
      this.error = msg || 'Simulation failed'
      this.running = false
    },

    selectStage(key) { this.selectedStageKey = key },
  },
})
