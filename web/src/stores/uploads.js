/**
 * Global upload queue store.
 *
 * Survives navigation between pages (pinia singleton). History of completed
 * / failed items persists to localStorage; in-flight items (state=uploading
 * | ingesting | queued) do NOT persist — a File handle cannot be serialized.
 *
 * Lifecycle of one item:
 *
 *   queued → uploading → ingesting → ready
 *                      ↘         ↘
 *                       error     error   (retry returns to queued)
 *
 *   - `uploading`: POSTing /documents/upload-and-ingest
 *   - `ingesting`: upload done, backend pipeline running (we poll getDocument)
 *   - `ready`/`error`: terminal
 */

import { defineStore } from 'pinia'
import { uploadAndIngest, getDocument } from '@/api'

const LS_KEY = 'opencraig.uploads.history.v1'
const MAX_HISTORY = 200        // trim localStorage so it doesn't grow forever
const CONCURRENCY = 2          // simultaneous upload slots
const POLL_INTERVAL_MS = 1500  // status poll cadence
const POLL_MAX_MS = 30 * 60_000 // 30min ceiling — huge docs may legitimately take long

const TERMINAL_STATES = new Set(['ready', 'error', 'cancelled'])
const BACKEND_READY = new Set(['ready'])
const BACKEND_FAILED = new Set(['error'])
const BACKEND_ACTIVE = new Set(['pending', 'parsing', 'parsed', 'structuring', 'embedding'])

function newId() {
  return 'u_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4)
}

function loadHistory() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr : []
  } catch {
    return []
  }
}

function saveHistory(items) {
  try {
    const toSave = items
      .filter((it) => TERMINAL_STATES.has(it.state))
      .slice(-MAX_HISTORY)
      .map(({ file, ...rest }) => rest)  // strip File handle
    localStorage.setItem(LS_KEY, JSON.stringify(toSave))
  } catch {
    // quota exceeded or serialization failed — skip silently, history is best-effort
  }
}

