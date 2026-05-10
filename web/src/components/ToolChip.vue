<script setup>
/**
 * Inline tool-call chip — one chip per tool dispatch, Claude-Code
 * style. Folded state hints at WHAT got called (one verb-and-object
 * line); expanded state shows the call's input / output rendered
 * type-aware:
 *
 *   Bash       headline ``$ <command>`` · expand → stdout/stderr block
 *   Write      headline ``Wrote <path>`` · expand → file path link +
 *              the content body (or new content for Edit)
 *   Edit       headline ``Edited <path>`` · expand → unified diff
 *              built from ``old_string`` / ``new_string``
 *   Read       headline ``Read <path>`` · expand → file body
 *   Glob/Grep  headline ``Pattern <p>`` · expand → match list
 *   search_*   headline `` <query>`` · expand → query + hits dump
 *   anything   else falls back to raw JSON input + text output blocks
 *
 * Filesystem-shaped paths (``/workdir/...`` plus the explicit
 * ``file_path`` / ``path`` props) render as clickable links that open
 * the workdir preview modal — the user's "interim artifact path
 * should be clickable" requirement.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { ChevronRight } from 'lucide-vue-next'
import ThinkingPulse from './ThinkingPulse.vue'

const props = defineProps({
  tool: { type: Object, required: true },
})

const { t } = useI18n()
const router = useRouter()

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

// ── Tool-family classification ───────────────────────────────────
// Each family gets its own rendering recipe; the catch-all "generic"
// just dumps input JSON + output text, same as the previous pass.
const family = computed(() => {
  const n = props.tool.name || ''
  if (n === 'Bash') return 'bash'
  if (n === 'Write') return 'write'
  if (n === 'Edit') return 'edit'
  if (n === 'Read') return 'read'
  if (n === 'Glob' || n === 'Grep') return 'pattern'
  if (n.startsWith('search_') || n === 'graph_explore' || n === 'web_search' || n === 'rerank') return 'search'
  if (n === 'read_chunk' || n === 'read_tree' || n === 'list_folders' || n === 'list_docs') return 'rag-read'
  return 'generic'
})

const inp = computed(() => props.tool.input || {})
const out = computed(() => props.tool.output || '')

// ── Folded headline ──────────────────────────────────────────────
// Verb-and-object string the user sees before clicking. "Read
// /workdir/x.md" is more informative than the raw "Read" the
// previous chip showed. Falls back to the legacy ``detail`` field
// for trace rows persisted before phase2(agent) — those have a
// 64-char detail snippet but no structured ``input`` dict yet.
const headline = computed(() => {
  const i = inp.value
  let fromInput = ''
  switch (family.value) {
    case 'bash':
      fromInput = i.command || ''
      break
    case 'write':
    case 'edit':
    case 'read':
      fromInput = i.file_path || i.path || ''
      break
    case 'pattern':
      fromInput = i.pattern || ''
      break
    case 'search':
      fromInput = i.query || ''
      break
    case 'rag-read':
      fromInput = i.chunk_id || i.doc_id || i.path || ''
      break
  }
  return fromInput || props.tool.detail || ''
})

const headlineMono = computed(() =>
  family.value === 'bash' || family.value === 'write' ||
  family.value === 'edit' || family.value === 'read' ||
  family.value === 'pattern' || family.value === 'rag-read',
)

// ── Path detection + click handler ───────────────────────────────
// Workdir paths come in two flavours: backend-side ``/workdir/foo``
// (the sandbox-internal absolute), or frontend-side ``/foo`` which
// is what the workspace UI talks. Strip the ``/workdir`` prefix
// when present so the workspace view's ``?path=`` query lands on
// the right folder.
function normaliseWorkdirPath(p) {
  if (typeof p !== 'string') return ''
  let s = p.trim()
  if (!s) return ''
  if (s.startsWith('/workdir')) s = s.slice('/workdir'.length) || '/'
  return s.startsWith('/') ? s : '/' + s
}
function openInWorkspace(p) {
  const n = normaliseWorkdirPath(p)
  if (!n) return
  // Folder path → workspace at that folder; file path → workspace
  // at the parent and let the user click into preview. Cheap
  // heuristic: trailing slash or no extension treated as folder.
  const isFile = /\.[^/]+$/.test(n) && !n.endsWith('/')
  const target = isFile ? n.replace(/\/[^/]+$/, '') || '/' : n
  router.push({ path: '/workspace', query: target === '/' ? {} : { path: target } })
}

// ── Diff builder for Edit ────────────────────────────────────────
// We don't have the actual unified diff, just old + new strings.
// Render line-prefixed +/- side-by-side so the user can see what
// changed. Simple enough; for a real full-file diff (git-blame
// quality) we'd reach for ``diff`` (npm) but the agent's edits are
// usually small enough that the eyeball comparison works.
const diffLines = computed(() => {
  if (family.value !== 'edit') return []
  const o = String(inp.value.old_string || '').split('\n')
  const n = String(inp.value.new_string || '').split('\n')
  const out = []
  for (const line of o) out.push({ kind: 'remove', text: line })
  for (const line of n) out.push({ kind: 'add', text: line })
  return out
})

// ── Pretty input JSON for the generic fallback path ──────────────
const inputJson = computed(() => {
  const i = inp.value
  if (i == null || (typeof i === 'object' && !Object.keys(i).length)) return ''
  try {
    return JSON.stringify(i, null, 2)
  } catch {
    return String(i)
  }
})

const running = computed(() => props.tool.status === 'running')
const expanded = ref(false)
function toggle() { expanded.value = !expanded.value }

// "Anything to render?" — the family-specific blocks each handle
// their own ``v-if`` so absent fields are silently skipped. When
// none of them have content (and the legacy ``detail`` is also
// empty) we fall through to the "no captured payload" hint. Used
// from the template to gate that hint so it doesn't co-render with
// e.g. a Bash command line that came from the ``detail`` fallback.
const hasAnyDetail = computed(() => Boolean(
  inputJson.value
    || out.value
    || props.tool.detail
    || diffLines.value.length,
))
</script>

<template>
  <div class="tool-chip" :class="{ 'is-expanded': expanded, 'is-running': running }">
    <button class="chip-head" @click="toggle">
      <ThinkingPulse v-if="running" :size="14" class="head-icon" />
      <ChevronRight v-else :size="12" :stroke-width="1.75"
        class="head-icon chev" :class="{ 'rotate-90': expanded }" />
      <span class="head-name">{{ toolLabel }}</span>
      <span
        v-if="headline"
        class="head-detail"
        :class="{ 'head-detail--mono': headlineMono }"
      >{{ headline }}</span>
      <span v-if="tool.summary" class="head-summary">· {{ tool.summary }}</span>
    </button>

    <div v-if="expanded" class="chip-body">
      <!-- Bash: $ command + stdout. The first ``v-if`` falls back to
           ``tool.detail`` so trace rows persisted before
           phase2(agent) (no structured ``input`` field yet) still
           show the command line, just without the captured stdout. -->
      <template v-if="family === 'bash'">
        <div v-if="inp.command || tool.detail" class="chip-block">
          <div class="chip-block__label">Command</div>
          <pre class="chip-block__pre"><code><span class="prompt">$ </span>{{ inp.command || tool.detail }}</code></pre>
        </div>
        <div v-if="out" class="chip-block">
          <div class="chip-block__label">Output</div>
          <pre class="chip-block__pre"><code>{{ out }}</code></pre>
        </div>
      </template>

      <!-- Write: clickable path + content body -->
      <template v-else-if="family === 'write'">
        <div v-if="inp.file_path" class="chip-block">
          <div class="chip-block__label">File</div>
          <button class="chip-path" @click="openInWorkspace(inp.file_path)">{{ inp.file_path }}</button>
        </div>
        <div v-if="inp.content" class="chip-block">
          <div class="chip-block__label">Content</div>
          <pre class="chip-block__pre"><code>{{ inp.content }}</code></pre>
        </div>
      </template>

      <!-- Edit: clickable path + unified-ish diff -->
      <template v-else-if="family === 'edit'">
        <div v-if="inp.file_path" class="chip-block">
          <div class="chip-block__label">File</div>
          <button class="chip-path" @click="openInWorkspace(inp.file_path)">{{ inp.file_path }}</button>
        </div>
        <div v-if="diffLines.length" class="chip-block">
          <div class="chip-block__label">Diff</div>
          <pre class="chip-block__pre chip-diff"><code><span
              v-for="(l, i) in diffLines"
              :key="i"
              class="diff-line"
              :class="'diff-line--' + l.kind"
            >{{ l.kind === 'add' ? '+ ' : '- ' }}{{ l.text }}
</span></code></pre>
        </div>
      </template>

      <!-- Read: clickable path + body -->
      <template v-else-if="family === 'read'">
        <div v-if="inp.file_path || inp.path" class="chip-block">
          <div class="chip-block__label">File</div>
          <button
            class="chip-path"
            @click="openInWorkspace(inp.file_path || inp.path)"
          >{{ inp.file_path || inp.path }}</button>
        </div>
        <div v-if="out" class="chip-block">
          <div class="chip-block__label">Body</div>
          <pre class="chip-block__pre"><code>{{ out }}</code></pre>
        </div>
      </template>

      <!-- Glob / Grep: pattern + matches -->
      <template v-else-if="family === 'pattern'">
        <div v-if="inp.pattern" class="chip-block">
          <div class="chip-block__label">Pattern</div>
          <pre class="chip-block__pre"><code>{{ inp.pattern }}</code></pre>
        </div>
        <div v-if="inp.path" class="chip-block">
          <div class="chip-block__label">In</div>
          <button class="chip-path" @click="openInWorkspace(inp.path)">{{ inp.path }}</button>
        </div>
        <div v-if="out" class="chip-block">
          <div class="chip-block__label">Matches</div>
          <pre class="chip-block__pre"><code>{{ out }}</code></pre>
        </div>
      </template>

      <!-- search_* / graph_explore / web_search / rerank -->
      <template v-else-if="family === 'search'">
        <div v-if="inp.query" class="chip-block">
          <div class="chip-block__label">Query</div>
          <pre class="chip-block__pre"><code>{{ inp.query }}</code></pre>
        </div>
        <div v-if="out" class="chip-block">
          <div class="chip-block__label">Hits</div>
          <pre class="chip-block__pre"><code>{{ out }}</code></pre>
        </div>
      </template>

      <!-- read_chunk / read_tree / list_* — RAG navigation -->
      <template v-else-if="family === 'rag-read'">
        <div v-if="inputJson" class="chip-block">
          <div class="chip-block__label">Input</div>
          <pre class="chip-block__pre"><code>{{ inputJson }}</code></pre>
        </div>
        <div v-if="out" class="chip-block">
          <div class="chip-block__label">Result</div>
          <pre class="chip-block__pre"><code>{{ out }}</code></pre>
        </div>
      </template>

      <!-- Catch-all: raw JSON input + raw text output -->
      <template v-else>
        <div v-if="inputJson" class="chip-block">
          <div class="chip-block__label">Input</div>
          <pre class="chip-block__pre"><code>{{ inputJson }}</code></pre>
        </div>
        <div v-if="out" class="chip-block">
          <div class="chip-block__label">Output</div>
          <pre class="chip-block__pre"><code>{{ out }}</code></pre>
        </div>
      </template>

      <!-- "no payload" hint only when EVERY potential render path
           comes up empty — including the legacy ``tool.detail``
           snippet that the Bash branch falls back to. Without this
           the chip showed "$ pwd" alongside "(no captured payload)"
           on old trace rows, which read as a contradiction. -->
      <div v-if="!hasAnyDetail" class="chip-block__empty">
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
  color: var(--color-t3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 360px;
}
.head-detail--mono {
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 0.6875rem;
}
.head-summary {
  color: var(--color-t3);
  font-size: 0.6875rem;
  white-space: nowrap;
}

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

/* Bash command prompt — subtle ``$`` prefix */
.prompt {
  color: var(--color-t3);
  user-select: none;
}

/* Path button — looks like a link, opens the workspace at that path */
.chip-path {
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 0.6875rem;
  color: var(--color-brand);
  background: transparent;
  border: none;
  padding: 0;
  text-align: left;
  cursor: pointer;
  text-decoration: underline;
  text-decoration-color: color-mix(in srgb, var(--color-brand) 35%, transparent);
  text-underline-offset: 2px;
  word-break: break-all;
}
.chip-path:hover {
  text-decoration-color: var(--color-brand);
}

/* Diff coloring */
.chip-diff { padding: 4px 8px; }
.diff-line {
  display: block;
  white-space: pre;
}
.diff-line--add {
  background: color-mix(in srgb, var(--color-ok-fg) 12%, transparent);
  color: var(--color-ok-fg);
}
.diff-line--remove {
  background: color-mix(in srgb, var(--color-err-fg) 12%, transparent);
  color: var(--color-err-fg);
}
</style>
