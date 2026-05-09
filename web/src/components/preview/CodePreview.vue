<template>
  <div class="code-preview">
    <div class="code-preview__toolbar">
      <span class="code-preview__lang">{{ langDisplay }}</span>
      <span class="flex-1"></span>
      <span v-if="truncated" class="code-preview__truncated">
        Truncated at {{ truncateBytes / 1024 }} KB — download to see the full file.
      </span>
    </div>
    <div class="code-preview__body">
      <div v-if="loading" class="code-preview__hint">Loading…</div>
      <div v-else-if="error" class="code-preview__hint code-preview__hint--err">
        Couldn't load file: {{ error }}
      </div>
      <div v-else class="code-preview__shiki" v-html="html" />
    </div>
  </div>
</template>

<script setup>
/**
 * Code preview with shiki syntax highlighting.
 *
 * shiki is loaded dynamically — adds ~600KB of grammars + themes that
 * we only want on the wire when the user actually opens a code file.
 * The viewer caches the highlighter on a module-level variable so
 * opening multiple code files in one session loads shiki once.
 *
 * Long files truncate at 256 KB to keep the highlighter snappy and
 * the modal scrollable; the user gets a banner and the unmodified
 * download URL via the modal toolbar.
 *
 * Languages map from extension via a small lookup; unrecognised
 * extensions render as plain text (shiki's 'txt' lang) so the file
 * is still readable, just without colour.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { extFor } from './fileType'

const props = defineProps({
  url: { type: String, required: true },
  filename: { type: String, default: '' },
})

// Truncate cap — large enough that most source files fit whole, small
// enough that 4K screens still feel snappy on highlight.
const truncateBytes = 256 * 1024

const source = ref('')
const html = ref('')
const loading = ref(true)
const error = ref('')
const truncated = ref(false)

// shiki's getHighlighter is heavy. Cache the instance at module scope
// so opening 5 code files in one session doesn't re-init it 5 times.
let _shikiHL = null
let _shikiThemeFor = null
async function getHighlighter() {
  if (_shikiHL) return _shikiHL
  // Dynamic import so the shiki bundle splits out of the main entry
  // chunk — only requested when a code file is previewed.
  const shiki = await import('shiki')
  _shikiHL = await shiki.createHighlighter({
    themes: ['github-dark', 'github-light'],
    langs: [
      'javascript', 'typescript', 'jsx', 'tsx', 'vue', 'svelte',
      'python', 'ruby', 'go', 'rust', 'java', 'kotlin',
      'c', 'cpp', 'csharp', 'php', 'swift',
      'shell', 'bash', 'powershell',
      'sql', 'graphql', 'html', 'css', 'scss',
      'json', 'jsonc', 'yaml', 'toml', 'ini',
      'markdown', 'xml', 'dockerfile',
    ],
  })
  // Pick the theme that matches the user's current colour scheme.
  // We hint shiki via the body's data attribute (set by useTheme
  // composable); fall back to dark since the modal sits on a dark
  // surface in both palettes.
  _shikiThemeFor = (mode) => (mode === 'light' ? 'github-light' : 'github-dark')
  return _shikiHL
}

const langDisplay = computed(() => langFor(props.filename))

const EXT_LANG_MAP = {
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'jsx',
  ts: 'typescript', tsx: 'tsx',
  py: 'python',
  rb: 'ruby',
  go: 'go',
  rs: 'rust',
  java: 'java',
  kt: 'kotlin',
  c: 'c', h: 'c',
  cc: 'cpp', cpp: 'cpp', cxx: 'cpp', hpp: 'cpp', hxx: 'cpp',
  cs: 'csharp', fs: 'csharp',
  php: 'php',
  swift: 'swift',
  sh: 'shell', bash: 'bash', zsh: 'bash', fish: 'shell',
  ps1: 'powershell',
  sql: 'sql', graphql: 'graphql', gql: 'graphql',
  vue: 'vue', svelte: 'svelte',
  html: 'html', htm: 'html',
  css: 'css', scss: 'scss', sass: 'scss', less: 'css',
  json: 'json', jsonc: 'jsonc',
  yaml: 'yaml', yml: 'yaml',
  toml: 'toml', ini: 'ini', cfg: 'ini', conf: 'ini', env: 'shell',
  md: 'markdown', markdown: 'markdown',
  xml: 'xml',
  dockerfile: 'dockerfile',
  txt: 'text', log: 'text', rst: 'text', tex: 'text',
}

function langFor(name) {
  const ext = extFor(name)
  return EXT_LANG_MAP[ext] || 'text'
}

async function load() {
  loading.value = true
  error.value = ''
  truncated.value = false
  try {
    const r = await fetch(props.url, { credentials: 'include' })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    let text = await r.text()
    if (text.length > truncateBytes) {
      text = text.slice(0, truncateBytes)
      truncated.value = true
    }
    source.value = text

    const lang = langFor(props.filename)
    if (lang === 'text') {
      // Shiki accepts 'text' but skipping the highlighter for plain
      // text saves the round-trip — render an escaped <pre> directly.
      html.value = `<pre class="shiki-fallback">${escapeHtml(text)}</pre>`
    } else {
      const hl = await getHighlighter()
      // Match the surrounding theme. Body-level dark/light data attr
      // is set by useTheme; default to dark when missing.
      const isDark = !document.documentElement.classList.contains('theme-light')
      html.value = hl.codeToHtml(text, {
        lang,
        theme: _shikiThemeFor(isDark ? 'dark' : 'light'),
      })
    }
  } catch (e) {
    error.value = e?.message || String(e)
    source.value = ''
    html.value = ''
  } finally {
    loading.value = false
  }
}

function escapeHtml(s) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

onMounted(load)
watch(() => props.url, () => { load() })
</script>

<style scoped>
.code-preview {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  background: var(--color-bg);
}
.code-preview__toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
  flex-shrink: 0;
  font-size: 11px;
  color: var(--color-t3);
}
.code-preview__lang {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--color-t2);
}
.code-preview__truncated {
  font-size: 10.5px;
  color: var(--color-warn-fg, #b45309);
}

.code-preview__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}
.code-preview__hint {
  font-size: 12px;
  color: var(--color-t3);
  text-align: center;
  padding: 32px 16px;
}
.code-preview__hint--err { color: #ef4444; }

/* shiki ships its own ``<pre class="shiki">`` with inline styles for
   colours; we just position + size it. Keep our default font + line
   spacing tight enough that long files don't feel airy. */
.code-preview__shiki :deep(pre) {
  margin: 0;
  padding: 16px 20px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre;
  overflow: visible;       /* parent scrolls — no double scrollbars */
}
.code-preview__shiki :deep(.shiki-fallback) {
  color: var(--color-t1);
  background: var(--color-bg);
}
</style>
