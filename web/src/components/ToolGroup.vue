<script setup>
/**
 * Outer-fold container for a batch of consecutive tool calls. Two
 * levels of disclosure, Claude-Code style:
 *
 *   ┃ ▶ Used 5 tools                              ← this component
 *
 *   ┃ ▼ Used 5 tools                              (expanded)
 *   ┃   ▶ Edit ToolChip.vue +6 -1                ← <ToolChip>
 *   ┃   ▶ Bash $ pwd
 *   ┃   ...
 *   ┃   ▼ Edit ToolChip.vue +6 -1                (chip expanded)
 *   ┃     <input / output / diff>
 *
 * Folded headline is a verb summary derived from the tool family
 * mix ("Edited 2 files, ran 3 commands"). Expanded body lists each
 * call's ToolChip; the chip itself owns the inner fold (input /
 * output / diff).
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronRight } from 'lucide-vue-next'
import ThinkingPulse from './ThinkingPulse.vue'
import ToolChip from './ToolChip.vue'

const props = defineProps({
  tools: { type: Array, required: true },
})

const { t } = useI18n()

const anyRunning = computed(() => props.tools.some((x) => x.status === 'running'))

// Family classifier kept in sync with ToolChip's so the headline's
// verb count matches what the user sees once they expand. Distinct
// families let us write "Edited 2 files, ran 3 commands" instead of
// the flatter "Used 5 tools".
function family(name) {
  if (name === 'Bash') return 'bash'
  if (name === 'Write') return 'write'
  if (name === 'Edit') return 'edit'
  if (name === 'Read') return 'read'
  if (name === 'Glob' || name === 'Grep') return 'pattern'
  if ((name || '').startsWith('search_') || name === 'graph_explore'
      || name === 'web_search' || name === 'rerank') return 'search'
  if (name === 'read_chunk' || name === 'read_tree'
      || name === 'list_folders' || name === 'list_docs') return 'rag-read'
  return 'other'
}

const headline = computed(() => {
  const counts = {}
  for (const x of props.tools) {
    const f = family(x.name)
    counts[f] = (counts[f] || 0) + 1
  }
  // Pretty phrases per family. Pluralisation is fine to be naive
  // here — Chinese is invariant; English just gets ``s`` for the
  // few human-facing labels.
  const phrases = []
  if (counts.edit)    phrases.push(`Edited ${counts.edit} file${counts.edit > 1 ? 's' : ''}`)
  if (counts.write)   phrases.push(`Wrote ${counts.write} file${counts.write > 1 ? 's' : ''}`)
  if (counts.read)    phrases.push(`Read ${counts.read} file${counts.read > 1 ? 's' : ''}`)
  if (counts.bash)    phrases.push(`Ran ${counts.bash} command${counts.bash > 1 ? 's' : ''}`)
  if (counts.pattern) phrases.push(`Searched ${counts.pattern} pattern${counts.pattern > 1 ? 's' : ''}`)
  if (counts.search)  phrases.push(`Queried ${counts.search} time${counts.search > 1 ? 's' : ''}`)
  if (counts['rag-read']) phrases.push(`Read ${counts['rag-read']} passage${counts['rag-read'] > 1 ? 's' : ''}`)
  if (counts.other)   phrases.push(`Used ${counts.other} other tool${counts.other > 1 ? 's' : ''}`)
  return phrases.join(' · ') || `${props.tools.length} tool${props.tools.length > 1 ? 's' : ''}`
})

// N=1 batches don't render a ToolGroup at all — the parent
// (``AgentMessageBody``) drops the outer fold and renders the
// single ToolChip flush in the message body. So everything that
// reaches this component is N≥2 and starts collapsed.
const expanded = ref(false)
function toggle() { expanded.value = !expanded.value }
</script>

<template>
  <div class="tool-group" :class="{ 'is-expanded': expanded, 'is-running': anyRunning }">
    <button class="group-head" @click="toggle">
      <span class="head-text">{{ headline }}</span>
      <!-- Chevron sits trailing — content reads "Edited 2 files…" on the
           left, the disclosure widget hangs on the right (macOS-style)
           so the eye lands on what was done before noticing it can be
           expanded. -->
      <ThinkingPulse v-if="anyRunning" :size="14" class="head-icon head-icon--end" />
      <ChevronRight v-else :size="12" :stroke-width="1.75"
        class="head-icon head-icon--end chev" :class="{ 'rotate-90': expanded }" />
    </button>
    <div v-if="expanded" class="group-body">
      <ToolChip v-for="(tc, i) in tools" :key="tc.call_id || i" :tool="tc" />
    </div>
  </div>
</template>

<style scoped>
.tool-group {
  margin: 8px 0;
  font-size: 0.75rem;
}

/* Folded headline — Claude.ai style: chevron + grey text, NO
   border, NO background-fill. Hover lights the underline only. */
.group-head {
  /* Shrink-to-content so the chevron hugs the headline text
     instead of detaching to the row's far right. Zero left
     padding lines the headline up flush with the surrounding
     message-body text — no inset, no inherited indent. */
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
  background: transparent;
  border: none;
  color: var(--color-t2);
  cursor: pointer;
  text-align: left;
}
.group-head:hover .head-text { color: var(--color-t1); }
.group-head:hover .head-icon { color: var(--color-t2); }
.head-icon {
  flex-shrink: 0;
  color: var(--color-t3);
  transition: transform .15s;
}
/* (chevron sits inline immediately after the headline, NOT pinned
   to the row's far right — the latter felt detached from the
   content it controls.) */
.head-icon.rotate-90 { transform: rotate(90deg); }
.head-text {
  font-feature-settings: "tnum";
  letter-spacing: -0.005em;
  font-weight: 500;
  transition: color .15s;
}

/* Expanded body — single rounded panel that holds every step. The
   chip rows inside lose their individual borders so the panel reads
   as one block of activity rather than a stack of nested cards. */
/* Outline-only panel: a thin border + rounded corner is enough
   to read as one block of activity. Filling the bg with ``bg3``
   conflicted with chip-block__pre (also bg3) — the inner code
   blocks would disappear. Leaving the panel transparent lets the
   ``bg3`` of the inner blocks pop out as actual content. */
.group-body {
  margin-top: 6px;
  padding: 6px 8px;
  background: transparent;
  border: 1px solid var(--color-line);
  border-radius: 8px;
}
</style>
