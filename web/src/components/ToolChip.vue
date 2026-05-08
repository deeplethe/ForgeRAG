<script setup>
/**
 * Inline tool-call chip — Claude Code-style compact summary of one
 * or more consecutive tool dispatches the agent made between two
 * stretches of natural-language reasoning.
 *
 *   ▸ Searched the corpus, read 4 passages
 *   ▸ Explored knowledge graph, read 8 passages
 *   ▸ Ran 7 commands, used a tool
 *
 * Click the chip to expand a per-tool list with each call's
 * detail (the query string for searches, the chunk_id for reads,
 * etc.) and any result summary the dispatch returned ("20 hits",
 * "10 entities", "error").
 *
 * Lives inline in the message body — the whole "agent reasoning
 * chain" panel that used to hover above the answer is now woven
 * into the message itself, with text segments (the model's own
 * narration via ``agent.thought`` / streamed deltas) rendered
 * as normal markdown between chips.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronRight } from 'lucide-vue-next'
import ThinkingPulse from './ThinkingPulse.vue'
import ToolRichOutputs from './ToolRichOutputs.vue'

const props = defineProps({
  tools: { type: Array, required: true },
})

const { t } = useI18n()

// Per-tool friendly labels for the EXPANDED detail rows. The
// collapsed headline uses verb-style summaries derived below.
const TOOL_LABELS = {
  search_bm25: 'chat.tool.search_bm25',
  search_vector: 'chat.tool.search_vector',
  read_chunk: 'chat.tool.read_chunk',
  read_tree: 'chat.tool.read_tree',
  graph_explore: 'chat.tool.graph_explore',
  web_search: 'chat.tool.web_search',
  rerank: 'chat.tool.rerank',
  python_exec: 'chat.tool.python_exec',
}
function toolLabel(name) {
  const k = TOOL_LABELS[name]
  return k ? t(k) : name
}

// Build the collapsed-state headline by counting tools per type
// and joining them with commas:
//   Searched 2 times, read 8 passages, explored graph
const headline = computed(() => {
  const counts = {}
  for (const t of props.tools) {
    counts[t.name] = (counts[t.name] || 0) + 1
  }
  const phrases = []
  for (const [name, n] of Object.entries(counts)) {
    phrases.push(t(`chat.chip.${name}`, { n }))
  }
  return phrases.join(t('chat.chip.sep'))
})

const anyRunning = computed(() => props.tools.some((t) => t.status === 'running'))

// Phase 2.5: rich outputs (matplotlib PNGs / DataFrame HTML / plotly
// JSON) saved to ``scratch/_rich_outputs/`` by the backend, attached
// to each tool entry as ``rich_outputs: [{kind, mime, path,
// size_bytes, project_id}]``. Flatten across all tool calls in
// THIS chip group; preserve order so figures render in the order
// they were produced.
const allRichOutputs = computed(() => {
  const out = []
  for (const tc of props.tools) {
    const list = Array.isArray(tc.rich_outputs) ? tc.rich_outputs : []
    for (const r of list) out.push(r)
  }
  return out
})

const expanded = ref(false)
function toggle() { expanded.value = !expanded.value }

function fmtMs(ms) {
  if (ms == null) return ''
  if (ms <= 0) return '<1ms'
  if (ms < 1000) return ms + 'ms'
  const sec = ms / 1000
  return sec < 10 ? sec.toFixed(1) + 's' : Math.round(sec) + 's'
}
</script>

<template>
  <div class="tool-chip" :class="{ 'is-expanded': expanded, 'is-running': anyRunning }">
    <button class="chip-head" @click="toggle">
      <ThinkingPulse v-if="anyRunning" :size="14" class="head-icon" />
      <ChevronRight v-else :size="12" :stroke-width="1.75"
        class="head-icon chev" :class="{ 'rotate-90': expanded }" />
      <span class="head-text">{{ headline }}</span>
    </button>
    <!-- Rich outputs (figures / HTML tables) ALWAYS show even when
         the chip is collapsed — they're content, not trace details.
         Indented to align with the chip's icon column. -->
    <ToolRichOutputs
      v-if="allRichOutputs.length"
      :outputs="allRichOutputs"
    />
    <ol v-if="expanded" class="detail-list">
      <li v-for="(tc, i) in tools" :key="tc.call_id || i" class="detail-row">
        <span class="detail-name">{{ toolLabel(tc.name) }}</span>
        <span v-if="tc.detail" class="detail-text">"{{ tc.detail }}"</span>
        <span class="detail-meta">
          <template v-if="tc.summary">{{ tc.summary }} · </template>
          <template v-if="tc.elapsedMs != null">{{ fmtMs(tc.elapsedMs) }}</template>
        </span>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.tool-chip {
  margin: 8px 0;
  font-size: 12px;
}
.chip-head {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px 4px 6px;
  background: transparent;
  border: 1px solid var(--color-line);
  border-radius: 6px;
  color: var(--color-t2);
  cursor: pointer;
  transition: background-color .15s, border-color .15s;
  text-align: left;
  max-width: 100%;
}
.chip-head:hover {
  background: var(--color-bg3);
  border-color: var(--color-line2);
}
.head-icon {
  flex-shrink: 0;
  color: var(--color-t3);
  transition: transform .15s;
}
.head-icon.rotate-90 { transform: rotate(90deg); }
.head-text {
  font-feature-settings: "tnum";
  letter-spacing: -0.005em;
}

.detail-list {
  list-style: none;
  margin: 6px 0 0 18px;
  padding: 0;
  border-left: 1px solid var(--color-line);
  padding-left: 12px;
}
.detail-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 3px 0;
  line-height: 1.5;
}
.detail-name {
  color: var(--color-t2);
  white-space: nowrap;
}
.detail-text {
  color: var(--color-t3);
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  flex: 1 1 auto;
}
.detail-meta {
  color: var(--color-t3);
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 11px;
  font-feature-settings: "tnum";
  margin-left: auto;
  white-space: nowrap;
}
</style>
