/**
 * Folder-drop walking helper.
 *
 * Standard HTML5 ``DataTransfer.files`` is flat — when the user drops
 * a folder, that list is empty (or contains a placeholder entry the
 * browser refuses to read as a file). The actual folder contents
 * sit behind ``DataTransfer.items[*].webkitGetAsEntry()`` which
 * exposes the FileSystem API: ``FileSystemFileEntry`` (file leaf)
 * and ``FileSystemDirectoryEntry`` (descend via ``createReader``).
 *
 * This module wraps the callback-based FileSystem API as async
 * functions that walk the drop and return a flat list of
 * ``{file, relPath}`` records — ``relPath`` is the slash-joined
 * path inside the dropped folder, so the caller can reconstruct
 * the folder structure under the chat's current dir.
 *
 * Browser support: webkitGetAsEntry is the de-facto name everyone
 * implements (Chrome, Firefox, Safari, Edge). Standardised as
 * ``getAsEntry`` in the File System Access spec but the webkit
 * prefix is what actually works today.
 */

/**
 * Walk a single FileSystemEntry into flat file records.
 *
 * @param {FileSystemEntry} entry
 * @param {string} basePath  Slash-joined path of ancestors, no trailing
 *   slash. Empty string at the top-level entry the user dropped.
 * @returns {Promise<{file: File, relPath: string}[]>}
 */
async function _walkEntry(entry, basePath) {
  if (!entry) return []
  if (entry.isFile) {
    return new Promise((resolve, reject) => {
      entry.file(
        (file) => resolve([{ file, relPath: basePath }]),
        (err) => reject(err),
      )
    })
  }
  if (entry.isDirectory) {
    // readEntries returns at most ~100 entries per call — must loop
    // until it returns empty. Failing to loop is a common foot-gun
    // (skips entries silently in larger folders).
    const reader = entry.createReader()
    const childEntries = await new Promise((resolve, reject) => {
      const all = []
      const readBatch = () => reader.readEntries(
        (batch) => {
          if (batch.length === 0) resolve(all)
          else { all.push(...batch); readBatch() }
        },
        (err) => reject(err),
      )
      readBatch()
    })
    const nextBase = basePath ? `${basePath}/${entry.name}` : entry.name
    const results = []
    for (const child of childEntries) {
      const sub = await _walkEntry(child, nextBase)
      results.push(...sub)
    }
    return results
  }
  return []
}

/**
 * Walk a DataTransfer's items (drop event payload), returning flat
 * file records with their relative paths preserved.
 *
 * Mixed drops (some files + some folders) work — each top-level
 * entry is descended independently. Pure-file drops (the common
 * case) come back as ``[{file, relPath: ''}, ...]``.
 *
 * Browsers that don't support webkitGetAsEntry (very old) fall
 * through to ``dataTransfer.files`` flat-list — folders just don't
 * descend, which is the same behaviour the page had before.
 *
 * @param {DataTransferItemList} items  e.dataTransfer.items
 * @param {FileList} fallbackFiles      e.dataTransfer.files (used if
 *   items API unavailable or returns nothing)
 * @returns {Promise<{file: File, relPath: string}[]>}
 */
export async function walkDataTransfer(items, fallbackFiles) {
  if (!items || !items.length || typeof items[0].webkitGetAsEntry !== 'function') {
    return Array.from(fallbackFiles || []).map((file) => ({ file, relPath: '' }))
  }
  const topEntries = []
  for (let i = 0; i < items.length; i++) {
    const it = items[i]
    if (it.kind !== 'file') continue
    const entry = it.webkitGetAsEntry()
    if (entry) topEntries.push(entry)
  }
  const out = []
  for (const e of topEntries) {
    const records = await _walkEntry(e, '')
    out.push(...records)
  }
  return out
}

/**
 * Convert ``<input type="file" webkitdirectory>`` FileList into the
 * same shape ``walkDataTransfer`` returns. Each File has a
 * ``webkitRelativePath`` like ``"sales/2025/Q3-report.pdf"``;
 * we split off the leaf name to derive the relPath.
 *
 * @param {FileList} files
 * @returns {{file: File, relPath: string}[]}
 */
export function fileListWithRelativePaths(files) {
  const arr = Array.from(files || [])
  return arr.map((file) => {
    // webkitRelativePath includes the FILE name; the chat-level dir
    // is everything BEFORE the last "/" — we treat that as relPath.
    const wkpath = file.webkitRelativePath || ''
    const i = wkpath.lastIndexOf('/')
    const relPath = i >= 0 ? wkpath.slice(0, i) : ''
    return { file, relPath }
  })
}

/**
 * Group ``{file, relPath}`` records by relPath so callers can batch-
 * enqueue per target folder. The chat / library upload stores accept
 * one ``folderPath`` per enqueue call, so we group rather than
 * push one-at-a-time (cheaper, and the queue's drawer reads as
 * "uploaded a folder" rather than "uploaded N files separately").
 *
 * @param {{file: File, relPath: string}[]} records
 * @param {string} baseFolderPath  The chat / library dir the user
 *   dropped INTO. relPaths are joined under this.
 * @returns {Map<string, File[]>}  folderPath → files
 */
export function groupByFolder(records, baseFolderPath = '/') {
  const groups = new Map()
  const base = baseFolderPath.replace(/\/+$/, '') || ''
  for (const { file, relPath } of records) {
    const folderPath = relPath ? `${base}/${relPath}` : (base || '/')
    const norm = folderPath || '/'
    if (!groups.has(norm)) groups.set(norm, [])
    groups.get(norm).push(file)
  }
  return groups
}
