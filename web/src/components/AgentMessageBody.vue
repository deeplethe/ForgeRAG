<script setup>
/**
 * Renders an agent message as an interleaved sequence of:
 *   * text segments — the model's narration / thoughts / final answer
 *   * tool-call groups — compact expandable chips
 *
 * Replaces the prior layout where a "thinking chain" panel hovered
 * above the message body. Now, in the Claude-Code style, the
 * narration weaves through the actions:
 *
 *   "Now check the existing migration pattern and add a new one:"
 *   ▸ Searched code, read 2 files, edited a file
 *   "Now update the frontend to read the persisted trace:"
 *   ▸ Read 3 files, searched code, edited a file
 *   "Based on the corpus, here is a comprehensive answer..."
 *   <FULL FINAL ANSWER MARKDOWN>
 *
 * Data shape: takes ``trace`` (the agent reasoning entries already
 * built by the SSE handler / persisted on the message row) plus
 * ``content`` (the final answer string) plus ``citations`` (for
 * inline ``[c_N]`` resolution in any text segment).
 */
import { computed } from 'vue'
import { renderMarkdown } from '@/utils/renderMarkdown'
import MarkdownBody from './MarkdownBody.vue'
import ToolGroup from './ToolGroup.vue'
import ToolChip from './ToolChip.vue'

const props = defineProps({
  trace: { type: Array, default: () => [] },
  content: { type: String, default: '' },
  citations: { type: Array, default: null },
  // Optional: render-citation-aware function from the parent so
  // inline ``[c_N]`` markers become clickable spans. The parent
  // owns the click handler + active-citation state, so it passes
  // its rendering function in instead of duplicating logic here.
  renderText: { type: Function, default: null },
  // Click handler for inline citation tags inside rendered text.
  onCiteClick: { type: Function, default: null },
})

// Turn the (raw) trace into render parts:
//   * 'text' parts come from phase/thought entries with non-empty text
//   * 'tools' parts group consecutive tool entries between text
//     stretches into a single batch — Claude Code-style two-level
//     fold: the outer ToolGroup chip says "Used N tools", expanding
//     it lists each tool's chip, and any single chip then expands
//     its own input/output. Keeps the chain readable when the agent
//     fires off 5+ calls without narrating between.
// Final answer (props.content) lands as the trailing text part.
const parts = computed(() => {
  const out = []
  const trace = props.trace || []

  // Helper to push a text part (merge with trailing text part if any).
  const pushText = (txt) => {
    if (!txt) return
    const last = out[out.length - 1]
    if (last && last.kind === 'text') {
      last.content = (last.content + '\n\n' + txt).trim()
    } else {
      out.push({ kind: 'text', content: txt.trim() })
    }
  }
  // Helper to start / extend a tool batch.
  const pushTool = (entry) => {
    const last = out[out.length - 1]
    if (last && last.kind === 'tools') {
      last.tools.push(entry)
    } else {
      out.push({ kind: 'tools', tools: [entry] })
    }
  }

  for (const entry of trace) {
    if (entry.kind === 'phase' || entry.kind === 'thought') {
      if (entry.text) pushText(entry.text)
    } else if (entry.kind === 'tool') {
      pushTool(entry)
    }
  }

  // Final answer body (the model's response after the last tool turn).
  // Goes at the very end — separated from any preceding thought text
  // so markdown rendering picks up headings + lists fresh.
  if (props.content) pushText(props.content)

  return out
})

function renderPart(text) {
  // Use the parent's citation-aware renderer when available, so
  // ``[c_N]`` markers in narration / answer text become clickable
  // chips wired to the parent's PDF panel.
  if (props.renderText) return props.renderText(text, props.citations)
  return renderMarkdown(text)
}
</script>

<template>
  <div class="agent-msg-body">
    <template v-for="(part, i) in parts" :key="i">
      <MarkdownBody
        v-if="part.kind === 'text'"
        class="text-part text-sm leading-7 text-t1"
        :html="renderPart(part.content)"
        @click="onCiteClick && onCiteClick($event)"
      />
      <!-- Single-tool batches skip the outer fold entirely — there's
           no information in "Used 1 tool" that you wouldn't already
           see one click deeper. The chip renders flush in the
           message body just like a paragraph would. -->
      <ToolChip
        v-else-if="part.kind === 'tools' && part.tools.length === 1"
        :tool="part.tools[0]"
      />
      <ToolGroup v-else-if="part.kind === 'tools'" :tools="part.tools" />
    </template>
  </div>
</template>

<style scoped>
.agent-msg-body {
  /* Spacing between parts is handled per-part: text parts use
     internal margin from MarkdownBody's typography, tool chips
     have their own top/bottom margin. */
}
.text-part {
  margin: 4px 0;
}
.text-part:first-child { margin-top: 0; }
.text-part:last-child { margin-bottom: 0; }

/* Horizontal-rule override scoped to text parts inside this
   component: render essentially invisible (margin only, no line).
   The model occasionally writes ``---`` between sections even
   though the prompt asks it not to; without this override every
   section break shows as a full-width grey bar that fragments
   the answer visually. We keep the spacing so the section break
   is felt, just without the ruler. The base ``.msg-body :deep(hr)``
   in MarkdownBody draws a ruler — fine for file previews but
   noisy in chat answers. */
.text-part :deep(hr) {
  border: none;
  margin: 0.5em 0;
}
</style>
