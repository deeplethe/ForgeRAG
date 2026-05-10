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
import { structuredPatch } from 'diff'
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
// Real Myers diff via the ``diff`` package — gives us a unified-style
// hunk list with shared ``context`` lines properly identified, so
// the rendered diff looks like the GitHub PR view (gutter line
// numbers, ``@@ ... @@`` hunk headers, only changed regions
// shown).
//
// ``old_string`` / ``new_string`` are the two strings the model
// passed to the Edit tool. They're not full files — the SDK passes
// only the targeted snippet — so the line numbers in the hunk
// headers are local to that snippet (start at 1), which is the
// honest thing to show: we don't have the surrounding file
// context, so showing absolute line numbers would lie.
const diffHunks = computed(() => {
  if (family.value !== 'edit') return []
  const o = String(inp.value.old_string || '')
  const n = String(inp.value.new_string || '')
  if (!o && !n) return []
  let patch
  try {
    patch = structuredPatch('a', 'b', o, n, '', '', { context: 3 })
  } catch {
    return []
  }
  const hunks = patch?.hunks || []
  // Pre-compute the (oldNum, newNum, sign, content) tuple per row
  // so the template can render each row without an inline counter.
  return hunks.map((h) => {
    let oldN = h.oldStart
    let newN = h.newStart
    const rows = []
    for (const line of (h.lines || [])) {
      const sign = line[0]
      const content = line.slice(1)
      if (sign === '+') {
        rows.push({ sign, content, oldNum: '', newNum: String(newN) })
        newN++
      } else if (sign === '-') {
        rows.push({ sign, content, oldNum: String(oldN), newNum: '' })
        oldN++
      } else {
        // ' ' context (or '\\' for "no newline at end of file" — treat as context)
        rows.push({ sign, content, oldNum: String(oldN), newNum: String(newN) })
        oldN++
        newN++
      }
    }
    return { ...h, rows }
  })
})

// Precise +N -M stat from the diff hunks — counts actual added /
// removed lines (skips the ``context`` lines that are unchanged
// on both sides). Falls back to "" when the entry isn't an Edit.
const editStat = computed(() => {
  if (family.value !== 'edit') return ''
  let plus = 0
  let minus = 0
  for (const h of diffHunks.value) {
    for (const line of h.lines || []) {
      if (line.startsWith('+')) plus++
      else if (line.startsWith('-')) minus++
    }
  }
  if (!plus && !minus) return ''
  return `+${plus} -${minus}`
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
    || diffHunks.value.length,
))
</script>

<template>
  <div class="tool-chip" :class="{ 'is-expanded': expanded, 'is-running': running }">
    <button class="chip-head" @click="toggle">
      <span class="head-name">{{ toolLabel }}</span>
      <span
        v-if="headline"
        class="head-detail"
        :class="{ 'head-detail--mono': headlineMono }"
      >{{ headline }}</span>
      <span v-if="editStat" class="head-stat">{{ editStat }}</span>
      <span v-if="tool.summary" class="head-summary">· {{ tool.summary }}</span>
      <!-- Chevron at the trailing edge so content reads left-to-right
           without the disclosure widget interrupting the verb-then-
           object scan. macOS finder-style. -->
      <ThinkingPulse v-if="running" :size="14" class="head-icon head-icon--end" />
      <ChevronRight v-else :size="12" :stroke-width="1.75"
        class="head-icon head-icon--end chev" :class="{ 'rotate-90': expanded }" />
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

      <!-- Edit: clickable path + GitHub-style unified diff. Each
           hunk gets its own ``@@ ... @@`` header, two gutter columns
           with old/new line numbers, and ``+`` / ``-`` / context
           rendering. ``old_string`` / ``new_string`` are snippets
           (not whole files) so the line numbers are local — they
           start at 1 in the snippet's coordinate space. -->
      <template v-else-if="family === 'edit'">
        <div v-if="inp.file_path" class="chip-block">
          <div class="chip-block__label">File</div>
          <button class="chip-path" @click="openInWorkspace(inp.file_path)">{{ inp.file_path }}</button>
        </div>
        <div v-if="diffHunks.length" class="chip-block">
          <div class="chip-block__label">Diff</div>
          <div class="diff-table">
            <template v-for="(h, hi) in diffHunks" :key="hi">
              <div class="diff-hunk-header">@@ -{{ h.oldStart }},{{ h.oldLines }} +{{ h.newStart }},{{ h.newLines }} @@</div>
              <div
                v-for="(r, ri) in h.rows"
                :key="hi + ':' + ri"
                class="diff-row"
                :class="'diff-row--' + (r.sign === '+' ? 'add' : r.sign === '-' ? 'remove' : 'context')"
              >
                <span class="diff-num">{{ r.oldNum }}</span>
                <span class="diff-num">{{ r.newNum }}</span>
                <span class="diff-sign">{{ r.sign === ' ' ? ' ' : r.sign }}</span>
                <span class="diff-content">{{ r.content }}</span>
              </div>
            </template>
          </div>
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
  margin: 2px 0;
  font-size: 0.75rem;
}
/* Row-style head: no border, no background-fill. Hover lights the
   row to signal "clickable". Reads cleanly inside the ToolGroup
   panel where these chips stack as one continuous block; also
   reads cleanly standing alone when the parent skipped the outer
   group (single-tool batch). */
