<template>
  <!-- Folder: keep emoji 📁. Cross-platform inconsistency is real but the
       glyph is universally read; users vetoed every SVG attempt. -->
  <span v-if="kind === 'folder'" class="file-icon-emoji" :style="{ fontSize: emojiSize }">📁</span>

  <!-- File: inline-rendered Phosphor duotone SVG. ``v-html`` so the SVG
       inherits ``color`` from this span. We deliberately keep all file
       icons monochromatic (Vercel-style) — type information is carried
       by the icon *shape*, not colour. Saturated per-type tints felt
       like noise against the otherwise grayscale workspace. -->
  <span
    v-else
    class="file-icon"
    :style="{ width: pxSize, height: pxSize }"
    v-html="svgMarkup"
  />
</template>

<script setup>
/**
 * FileIcon — Phosphor duotone file icons, monochromatic.
 *
 * Vercel-style: type information is carried by icon *shape* alone, not
 * colour. We tried per-type tints (PDF red, DOC blue, etc.) but they
 * read as visual noise against the otherwise grayscale workspace.
 *
 * Format detection runs off the **filename extension**, not the
 * ``Document.format`` field — that field used to get clobbered by
 * the parser's post-conversion view (a ``.md`` document parsed via
 * markdown→PDF would land in DB as ``format='pdf'``). Filename is
 * the user-facing truth; format is parser bookkeeping.
 *
 * Folders intentionally use the ``📁`` emoji — every SVG attempt got
 * rejected and the emoji is universally legible.
 *
 * Props:
 *   kind   — 'folder' | 'file'  (default 'file')
 *   name   — filename (extension extracted internally)
 *   size   — px (default 24)
 *
 * Source SVGs live under ``web/src/assets/file-icons/`` and are pulled
 * in as raw strings via vite's ``?raw`` query so we can inline them
 * (which lets ``currentColor`` inherit from the parent span — ``<img>``
 * tags can't do that, leaving them locked at black).
 *
 * Source: https://phosphoricons.com (MIT). Files are vendored verbatim
 * under ``web/src/assets/file-icons/file-*.svg``.
 */
import { computed } from 'vue'

import filePdf from '@/assets/file-icons/file-pdf.svg?raw'
import fileDoc from '@/assets/file-icons/file-doc.svg?raw'
import fileXls from '@/assets/file-icons/file-xls.svg?raw'
import filePpt from '@/assets/file-icons/file-ppt.svg?raw'
import fileMd from '@/assets/file-icons/file-md.svg?raw'
import fileImage from '@/assets/file-icons/file-image.svg?raw'
import fileCode from '@/assets/file-icons/file-code.svg?raw'
import fileAudio from '@/assets/file-icons/file-audio.svg?raw'
import fileVideo from '@/assets/file-icons/file-video.svg?raw'
import fileArchive from '@/assets/file-icons/file-archive.svg?raw'
import fileHtml from '@/assets/file-icons/file-html.svg?raw'
import fileCss from '@/assets/file-icons/file-css.svg?raw'
import fileJs from '@/assets/file-icons/file-js.svg?raw'
import filePy from '@/assets/file-icons/file-py.svg?raw'
import fileCsv from '@/assets/file-icons/file-csv.svg?raw'
import fileSvg from '@/assets/file-icons/file-svg.svg?raw'
import fileText from '@/assets/file-icons/file-text.svg?raw'
import fileGeneric from '@/assets/file-icons/file.svg?raw'

const props = defineProps({
  kind: { type: String, default: 'file' },
  name: { type: String, default: '' },
  size: { type: [Number, String], default: 24 },
})

// Filename → typeKey, used to look up the right SVG. Order matters
// within a category — explicit extensions win over catch-alls
// (``js`` before ``code``, ``html`` before ``code``).
const _EXT_TO_TYPE = {
  pdf: 'pdf',
  doc: 'doc', docx: 'doc', rtf: 'doc', odt: 'doc', pages: 'doc',
  xls: 'xls', xlsx: 'xls', ods: 'xls', numbers: 'xls',
  csv: 'csv', tsv: 'csv',
  ppt: 'ppt', pptx: 'ppt', key: 'ppt',
  md: 'md', markdown: 'md', mdown: 'md', mkdn: 'md',
  txt: 'text',
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image',
  webp: 'image', bmp: 'image', tiff: 'image', tif: 'image',
  svg: 'svg',
  mp3: 'audio', wav: 'audio', ogg: 'audio', m4a: 'audio', flac: 'audio',
  mp4: 'video', avi: 'video', mov: 'video', mkv: 'video', webm: 'video',
  zip: 'archive', tar: 'archive', gz: 'archive', '7z': 'archive', rar: 'archive',
  html: 'html', htm: 'html',
  css: 'css',
  js: 'js', mjs: 'js', cjs: 'js',
  ts: 'js', tsx: 'js', jsx: 'js',
  py: 'py',
  json: 'code', yaml: 'code', yml: 'code', xml: 'code',
  go: 'code', rs: 'code', java: 'code', c: 'code', cpp: 'code', h: 'code', sh: 'code', sql: 'code',
}

const _TYPE_TO_SVG = {
  pdf: filePdf,
  doc: fileDoc,
  xls: fileXls,
  csv: fileCsv,
  ppt: filePpt,
  md: fileMd,
  text: fileText,
  image: fileImage,
  svg: fileSvg,
  audio: fileAudio,
  video: fileVideo,
  archive: fileArchive,
  html: fileHtml,
  css: fileCss,
  js: fileJs,
  py: filePy,
  code: fileCode,
  generic: fileGeneric,
}

const typeKey = computed(() => {
  const ext = (props.name || '').toLowerCase().replace(/^.*\./, '')
  return _EXT_TO_TYPE[ext] || 'generic'
})

const svgMarkup = computed(() => _TYPE_TO_SVG[typeKey.value] || _TYPE_TO_SVG.generic)
const pxSize = computed(() => `${props.size}px`)
const emojiSize = computed(() => `${Number(props.size) * 0.85}px`)
</script>

<style scoped>
.file-icon-emoji {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
}

.file-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: var(--color-t2);   /* monochrome — shape carries the type signal */
}
.file-icon :deep(svg) {
  width: 100%;
  height: 100%;
  display: block;
}
</style>
