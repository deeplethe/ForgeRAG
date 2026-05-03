/**
 * Capabilities store — server-side feature flags surfaced via /health.
 *
 * Fetched once on app mount and held for the session. Read by the
 * uploads store / Workspace UI to gate features the user shouldn't
 * see when the deployment isn't configured for them — currently:
 *
 *   imageUpload          (bool)  — image-as-document uploads work
 *   imageExtensions      (array) — accepted image extensions when
 *                                  the above is true; empty list
 *                                  otherwise
 *   spreadsheetUpload    (bool)  — spreadsheet-as-document uploads
 *                                  work (requires table_enrichment
 *                                  LLM configured)
 *   spreadsheetExtensions (array) — accepted spreadsheet extensions
 *                                   when spreadsheetUpload is true
 *   spreadsheetMaxCells  (number) — hard upload limit on cell count;
 *                                   used to short-circuit huge file
 *                                   uploads before bytes are sent
 *   legacyOfficeExtensions (array) — extensions ALWAYS rejected by
 *                                    the backend (.doc / .ppt /
 *                                    .xls); UI uses this to surface
 *                                    a "save as .docx" hint before
 *                                    sending the bytes
 *
 * The store is intentionally tiny — no caching, no background
 * refresh. /health is cheap and we only call it once. If a deploy
 * flips `image_enrichment.enabled` mid-session, the user has to
 * reload the page (acceptable: config changes already require a
 * backend restart).
 */

import { defineStore } from 'pinia'

import { getHealth } from '@/api'

export const useCapabilitiesStore = defineStore('capabilities', {
  state: () => ({
    loaded: false,
    imageUpload: false,
    imageExtensions: [],
    spreadsheetUpload: false,
    spreadsheetExtensions: [],
    spreadsheetMaxCells: 0,
    legacyOfficeExtensions: [],
  }),

  actions: {
    async refresh() {
      try {
        const h = await getHealth()
        const f = h?.features || {}
        // Defensive defaults — if the backend is older or returns a
        // partial payload, we fall back to "feature off" rather than
        // assume it's on. Better to under-permit than over-permit.
        this.imageUpload = !!f.image_upload
        this.imageExtensions = Array.isArray(f.image_upload_extensions)
          ? f.image_upload_extensions.map((e) => e.toLowerCase())
          : []
        this.spreadsheetUpload = !!f.spreadsheet_upload
        this.spreadsheetExtensions = Array.isArray(f.spreadsheet_upload_extensions)
          ? f.spreadsheet_upload_extensions.map((e) => e.toLowerCase())
          : []
        this.spreadsheetMaxCells = Number.isFinite(f.spreadsheet_max_cells)
          ? f.spreadsheet_max_cells
          : 0
        this.legacyOfficeExtensions = Array.isArray(f.legacy_office_extensions)
          ? f.legacy_office_extensions.map((e) => e.toLowerCase())
          : []
        this.loaded = true
      } catch {
        // Network fail / 5xx — leave defaults (everything off). The
        // upload code path then falls back on the backend's own
        // 415 rejection, which is fine; the toast just won't be as
        // pre-emptive.
        this.loaded = true
      }
    },

    /**
     * Classify a File against the current capabilities.
     * Returns one of:
     *   {ok: true}                                       — pass through
     *   {ok: false, reason: 'legacy_office', ...}        — old .doc/.ppt/.xls
     *   {ok: false, reason: 'image_disabled', ...}       — image but no VLM
     *   {ok: false, reason: 'spreadsheet_disabled', ...} — spreadsheet but no LLM
     */
    classify(file) {
      const name = (file && file.name) || ''
      const ext = '.' + (name.split('.').pop() || '').toLowerCase()
      if (this.legacyOfficeExtensions.includes(ext)) {
        return {
          ok: false,
          reason: 'legacy_office',
          ext,
          // Suggest the OOXML twin: .doc → .docx, .ppt → .pptx, etc.
          suggested: ext + 'x',
        }
      }
      // Standard image extensions — gated on capabilities.
      const ALL_IMAGE_EXTS = [
        '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff',
      ]
      if (ALL_IMAGE_EXTS.includes(ext) && !this.imageUpload) {
        return { ok: false, reason: 'image_disabled', ext }
      }
      // Spreadsheet extensions — gated on table_enrichment LLM. Mirror
      // of the image-no-VLM gate: without an LLM, the pipeline can't
      // generate a TABLE description and the doc would be unretrievable.
      const ALL_SPREADSHEET_EXTS = ['.xlsx', '.csv', '.tsv']
      if (ALL_SPREADSHEET_EXTS.includes(ext) && !this.spreadsheetUpload) {
        return { ok: false, reason: 'spreadsheet_disabled', ext }
      }
      return { ok: true }
    },
  },
})
