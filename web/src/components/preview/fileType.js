/**
 * Workbench file-type dispatcher.
 *
 * Maps a filename's extension to the preview-component family used
 * by ``FilePreview.vue``. Each family is a discrete viewer (image,
 * video, audio, code, etc.) — they don't share rendering code so a
 * mismatch returns 'unsupported' and the modal shows a download
 * fallback instead of guessing.
 *
 * Adding a new family means:
 *   1. add the extensions to one of the SETs below (or create a new
 *      one + branch),
 *   2. add a ``case`` arm in ``FilePreview.vue``'s template, and
 *   3. drop the new viewer component under ``components/preview/``.
 *
 * Extensions are compared case-insensitively. Files without an
 * extension fall through to 'unsupported'.
 */

const IMAGE_EXTS = new Set([
  'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'svg', 'tif', 'tiff', 'avif',
])
const VIDEO_EXTS = new Set(['mp4', 'webm', 'mov', 'mkv'])
const AUDIO_EXTS = new Set(['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac'])
const MARKDOWN_EXTS = new Set(['md', 'markdown'])
const HTML_EXTS = new Set(['html', 'htm'])
const SPREADSHEET_EXTS = new Set(['csv', 'tsv', 'xlsx', 'xls'])
const DOCX_EXTS = new Set(['docx'])
// Anything text-shaped that isn't markdown / html / spreadsheet
// dispatches to the code viewer (which falls back to plain text
// when the language detector doesn't recognise the extension).
const CODE_EXTS = new Set([
  // languages
  'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx',
  'py', 'rb', 'go', 'rs', 'java', 'kt', 'scala',
  'c', 'cc', 'cpp', 'cxx', 'h', 'hpp', 'hxx',
  'cs', 'fs', 'swift', 'php', 'pl', 'lua', 'r',
  'sh', 'bash', 'zsh', 'fish', 'ps1', 'bat', 'cmd',
  'sql', 'graphql', 'gql', 'vue', 'svelte',
  // markup-ish + config
  'xml', 'json', 'jsonc', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'env',
  'css', 'scss', 'sass', 'less',
  'dockerfile', 'gitignore', 'editorconfig',
  // plain
  'txt', 'log', 'rst', 'tex',
])

export function previewKindFor(filename) {
  if (!filename) return 'unsupported'
  const idx = filename.lastIndexOf('.')
  if (idx < 0) return 'unsupported'
  const ext = filename.slice(idx + 1).toLowerCase()
  if (IMAGE_EXTS.has(ext)) return 'image'
  if (VIDEO_EXTS.has(ext)) return 'video'
  if (AUDIO_EXTS.has(ext)) return 'audio'
  if (ext === 'pdf') return 'pdf'
  if (MARKDOWN_EXTS.has(ext)) return 'markdown'
  if (HTML_EXTS.has(ext)) return 'html'
  if (SPREADSHEET_EXTS.has(ext)) return 'spreadsheet'
  if (DOCX_EXTS.has(ext)) return 'docx'
  if (CODE_EXTS.has(ext)) return 'code'
  return 'unsupported'
}

// Lowercase extension or '' when none. Useful for the code viewer's
// language picker.
export function extFor(filename) {
  if (!filename) return ''
  const idx = filename.lastIndexOf('.')
  return idx < 0 ? '' : filename.slice(idx + 1).toLowerCase()
}