.chip-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 3px 6px;
  background: transparent;
  border: none;
  border-radius: 4px;
  color: var(--color-t2);
  cursor: pointer;
  transition: background-color .12s;
  text-align: left;
}
/* No hover-bg fill — that read as "this is interactive" too loudly
   and the chevron + chip already says "expandable". Hover just
   nudges the head text/icon to full-strength colour. */
.chip-head:hover .head-name { color: var(--color-t1); }
.chip-head:hover .head-icon { color: var(--color-t2); }
.head-icon {
  flex-shrink: 0;
  color: var(--color-t3);
  transition: transform .15s;
}
.head-icon--end {
  /* Trailing-edge chevron: pin to the right of the row regardless
     of how much content sits before it. */
  margin-left: auto;
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
/* Edit chip's "+N -M" line stat. Coloured like a git diff header
   summary so the additions/removals signal stays even at the chip
   level without making the user expand. */
.head-stat {
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 0.6875rem;
  font-feature-settings: "tnum";
  white-space: nowrap;
  color: var(--color-t3);
}

/* Expanded body sits flush under the row head. The ToolGroup panel
   already provides the surrounding box; an additional left rail
   inside would over-decorate. When the chip is rendered on its own
   (N=1, no panel), the left padding still gives an indented feel
   without a visible line. */
.chip-body {
  margin: 4px 0 8px 0;
  padding: 0 6px 0 24px;
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

/* GitHub-style unified diff. Two gutter columns (old line / new
   line), one sign column, one content column. Hunk header sits on
   its own row with a tinted background — same as the @@ header
   you'd see in a GitHub PR diff. Coloured backgrounds for added /
   removed lines stay subtle; context rows are uncoloured. */
.diff-table {
  font-family: 'IBM Plex Mono', 'SF Mono', 'Consolas', monospace;
  font-size: 0.6875rem;
  line-height: 1.55;
  background: var(--color-bg3);
  border-radius: 6px;
  overflow-x: auto;
  max-height: 480px;
  overflow-y: auto;
}
.diff-hunk-header {
  display: block;
  padding: 4px 10px;
  color: var(--color-t3);
  background: color-mix(in srgb, var(--color-t3) 10%, transparent);
  font-feature-settings: "tnum";
  white-space: pre;
}
.diff-row {
  display: grid;
  grid-template-columns: 36px 36px 14px 1fr;
  white-space: pre;
}
.diff-num {
  color: var(--color-t3);
  font-feature-settings: "tnum";
  text-align: right;
  padding: 0 6px;
  user-select: none;
  border-right: 1px solid color-mix(in srgb, var(--color-t3) 18%, transparent);
}
.diff-sign {
  text-align: center;
  user-select: none;
  color: var(--color-t3);
}
.diff-content { padding-right: 8px; }
.diff-row--add {
  background: color-mix(in srgb, var(--color-ok-fg) 12%, transparent);
}
.diff-row--add .diff-sign,
.diff-row--add .diff-content { color: var(--color-ok-fg); }
.diff-row--remove {
  background: color-mix(in srgb, var(--color-err-fg) 12%, transparent);
}
.diff-row--remove .diff-sign,
.diff-row--remove .diff-content { color: var(--color-err-fg); }
.diff-row--context .diff-content { color: var(--color-t1); }
</style>
