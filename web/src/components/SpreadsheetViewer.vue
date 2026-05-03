<!--
  SpreadsheetViewer — viewer for spreadsheet-as-document uploads
  (.xlsx / .csv / .tsv).

  Spreadsheet docs are parsed into one ``BlockType.TABLE`` block per
  sheet (the ``SpreadsheetBackend`` invariant). Each TABLE block
  carries:
    * ``table_markdown`` — the raw rendered markdown grid (full data,
      bounded by ``SPREADSHEET_MAX_CELLS`` at upload time)
    * ``text`` — the LLM description that's actually embedded /
      retrieved (and the sole input to the description-only KG
      ``entity_type=TABLE`` injection)

  We render the description as a header strip (so users see what the
  retrieval system sees) and the markdown as a real HTML table below.
  Switching sheets via the tab strip is a pure client-side filter on
  ``allBlocks`` — no extra API call.

  Drops the bbox / page / highlight idiom because there's no spatial
  layout to highlight (TABLE bbox is the sentinel ``(0,0,0,0)``).
-->
<script setup>
import { computed, ref, watch } from 'vue'
import { Download } from 'lucide-vue-next'

const props = defineProps({
  // The TABLE blocks for this doc, ordered by ``page_no``. Empty array
  // is valid (e.g. parse hasn't completed yet) — renders an empty state.
  tableBlocks: { type: Array, required: true },
  // Optional ``page_no -> sheet name`` map sourced from
  // ``DocumentOut.pages``. Falls back to ``Sheet N`` when missing.
  sheetNames: { type: Object, default: () => ({}) },
  // Original-file download URL — the viewer mirrors PdfViewer / ImageViewer
  // by surfacing a download affordance for the source bytes.
  downloadUrl: { type: String, default: '' },
  filename: { type: String, default: '' },
})

const activeIdx = ref(0)
// Reset to sheet 0 when the doc changes (props.tableBlocks identity flip).
watch(
  () => props.tableBlocks,
  () => { activeIdx.value = 0 },
)

const activeBlock = computed(() => props.tableBlocks[activeIdx.value] || null)

function sheetLabel(blk, i) {
  if (!blk) return `Sheet ${i + 1}`
  return props.sheetNames[blk.page_no] || `Sheet ${blk.page_no || i + 1}`
}

// ── Markdown table → HTML ────────────────────────────────────────
// Tiny parser for the GFM table format ``SpreadsheetBackend`` emits.
// Format invariants from the backend:
//   1st line: ``| h1 | h2 | ... |``
//   2nd line: ``| --- | --- | ... |`` (separator)
//   Nth line: ``| v1 | v2 | ... |``
// Anything before the first ``|`` line is ignored (caption / blank).
//
// Cell content escapes (mirror of ``parser.backends.spreadsheet._escape``):
//   literal ``\``  →  ``\\``  in markdown
//   literal ``|``  →  ``\|``  in markdown
// We must split on **un-escaped** pipes only, then unescape each cell —
// otherwise cells containing ``|`` would split into extra columns and
// cells containing ``\`` would render with an extra backslash visible.
function parseMarkdownTable(md) {
  if (!md || typeof md !== 'string') return { headers: [], rows: [] }
  const lines = md.split(/\r?\n/).map((l) => l.trim()).filter(Boolean)
  // Find the first row that starts with ``|`` — skips caption text.
  const start = lines.findIndex((l) => l.startsWith('|'))
  if (start < 0) return { headers: [], rows: [] }
  const tableLines = lines.slice(start).filter((l) => l.startsWith('|'))
  if (tableLines.length < 2) return { headers: [], rows: [] }

  // Split on a ``|`` that's NOT preceded by an odd number of ``\``.
  // Walk the string once, counting consecutive backslashes — this
  // sidesteps the lookbehind-compatibility question and handles
  // ``\\|`` correctly (escaped backslash + unescaped pipe = split).
  const splitRow = (line) => {
    const inner = line.replace(/^\|/, '').replace(/\|\s*$/, '')
    const cells = []
    let buf = ''
    let bs = 0  // running count of trailing backslashes
    for (let i = 0; i < inner.length; i++) {
      const ch = inner[i]
      if (ch === '\\') {
        buf += ch
        bs += 1
        continue
      }
      if (ch === '|' && bs % 2 === 0) {
        cells.push(buf)
        buf = ''
        bs = 0
        continue
      }
      buf += ch
      bs = 0
    }
    cells.push(buf)
    // Unescape: ``\\`` → ``\`` and ``\|`` → ``|``. Order matters —
    // do the pipe-unescape first so ``\\\|`` (escaped backslash +
    // escaped pipe) becomes ``\|`` (literal backslash + literal pipe)
    // not ``||`` (which would re-introduce a structural pipe).
    return cells.map((c) => c.trim().replace(/\\\|/g, '|').replace(/\\\\/g, '\\'))
  }

  const headers = splitRow(tableLines[0])
  // Line 1 is the separator (``--- | ---``); skip it.
  const rows = tableLines.slice(2).map(splitRow)
  return { headers, rows }
}

