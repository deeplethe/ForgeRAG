<template>
  <div class="params-panel">
    <!-- ── Card 1: Query inputs ───────────────────────────────── -->
    <div class="panel card">
      <label class="field-label">Query</label>
      <textarea
        :value="store.params.query"
        @input="store.setQuery($event.target.value)"
        rows="3"
        placeholder="e.g. what does the schema diff doc say about migrations?"
        class="input textarea"
      />
      <label class="field-label mt-3">Path filter <span class="hint">(optional)</span></label>
      <input
        :value="store.params.path_filter"
        @input="store.setPathFilter($event.target.value)"
        placeholder="/legal/2024"
        class="input"
      />
    </div>

    <!-- ── Card 2: Retrieval knobs (paths + top-K + expansion + resilience) -->
    <div class="panel card">
      <div class="group">
        <div class="section-head">Retrieval paths</div>
        <TripleSwitch
          v-for="k in pathSwitches"
          :key="k.key"
          :label="k.label"
          :model-value="store.params.overrides[k.key]"
          :modified="store.isDirty(k.key)"
          @update:modelValue="store.setOverride(k.key, $event)"
          @reset="store.resetField(k.key)"
        />
      </div>

      <div class="group">
        <div class="section-head">Top-K</div>
        <NumRow
          v-for="k in topKFields"
          :key="k.key"
          :label="k.label"
          :model-value="store.params.overrides[k.key]"
          :modified="store.isDirty(k.key)"
          :min="1" :max="500"
          @update:modelValue="store.setOverride(k.key, $event)"
          @reset="store.resetField(k.key)"
        />
      </div>

      <div class="group">
        <div class="section-head">Context expansion</div>
        <TripleSwitch
          v-for="k in expansionSwitches"
          :key="k.key"
          :label="k.label"
          :model-value="store.params.overrides[k.key]"
          :modified="store.isDirty(k.key)"
          @update:modelValue="store.setOverride(k.key, $event)"
          @reset="store.resetField(k.key)"
        />
      </div>

      <div class="group">
        <div class="section-head">Resilience</div>
        <TripleSwitch
          label="Allow partial failure"
          :model-value="store.params.overrides.allow_partial_failure"
          :modified="store.isDirty('allow_partial_failure')"
          @update:modelValue="store.setOverride('allow_partial_failure', $event)"
          @reset="store.resetField('allow_partial_failure')"
        />
      </div>
    </div>

    <!-- ── Card 3: Presets ────────────────────────────────────── -->
    <div class="panel card">
      <div class="section-head">Presets</div>
      <div class="flex gap-1.5 mb-2">
        <input
          v-model="newPresetName"
          placeholder="name…"
          class="input flex-1"
          @keydown.enter="doSavePreset"
        />
        <button @click="doSavePreset" class="btn-secondary" :disabled="!newPresetName.trim()">Save</button>
      </div>
      <div v-if="!store.presets.length" class="hint py-1">No presets yet.</div>
      <div
        v-for="p in store.presets" :key="p.id"
        class="preset-row"
      >
        <button @click="store.loadPreset(p.id)" class="preset-name">{{ p.name }}</button>
        <button @click="store.deletePreset(p.id)" class="btn-tiny" title="Delete">✕</button>
      </div>
    </div>

    <!-- ── Run / Reset (no card — actions live outside the panels) -->
    <div class="run-section">
      <button
        @click="$emit('run')"
        :disabled="store.running || !store.params.query.trim()"
        class="btn-primary w-full"
      >
        {{ store.running ? 'Running…' : 'Run simulation' }}
      </button>
      <button @click="store.resetParams" class="btn-ghost mt-1.5">Reset all to defaults</button>
      <div v-if="store.error" class="err-msg">{{ store.error }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useSimulationStore } from '@/stores/simulation'
import TripleSwitch from './TripleSwitch.vue'
import NumRow from './NumRow.vue'

defineEmits(['run'])

const store = useSimulationStore()
const newPresetName = ref('')

const pathSwitches = [
  { key: 'query_understanding', label: 'Query understanding' },
  { key: 'kg_path',             label: 'KG path' },
  { key: 'tree_path',           label: 'Tree path' },
  { key: 'tree_llm_nav',        label: 'Tree LLM navigator' },
  { key: 'rerank',              label: 'Rerank stage' },
]
const topKFields = [
  { key: 'bm25_top_k',      label: 'BM25 top-k' },
  { key: 'vector_top_k',    label: 'Vector top-k' },
  { key: 'tree_top_k',      label: 'Tree top-k' },
  { key: 'kg_top_k',        label: 'KG top-k' },
  { key: 'rerank_top_k',    label: 'Rerank top-k' },
  { key: 'candidate_limit', label: 'Candidate limit' },
]
const expansionSwitches = [
  { key: 'descendant_expansion', label: 'Descendant' },
  { key: 'sibling_expansion',    label: 'Sibling' },
  { key: 'crossref_expansion',   label: 'Cross-ref' },
]

function doSavePreset() {
  if (!newPresetName.value.trim()) return
  store.savePreset(newPresetName.value)
  newPresetName.value = ''
}
</script>

<style scoped>
/*
  Card-based layout (Vercel observability pattern):
    - Each .panel is a self-contained card; no border-bottom dividers between
      sections. Stack with `gap` for breathing room.
    - Within a card, related rows are visually grouped by `.group` margin —
      no horizontal rules, just a `.section-head` label.
*/
.params-panel {
  padding: 14px 14px 24px;
  font-size: 11px;
  color: var(--color-t2);
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.card { padding: 16px 18px; }

.group + .group { margin-top: 16px; }

.field-label {
  display: block;
  font-size: 10px;
  color: var(--color-t3);
  margin-bottom: 4px;
}
.hint { font-size: 10px; color: var(--color-t3); }
.mt-3 { margin-top: 12px; }

.textarea { resize: vertical; font-family: inherit; min-height: 56px; }

.btn-ghost {
  width: 100%;
  padding: 5px;
  font-size: 10px;
  color: var(--color-t3);
  background: transparent;
  border: none;
  cursor: pointer;
}
.btn-ghost:hover { color: var(--color-t1); }

.btn-tiny {
  width: 18px; height: 18px;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 10px;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: var(--r-sm);
  cursor: pointer;
}
.btn-tiny:hover { background: var(--color-bg2); color: var(--color-err-fg); }

.preset-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 0;
}
.preset-name {
  flex: 1;
  text-align: left;
  padding: 5px 8px;
  font-size: 11px;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.preset-name:hover { background: var(--color-bg2); color: var(--color-t1); border-color: var(--color-line); }

.err-msg {
  margin-top: 8px;
  font-size: 10px;
  color: var(--color-err-fg);
}

.run-section { padding-top: 4px; }
.w-full { width: 100%; }
</style>
