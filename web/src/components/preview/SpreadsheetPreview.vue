<template>
  <div class="ss-preview">
    <div v-if="sheets.length > 1" class="ss-preview__tabs">
      <button
        v-for="(sheet, i) in sheets"
        :key="i"
        class="ss-preview__tab"
        :class="{ 'is-active': i === activeIdx }"
        @click="activeIdx = i"
      >{{ sheet.name }}</button>
      <span class="flex-1"></span>
      <span v-if="truncated" class="ss-preview__truncated">
        Showing first {{ ROW_CAP }} rows
      </span>
    </div>
    <div v-else-if="truncated" class="ss-preview__tabs">
      <span class="flex-1"></span>
      <span class="ss-preview__truncated">
        Showing first {{ ROW_CAP }} rows
      </span>
    </div>

    <div class="ss-preview__body">
      <div v-if="loading" class="ss-preview__hint">Loading…</div>
      <div v-else-if="error" class="ss-preview__hint ss-preview__hint--err">
        Couldn't load file: {{ error }}
      </div>
      <div v-else-if="!activeSheet || !activeSheet.rows.length" class="ss-preview__hint">
        Empty sheet.
      </div>
      <table v-else class="ss-preview__table">
        <thead>
          <tr>
            <th class="ss-preview__corner"></th>
            <th
              v-for="(col, i) in colHeaders"
              :key="i"
              class="ss-preview__col-head"
            >{{ col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(row, ri) in activeSheet.rows"
            :key="ri"
          >
            <th class="ss-preview__row-head">{{ ri + 1 }}</th>
            <td
              v-for="(_, ci) in colHeaders"
              :key="ci"
              class="ss-preview__cell"
              :title="cellTitle(row[ci])"
            >{{ formatCell(row[ci]) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
/**
 * Spreadsheet preview for xlsx / xls / csv / tsv.
 *
 * Lazy-loads the ``xlsx`` (sheetjs) library when first used —
 * ~400KB minified, only fetched when the user opens a spreadsheet
 * file. The Library has its own ``SpreadsheetViewer`` that renders
 * pre-parsed markdown tables from indexed documents; this is the
 * Workbench peer that takes raw spreadsheet bytes and parses them
 * client-side.
 *
 * Multi-sheet xlsx workbooks render as tabs across the top; CSV /
 * TSV are single-sheet with a synthesised name. Rows are capped to
 * keep the table responsive — the modal toolbar's Download serves
 * the full file.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { extFor } from './fileType'

const props = defineProps({
  url: { type: String, required: true },
  filename: { type: String, default: '' },
})

const ROW_CAP = 1000

const sheets = ref([])         // [{ name, rows: 2D array }]
const activeIdx = ref(0)
const loading = ref(true)
const error = ref('')
const truncated = ref(false)

const activeSheet = computed(() => sheets.value[activeIdx.value] || null)
const colHeaders = computed(() => {
  const s = activeSheet.value
  if (!s || !s.rows.length) return []
  const maxCols = s.rows.reduce((m, r) => Math.max(m, r.length), 0)
  return Array.from({ length: maxCols }, (_, i) => columnLetter(i))
})

// Excel-style column letter: 0 → A, 25 → Z, 26 → AA, ...
function columnLetter(i) {
  let s = ''
  let n = i
  while (true) {
    s = String.fromCharCode(65 + (n % 26)) + s
    if (n < 26) break
    n = Math.floor(n / 26) - 1
  }
  return s
}

function formatCell(v) {
  if (v == null) return ''
  if (typeof v === 'number') {
    // Integer-looking numbers stay integer; floats get a sane default
    // precision rather than the full IEEE noise.
    if (Number.isInteger(v)) return String(v)
    return v.toFixed(6).replace(/\.?0+$/, '')
  }
  if (v instanceof Date) return v.toLocaleString()
  return String(v)
}

function cellTitle(v) {
  // Tooltip surfaces the un-truncated value when display ellipsises.
  if (v == null) return ''
  return formatCell(v)
}

async function load() {
  loading.value = true
  error.value = ''
  truncated.value = false
  sheets.value = []
  activeIdx.value = 0

  try {
    const ext = extFor(props.filename)
    const r = await fetch(props.url, { credentials: 'include' })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)

    if (ext === 'csv' || ext === 'tsv') {
      const text = await r.text()
      const sep = ext === 'tsv' ? '\t' : ','
      const rows = parseCsv(text, sep)
      sheets.value = [{
        name: ext === 'tsv' ? 'TSV' : 'CSV',
        rows: capRows(rows),
      }]
    } else {
      const buf = await r.arrayBuffer()
      const xlsx = await import('xlsx')
      const wb = xlsx.read(buf, { type: 'array', cellDates: true })
      const out = []
      for (const sn of wb.SheetNames) {
        const ws = wb.Sheets[sn]
        const rows = xlsx.utils.sheet_to_json(ws, {
          header: 1,
          defval: null,
          blankrows: false,
        })
        out.push({ name: sn, rows: capRows(rows) })
      }
      sheets.value = out
    }
  } catch (e) {
    error.value = e?.message || String(e)
    sheets.value = []
  } finally {
    loading.value = false
  }
}

function capRows(rows) {
  if (rows.length > ROW_CAP) {
    truncated.value = true
    return rows.slice(0, ROW_CAP)
  }
  return rows
}

// Minimal RFC-4180 CSV parser — handles quoted fields with embedded
// commas / newlines / escaped quotes. Avoids pulling in another lib
// for the simple case (sheetjs would also handle CSV, but loading
// xlsx for a CSV is overkill).
function parseCsv(text, sep) {
  const rows = []
  let row = []
  let field = ''
  let inQuotes = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++ }
        else { inQuotes = false }
      } else {
        field += c
      }
    } else {
      if (c === '"') {
        inQuotes = true
      } else if (c === sep) {
        row.push(field); field = ''
      } else if (c === '\n') {
        row.push(field); field = ''
        rows.push(row); row = []
      } else if (c === '\r') {
        // swallow — the \n handler will commit the row
      } else {
        field += c
      }
    }
  }
  // Trailing field / row.
  if (field.length || row.length) {
    row.push(field)
    rows.push(row)
  }
  return rows
}

onMounted(load)
watch(() => props.url, () => { load() })
</script>

<style scoped>
.ss-preview {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  background: var(--color-bg);
}
.ss-preview__tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
  flex-shrink: 0;
}
.ss-preview__tab {
  padding: 3px 10px;
  font-size: 0.6875rem;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.ss-preview__tab:hover { color: var(--color-t1); }
.ss-preview__tab.is-active {
  color: var(--color-t1);
  background: var(--color-bg3);
}
.ss-preview__truncated {
  font-size: 0.65625rem;
  color: var(--color-warn-fg, #b45309);
}

.ss-preview__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}
.ss-preview__hint {
  font-size: 0.75rem;
  color: var(--color-t3);
  text-align: center;
  padding: 32px 16px;
}
.ss-preview__hint--err { color: #ef4444; }

.ss-preview__table {
  border-collapse: collapse;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.71875rem;
  color: var(--color-t1);
  width: max-content;
  /* Minimum so an empty / single-column sheet doesn't collapse to
     icon-width. The body's overflow:auto handles wider workbooks. */
  min-width: 100%;
}

.ss-preview__corner,
.ss-preview__col-head {
  position: sticky;
  top: 0;
  background: var(--color-bg2);
  z-index: 2;
  font-weight: 500;
  font-size: 0.65625rem;
  color: var(--color-t3);
  text-align: center;
  border: 1px solid var(--color-line);
  padding: 4px 8px;
  user-select: none;
}
.ss-preview__row-head {
  position: sticky;
  left: 0;
  background: var(--color-bg2);
  z-index: 1;
  font-weight: 500;
  font-size: 0.65625rem;
  color: var(--color-t3);
  text-align: center;
  border: 1px solid var(--color-line);
  padding: 4px 8px;
  min-width: 40px;
  user-select: none;
}
.ss-preview__corner {
  position: sticky;
  top: 0;
  left: 0;
  z-index: 3;
  min-width: 40px;
}
.ss-preview__cell {
  border: 1px solid var(--color-line);
  padding: 4px 8px;
  white-space: nowrap;
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
