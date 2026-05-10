<script setup>
/**
 * Inline tool-call chip — one chip per tool dispatch (Claude Code-
 * style). Folded state is a single one-liner that hints at WHAT got
 * called (tool name + a short detail like the Bash command, the
 * search query, the file path); expanded state shows the full
 * ``input`` (params dict the model handed the tool) and ``output``
 * (the tool's response, capped at 8 KiB upstream).
 *
 * The single-tool-per-chip layout replaces the prior batched chip
 * ("Searched 3 times, read 8 passages") because the user wants to
 * be able to click any specific call and see its bash output / file
 * diff / hit list — that's per-call data, not per-batch.
 *
 * This pass keeps the expansion content as raw text/JSON code blocks.
 * A follow-up pass specialises the rendering per tool type
 * (Bash → ``$ command`` + stdout; Read/Edit/Write → clickable file
 * path + diff; search_* → query + hits chips).
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronRight } from 'lucide-vue-next'
import ThinkingPulse from './ThinkingPulse.vue'

const props = defineProps({
  tool: { type: Object, required: true },
})

const { t } = useI18n()

// Friendly per-tool labels for the chip headline. Falls back to the
// raw tool name (``Bash``, ``Write``, …) when no i18n key matches —
// the SDK-driven Bash/Write/Read/Edit family doesn't get translated
// for now since the names are universally legible.
const TOOL_LABELS = {
  search_bm25: 'chat.tool.search_bm25',
  search_vector: 'chat.tool.search_vector',
  read_chunk: 'chat.tool.read_chunk',
  read_tree: 'chat.tool.read_tree',
  graph_explore: 'chat.tool.graph_explore',
  web_search: 'chat.tool.web_search',
  rerank: 'chat.tool.rerank',
}
const toolLabel = computed(() => {
  const k = TOOL_LABELS[props.tool.name]
  return k ? t(k) : props.tool.name
})

// Short summary line for the collapsed state. Prefer the runtime's
// pre-computed ``detail`` (already 64-char-truncated) so we don't
// need to know each tool's params shape; falls back to a derived
// preview from ``input`` for newer-style entries that didn't bother
// computing detail.
const headline = computed(() => {
  if (props.tool.detail) return props.tool.detail
  const inp = props.tool.input
  if (inp && typeof inp === 'object') {
    for (const k of ['command', 'query', 'path', 'file_path', 'chunk_id', 'doc_id']) {
      if (typeof inp[k] === 'string' && inp[k]) return inp[k].slice(0, 80)
    }
  }
  return ''
})

const inputJson = computed(() => {
  const inp = props.tool.input
  if (inp == null || (typeof inp === 'object' && !Object.keys(inp).length)) return ''
  try {
    return JSON.stringify(inp, null, 2)
  } catch {
    return String(inp)
  }
})

const outputText = computed(() => props.tool.output || '')

const running = computed(() => props.tool.status === 'running')

const expanded = ref(false)
function toggle() { expanded.value = !expanded.value }
</script>

<template>
  <div class="tool-chip" :class="{ 'is-expanded': expanded, 'is-running': running }">
    <button class="chip-head" @click="toggle">
      <ThinkingPulse v-if="running" :size="14" class="head-icon" />
      <ChevronRight v-else :size="12" :stroke-width="1.75"
        class="head-icon chev" :class="{ 'rotate-90': expanded }" />
      <span class="head-name">{{ toolLabel }}</span>
      <span v-if="headline" class="head-detail">{{ headline }}</span>
      <span v-if="tool.summary" class="head-summary">· {{ tool.summary }}</span>
    </button>
    <div v-if="expanded" class="chip-body">
      <div v-if="inputJson" class="chip-block">
        <div class="chip-block__label">Input</div>
        <pre class="chip-block__pre"><code>{{ inputJson }}</code></pre>
      </div>
      <div v-if="outputText" class="chip-block">
        <div class="chip-block__label">Output</div>
        <pre class="chip-block__pre"><code>{{ outputText }}</code></pre>
      </div>
      <div v-if="!inputJson && !outputText" class="chip-block__empty">
        (no captured payload)
      </div>
    </div>
  </div>
</template>

<style scoped>
.tool-chip {
  margin: 6px 0;
  font-size: 0.75rem;
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
.head-name {
  font-weight: 500;
  color: var(--color-t1);
  white-space: nowrap;
}
.head-detail {
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 0.6875rem;
  color: var(--color-t3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}
.head-summary {
  color: var(--color-t3);
  font-size: 0.6875rem;
  white-space: nowrap;
}

/* Expanded body — input + output blocks stacked. */
.chip-body {
  margin: 6px 0 0 18px;
  padding: 0 0 0 12px;
  border-left: 1px solid var(--color-line);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.chip-block__label {
  font-size: 0.625rem;
  font-weight: 600;
  color: var(--color-t3);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 2px;
}
.chip-block__pre {
  margin: 0;
  padding: 8px 10px;
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 0.6875rem;
  line-height: 1.5;
  color: var(--color-t1);
  background: var(--color-bg3);
  border-radius: 6px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 360px;
  overflow-y: auto;
}
.chip-block__empty {
  font-size: 0.6875rem;
  color: var(--color-t3);
  font-style: italic;
}
</style>