const parsedTable = computed(() => {
  const md = activeBlock.value?.table_markdown || ''
  return parseMarkdownTable(md)
})

const description = computed(() => activeBlock.value?.text || '')

const dl = computed(() => props.downloadUrl || '')
</script>

<template>
  <div class="sheet-viewer">
    <!-- Toolbar — sheet tabs + download. Mirrors PdfViewer / ImageViewer
         placement so DocDetail toolbars are consistent across formats. -->
    <div class="sv-toolbar">
      <div class="sv-tabs" role="tablist">
        <button
          v-for="(blk, i) in tableBlocks"
          :key="blk.block_id || i"
          class="sv-tab"
          :class="{ 'sv-tab--active': i === activeIdx }"
          role="tab"
          :aria-selected="i === activeIdx"
          @click="activeIdx = i"
        >
          {{ sheetLabel(blk, i) }}
        </button>
      </div>
      <a
        v-if="dl"
        class="sv-btn"
        :href="dl"
        :download="filename || true"
        title="Download original"
      >
        <Download :size="14" :stroke-width="1.6" />
      </a>
    </div>

    <!-- Description strip — what the retrieval layer actually sees. -->
    <div v-if="description" class="sv-desc">
      <span class="sv-desc-label">DESCRIPTION</span>
      <p class="sv-desc-text">{{ description }}</p>
    </div>

    <!-- Table. Scrolls both axes; sticky header for tall sheets. -->
    <div class="sv-stage">
      <div v-if="!activeBlock" class="sv-empty">No sheets parsed yet.</div>
      <div
        v-else-if="!parsedTable.headers.length && !parsedTable.rows.length"
        class="sv-empty"
      >
        Empty sheet.
      </div>
      <table v-else class="sv-table">
        <thead>
          <tr>
            <th v-for="(h, ci) in parsedTable.headers" :key="ci">{{ h }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, ri) in parsedTable.rows" :key="ri">
            <td v-for="(cell, ci) in row" :key="ci">{{ cell }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.sheet-viewer {
  position: relative;
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  background: var(--color-bg2);
  overflow: hidden;
}

.sv-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg);
  flex: 0 0 auto;
}

.sv-tabs {
  display: flex;
  gap: 2px;
  flex: 1 1 auto;
  overflow-x: auto;
  scrollbar-width: thin;
}

.sv-tab {
  padding: 4px 10px;
  font-size: 12px;
  font-family: var(--font-mono, ui-monospace, monospace);
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.1s, color 0.1s, border 0.1s;
}

.sv-tab:hover {
  background: var(--color-bg2);
  color: var(--color-t1);
}

.sv-tab--active {
  color: var(--color-t1);
  background: var(--color-bg2);
  border-color: var(--color-line);
}

.sv-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: var(--color-t2);
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.1s, color 0.1s;
  text-decoration: none;
  flex: 0 0 auto;
}

.sv-btn:hover {
  background: var(--color-bg2);
  color: var(--color-t1);
}

.sv-desc {
  padding: 8px 12px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg);
  flex: 0 0 auto;
}

.sv-desc-label {
  display: inline-block;
  font-size: 9px;
  font-family: var(--font-mono, ui-monospace, monospace);
  color: var(--color-t3);
  letter-spacing: 0.06em;
  margin-right: 8px;
}

.sv-desc-text {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-t1);
  white-space: pre-wrap;
}

.sv-stage {
  flex: 1 1 auto;
  overflow: auto;
  padding: 8px;
}

.sv-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--color-t3);
  font-size: 12px;
}

.sv-table {
  border-collapse: collapse;
  font-size: 12px;
  font-family: var(--font-mono, ui-monospace, monospace);
  background: var(--color-bg);
  /* No fixed width: let the table grow horizontally so wide sheets
     scroll inside ``.sv-stage`` rather than being squashed. */
}

.sv-table th,
.sv-table td {
  border: 1px solid var(--color-line);
  padding: 4px 8px;
  text-align: left;
  vertical-align: top;
  white-space: nowrap;
  color: var(--color-t1);
  /* Cap individual cell widths so a freak 5kB cell doesn't blow out
     the table; full content stays available via cell hover/click. */
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sv-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--color-bg2);
  color: var(--color-t1);
  font-weight: 600;
}

.sv-table tbody tr:nth-child(even) {
  background: var(--color-bg2);
}

.sv-table tbody tr:hover {
  background: var(--color-line);
}
</style>
