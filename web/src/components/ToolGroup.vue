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
import { ChevronRight, ChevronDown } from 'lucide-vue-next'
import ThinkingPulse from './ThinkingPulse.vue'
import ToolChip from './ToolChip.vue'

const props = defineProps({
  tools: { type: Array, required: true },
})

const { t } = useI18n()

const anyRunning = computed(() => props.tools.some((x) => x.status === 'running'))
// Failure detection — same triple-source check as ToolChip so the
// summary stays consistent across the two components.
function isFailed(t) {
  return Boolean(
    t?.isError
      || t?.status === 'error'
      || t?.summary === 'error',
  )
}
const failedCount = computed(() =>
  props.tools.reduce((n, t) => n + (isFailed(t) ? 1 : 0), 0),
)

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
  <div class="tool-group" :class="{ 'is-expanded': expanded, 'is-running': anyRunning, 'has-failed': failedCount > 0 }">
    <button class="group-head" @click="toggle">
      <span class="head-text">{{ headline }}</span>
      <!-- Failure tally — small red badge appended to the headline
           ("Used 5 tools  · 1 failed") so the user sees the failure
           count BEFORE expanding the group. Plural-aware label. -->
      <span v-if="failedCount > 0" class="head-failed">· {{ failedCount }} failed</span>
      <!-- Chevron sits trailing — content reads "Edited 2 files…" on the
           left, the disclosure widget hangs on the right (macOS-style)
           so the eye lands on what was done before noticing it can be
           expanded. -->
      <ThinkingPulse v-if="anyRunning" :size="14" class="head-icon head-icon--end" />
      <component
        v-else
        :is="expanded ? ChevronDown : ChevronRight"
        :size="12"
        :stroke-width="1.75"
        class="head-icon head-icon--end chev"
      />
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
   content it controls. Expanded state swaps to ``ChevronDown``
   directly instead of rotating ``ChevronRight``, dodging any
   Tailwind ``.rotate-90`` utility specificity weirdness.) */
.head-text {
  font-feature-settings: "tnum";
  letter-spacing: -0.005em;
  font-weight: 500;
  transition: color .15s;
}
/* Failure tally — small red badge on the headline so a batch with
   any failed tools is scan-pickable without expanding. Stays subtle
   (no background fill) — paired with the in-row red headlines on
   the actual failed chips below. */
.head-failed {
  margin-left: 4px;
  color: var(--color-err-fg);
  font-size: 0.6875rem;
  font-feature-settings: "tnum";
  white-space: nowrap;
}

/* Expanded body — single rounded gray panel holding every step.
   Hierarchy reads through INDENTATION, not chunky color jumps:
   chip rows inside align flush-left with this panel's padding,
   chip-expansion content nudges in just slightly more so the
   second-vs-third level relationship is a small, even step. */
.group-body {
  margin-top: 4px;
  padding: 6px 10px;
  background: var(--color-bg3);
  border-radius: 8px;
}
</style>