export const useUploadsStore = defineStore('uploads', {
  state: () => ({
    items: loadHistory(),
    drawerOpen: false,
    _pumpRunning: false,
  }),

  getters: {
    active: (s) => s.items.filter((it) => !TERMINAL_STATES.has(it.state)),
    completed: (s) => s.items.filter((it) => it.state === 'ready'),
    failed: (s) => s.items.filter((it) => it.state === 'error'),
    queued: (s) => s.items.filter((it) => it.state === 'queued'),
    uploading: (s) => s.items.filter((it) => it.state === 'uploading'),
    ingesting: (s) => s.items.filter((it) => it.state === 'ingesting'),
    hasActivity: (s) => s.items.some((it) => !TERMINAL_STATES.has(it.state)),
    // summary for the collapsed floating button
    summary(s) {
      const act = this.active.length
      const ok = this.completed.length
      const bad = this.failed.length
      return { active: act, ok, bad, total: act + ok + bad }
    },
  },

  actions: {
    // ── queue entry ───────────────────────────────────────────────────
    enqueue(files, { folderPath = '/' } = {}) {
      const arr = Array.isArray(files) ? files : Array.from(files)
      for (const f of arr) {
        this.items.push({
          id: newId(),
          name: f.name,
          size: f.size,
          folderPath,
          state: 'queued',
          progress: 0,
          doc_id: null,
          file_id: null,
          backend_status: null,
          error: '',
          created_at: Date.now(),
          finished_at: null,
          file: f,  // NOT persisted
        })
      }
      this._pump()
    },

    retry(id) {
      const it = this.items.find((x) => x.id === id)
      if (!it || it.state !== 'error') return
      // Can only resume via upload if we still have the File handle in memory.
      // For localStorage-restored history items the file is gone — the UI
      // hides the retry button in that case.
      if (!it.file) return
      it.state = 'queued'
      it.error = ''
      it.progress = 0
      this._pump()
    },

    cancel(id) {
      const it = this.items.find((x) => x.id === id)
      if (!it) return
      if (TERMINAL_STATES.has(it.state)) return
      // Soft-cancel: flip state so the polling loop exits on next tick.
      // We don't abort in-flight fetch (not wired) — upload finishes server-side,
      // but the UI stops caring.
      it.state = 'cancelled'
      it.finished_at = Date.now()
      saveHistory(this.items)
    },

    remove(id) {
      const i = this.items.findIndex((x) => x.id === id)
      if (i >= 0) {
        this.items.splice(i, 1)
        saveHistory(this.items)
      }
    },

    clearCompleted() {
      this.items = this.items.filter((it) => it.state !== 'ready')
      saveHistory(this.items)
    },

    clearFailed() {
      this.items = this.items.filter((it) => it.state !== 'error')
      saveHistory(this.items)
    },

    toggleDrawer(v) {
      this.drawerOpen = v == null ? !this.drawerOpen : !!v
    },

    // ── internal: worker pump ─────────────────────────────────────────
    async _pump() {
      if (this._pumpRunning) return
      this._pumpRunning = true
      try {
        while (true) {
          const inFlight = this.items.filter(
            (it) => it.state === 'uploading' || it.state === 'ingesting',
          ).length
          if (inFlight >= CONCURRENCY) break
          const next = this.items.find((it) => it.state === 'queued')
          if (!next) break
          // Don't await — kick off work in parallel up to CONCURRENCY.
          // The loop re-checks inFlight and exits when saturated or no queue.
          this._processOne(next)
        }
      } finally {
        this._pumpRunning = false
      }
    },

    async _processOne(item) {
      if (!item.file) {
        item.state = 'error'
        item.error = 'File handle lost (reload?) — re-select the file'
        item.finished_at = Date.now()
        saveHistory(this.items)
        return
      }
      item.state = 'uploading'
      item.progress = 10
      try {
        const r = await uploadAndIngest(item.file, { folderPath: item.folderPath })
        if (item.state === 'cancelled') return
        item.doc_id = r.doc_id
        item.file_id = r.file_id
        item.backend_status = r.status || 'pending'
        item.state = 'ingesting'
        item.progress = 30
        await this._pollUntilTerminal(item)
      } catch (e) {
        if (item.state === 'cancelled') return
        item.state = 'error'
        item.error = e?.message || String(e)
        item.finished_at = Date.now()
      } finally {
        saveHistory(this.items)
        // Kick the pump in case a slot just opened up
        this._pump()
      }
    },

    async _pollUntilTerminal(item) {
      const started = Date.now()
      while (Date.now() - started < POLL_MAX_MS) {
        if (item.state === 'cancelled') return
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS))
        if (item.state === 'cancelled') return
        let doc
        try {
          doc = await getDocument(item.doc_id)
        } catch (e) {
          // Transient error during polling — don't fail the whole item on one
          // hiccup, loop continues.
          continue
        }
        const st = doc?.status
        item.backend_status = st
        item.progress = _progressFromStatus(st)
        if (BACKEND_READY.has(st)) {
          item.state = 'ready'
          item.progress = 100
          item.finished_at = Date.now()
          // Free the File handle now that we're done — no point keeping ~MBs
          // of binary in memory once ingestion succeeded.
          item.file = null
          return
        }
        if (BACKEND_FAILED.has(st)) {
          item.state = 'error'
          // `error_message` is the real field name in DocumentOut; fall back
          // to `error` (some older code paths) and finally a generic message.
          item.error = doc?.error_message || doc?.error || 'ingestion failed'
          item.finished_at = Date.now()
          return
        }
      }
      // timed out — mark as error, operator can retry
      item.state = 'error'
      item.error = 'ingestion timed out (>30min)'
      item.finished_at = Date.now()
    },
  },
})

function _progressFromStatus(st) {
  switch (st) {
    case 'pending':     return 35
    case 'parsing':     return 50
    case 'parsed':      return 65
    case 'structuring': return 75
    case 'embedding':   return 90
    case 'ready':       return 100
    default:            return 40
  }
}
