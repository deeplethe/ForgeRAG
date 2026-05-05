/**
 * useDocCache — singleton doc_id → document metadata cache.
 *
 * Citations carry only ``doc_id`` (the stable identity); display names
 * change via rename / move / re-ingest, so we resolve filenames at
 * render time. The cache persists for the SPA's lifetime — Vue
 * reactivity drives re-renders the moment a fetch lands.
 *
 * Batching: ``ensure(docId)`` collects requests in a microtask-scoped
 * queue and fires ONE ``POST /documents/lookup`` per render-tick. A
 * conversation with 5 citations across 3 unique docs costs one round
 * trip, not three.
 *
 * Usage in a template:
 *   const { ensure, getFilename } = useDocCache()
 *   ensure(c.doc_id)               // safe to call repeatedly
 *   <span>{{ getFilename(c.doc_id) || '(untitled)' }}</span>
 *
 * Cache invalidation: workspace rename / move flows can call
 * ``invalidate(docId)`` to drop a stale entry; the next ``ensure()``
 * re-fetches.
 *
 * Failure handling: 404 / network errors store an empty record so we
 * don't keep retrying. Call ``invalidate(docId)`` to retry.
 */

import { ref } from 'vue'
import { lookupDocuments } from '@/api'

// Reactive Map so component templates re-render when entries land.
const cache = ref(new Map())   // doc_id → DocumentOut | { filename: '' } on miss
const inFlight = new Set()     // doc_ids currently in a pending batch
const queue = new Set()        // doc_ids enqueued for the next batch
let scheduled = false

function flushBatch() {
  scheduled = false
  if (queue.size === 0) return
  const ids = [...queue]
  queue.clear()
  ids.forEach(id => inFlight.add(id))
  lookupDocuments(ids)
    .then((docs) => {
      const next = new Map(cache.value)
      const seen = new Set()
      // ``docs`` is a list (FastAPI ``List[DocumentOut]``); the route
      // silently drops missing IDs so we have to fill in blanks for any
      // we asked for but didn't get back.
      for (const d of docs || []) {
        if (d?.doc_id) {
          next.set(d.doc_id, d)
          seen.add(d.doc_id)
        }
      }
      for (const id of ids) {
        if (!seen.has(id)) next.set(id, { filename: '' })
      }
      cache.value = next
    })
    .catch((err) => {
      // Don't lock these IDs forever; mark as empty so renders proceed.
      const next = new Map(cache.value)
      for (const id of ids) next.set(id, { filename: '' })
      cache.value = next
      // eslint-disable-next-line no-console
      console.warn('lookupDocuments failed for', ids.length, 'ids:', err?.message || err)
    })
    .finally(() => {
      ids.forEach(id => inFlight.delete(id))
    })
}

function ensure(docId) {
  if (!docId || cache.value.has(docId) || inFlight.has(docId) || queue.has(docId)) return
  queue.add(docId)
  if (!scheduled) {
    scheduled = true
    // Microtask = "after this synchronous render pass". Vue typically
    // resolves all template expressions in one tick, so all the
    // ``ensure`` calls from a fresh conversation render coalesce into
    // a single batch.
    queueMicrotask(flushBatch)
  }
}

function getFilename(docId) {
  if (!docId) return ''
  return cache.value.get(docId)?.filename || ''
}

function getFileId(docId) {
  // Returns the file_id alongside doc_id from the cached
  // DocumentOut. Agent citations carry only ``chunk_id`` /
  // ``doc_id`` / ``page``, so the chat citation card resolves
  // ``file_id`` here to build PDF preview URLs.
  if (!docId) return ''
  return cache.value.get(docId)?.file_id || ''
}

function invalidate(docId) {
  if (!docId) return
  const next = new Map(cache.value)
  next.delete(docId)
  cache.value = next
}

export function useDocCache() {
  return { ensure, getFilename, getFileId, invalidate, cache }
}
