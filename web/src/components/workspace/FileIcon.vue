<template>
  <img
    :src="iconUrl"
    :alt="alt"
    :width="size"
    :height="size"
    class="file-icon"
    draggable="false"
  />
</template>

<script setup>
/**
 * FileIcon — coloured PNG icons sourced from ``public/file_icons/``.
 *
 * Two variants per type, selected via the ``variant`` prop:
 *   * ``normal`` (default) — used in list-view rows where the icon
 *     sits next to text; renders well at 16-32 px.
 *   * ``jumbo``  — used in tile / grid rows where the icon is the
 *     focal element; renders well at 36-64 px.
 *
 * Filename → typeKey resolves the extension against the icon set we
 * actually ship in ``public/file_icons/``. Unknown extensions
 * (and folders that somehow miss the ``kind === 'folder'`` short
 * circuit) fall back to ``unknown(_jumbo).png``.
 *
 * Why ``<img>`` and not the previous inline SVG: these icons are
 * full-colour bitmaps; ``currentColor`` doesn't apply, and the
 * <img> tag is the simplest path that lets the browser cache + scale
 * + alt-text the asset for free. The Phosphor monochrome SVGs that
 * lived in ``src/assets/file-icons/`` are no longer referenced and
 * can be deleted in a follow-up.
 *
 * Props:
 *   kind     — 'folder' | 'file'         (default 'file')
 *   name     — filename (extension extracted internally; ignored if folder)
 *   size     — px, drives both width & height (default 24)
 *   variant  — 'normal' | 'jumbo'        (default 'normal')
 */
import { computed } from 'vue'

const props = defineProps({
  kind: { type: String, default: 'file' },
  name: { type: String, default: '' },
  size: { type: [Number, String], default: 24 },
  variant: { type: String, default: 'normal' },
})

// Filename extension → icon key in ``public/file_icons/``. Names are
// generic on purpose so each glyph covers a family (the ``text`` icon
// is the catch-all for plain-text-y files, ``archive`` for any
// compressed bundle, etc.). Anything not mapped falls through to
// ``unknown``.
const _EXT_TO_TYPE = {
  // Spreadsheets
  csv: 'csv', tsv: 'csv',
  xls: 'xlsx', xlsx: 'xlsx', ods: 'xlsx', numbers: 'xlsx',
  // Word docs
  doc: 'doc',
  docx: 'docx', rtf: 'docx', odt: 'docx', pages: 'docx',
  // Slides
  ppt: 'pptx', pptx: 'pptx', key: 'pptx',
  // Microsoft Publisher
  pub: 'pub',
  // PDFs
  pdf: 'pdf',
  // Python
  py: 'py',
  // Plain-text family — markdown, plain, log, rst, source code with
  // no dedicated icon, etc. Loose definition on purpose: the icon
  // reads as "human-readable text", so anything text-shaped falls
  // here unless a more specific icon exists.
  txt: 'text', log: 'text', rst: 'text',
  md: 'text', markdown: 'text', mdown: 'text', mkdn: 'text',
  // Source code without a per-language icon → text icon.
  // (py keeps its own icon; everything else collapses.)
  js: 'text', mjs: 'text', cjs: 'text', ts: 'text', tsx: 'text', jsx: 'text',
  go: 'text', rs: 'text', java: 'text', c: 'text', cpp: 'text', h: 'text',
  sh: 'text', bash: 'text', zsh: 'text', sql: 'text',
  html: 'text', htm: 'text', css: 'text', scss: 'text', less: 'text',
  vue: 'text',
  // Structured data — JSON / YAML / XML share the json glyph.
  json: 'json', yaml: 'json', yml: 'json', xml: 'json',
  // Configuration — INI / TOML / .env / dotfile-style configs.
  ini: 'config', conf: 'config', cfg: 'config', toml: 'config', env: 'config',
  // Databases
  accdb: 'database', mdb: 'database',
  db: 'database', sqlite: 'database', sqlite3: 'database',
  // Archives — every compressed bundle collapses onto the archive icon.
  tar: 'archive', gz: 'archive', tgz: 'archive', zip: 'archive',
  '7z': 'archive', rar: 'archive', bz2: 'archive', xz: 'archive',
}

const typeKey = computed(() => {
  if (props.kind === 'folder') return 'folder'
  const ext = (props.name || '').toLowerCase().replace(/^.*\./, '')
  return _EXT_TO_TYPE[ext] || 'unknown'
})

const iconUrl = computed(() => {
  const suffix = props.variant === 'jumbo' ? '_jumbo' : ''
  return `/file_icons/${typeKey.value}${suffix}.png`
})

const alt = computed(() => (props.kind === 'folder' ? 'Folder' : typeKey.value))
</script>

<style scoped>
.file-icon {
  display: inline-block;
  flex-shrink: 0;
  object-fit: contain;
  user-select: none;
}
</style>
