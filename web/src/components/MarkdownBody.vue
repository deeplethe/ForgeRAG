<template>
  <div class="msg-body" v-html="rendered" />
</template>

<script setup>
/**
 * Shared markdown render surface.
 *
 * One component consumed by every place the app renders markdown for
 * humans to read — the agent's chat reply and the workdir ``.md`` file
 * preview today, plus whatever lands next. Two-way input shape so
 * callers can either:
 *
 *   * pass raw markdown in ``source`` and let us run
 *     ``utils/renderMarkdown`` (marked + katex), or
 *   * pass already-rendered HTML in ``html`` (the chat does this for
 *     citation post-processing — its renderer wraps ``[c_N]`` markers
 *     in clickable spans before the HTML lands here).
 *
 * Click events bubble naturally through the root ``<div>``, so a
 * caller binding ``@click="onCiteClick"`` on ``<MarkdownBody>``
 * receives clicks on inline citations the same as before.
 *
 * The typography rules below are scoped to this component because
 * ``v-html`` content carries no scope attribute itself — ``:deep``
 * unwraps the data-attribute on the descendants so the cascade
 * actually reaches them. Living in this single component keeps the
 * styling consistent across surfaces; previously each consumer had
 * its own (or missing) copy.
 */
import { computed } from 'vue'
import { renderMarkdown } from '@/utils/renderMarkdown'

const props = defineProps({
  source: { type: String, default: '' },
  html: { type: String, default: null },
})

const rendered = computed(() =>
  props.html != null ? props.html : renderMarkdown(props.source || ''),
)
</script>

<style scoped>
.msg-body :deep(p) { margin: 0.4em 0; }
.msg-body :deep(p:first-child) { margin-top: 0; }
.msg-body :deep(p:last-child) { margin-bottom: 0; }
.msg-body :deep(h1),
.msg-body :deep(h2),
.msg-body :deep(h3),
.msg-body :deep(h4) { font-weight: 600; margin: 0.8em 0 0.3em; line-height: 1.4; }
.msg-body :deep(h1) { font-size: 1.5em; }
.msg-body :deep(h2) { font-size: 1.25em; }
.msg-body :deep(h3) { font-size: 1.1em; }
.msg-body :deep(ul),
.msg-body :deep(ol) { padding-left: 1.5em; margin: 0.4em 0; }
.msg-body :deep(li) { margin: 0.15em 0; }
.msg-body :deep(ul) { list-style: disc; }
.msg-body :deep(ol) { list-style: decimal; }
.msg-body :deep(blockquote) {
  border-left: 3px solid var(--color-line, #ddd); padding-left: 0.8em;
  margin: 0.5em 0; color: var(--color-t2, #666);
}
.msg-body :deep(code) {
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 0.85em; padding: 0.15em 0.35em; border-radius: 4px;
  background: var(--color-bg3, #f0f0f0);
}
.msg-body :deep(pre) {
  margin: 0.5em 0; padding: 0.75em 1em; border-radius: 8px;
  background: var(--color-bg3, #f0f0f0); overflow-x: auto;
  font-size: 0.82em; line-height: 1.5;
}
.msg-body :deep(pre code) {
  padding: 0; background: none; font-size: inherit;
}
.msg-body :deep(table) {
  border-collapse: collapse; margin: 0.5em 0; font-size: 0.9em; width: auto;
}
.msg-body :deep(th),
.msg-body :deep(td) {
  border: 1px solid var(--color-line, #ddd); padding: 0.35em 0.65em; text-align: left;
}
.msg-body :deep(th) { background: var(--color-bg3, #f0f0f0); font-weight: 600; }
.msg-body :deep(hr) { border: none; border-top: 1px solid var(--color-line, #ddd); margin: 0.8em 0; }
.msg-body :deep(a) { color: var(--color-brand, #3d3d3d); text-decoration: underline; }
.msg-body :deep(strong) { font-weight: 600; }
.msg-body :deep(img) { max-width: 100%; border-radius: 6px; }

/* KaTeX overrides */
.msg-body :deep(.katex-display) { margin: 0.5em 0; overflow-x: auto; }
.msg-body :deep(.katex) { font-size: 1em; }

/* cite-tag lives inside v-html, so needs :deep() under scoped styles */
.msg-body :deep(.cite-tag) {
  display: inline; padding: 1px 5px; margin: 0 1px; border-radius: 4px;
  font-size: 10px; font-weight: 600;
  color: var(--color-brand, #3d3d3d); background: var(--color-bg3, #f0f0f0);
  cursor: pointer; transition: background .15s;
}
.msg-body :deep(.cite-tag:hover) { background: var(--color-line2, #ddd); }
</style>
