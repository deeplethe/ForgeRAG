/**
 * Generic row shape for the file-browser components.
 *
 * The Library and the Workbench both render directory-style trees of
 * folders + files, but the underlying data sources are completely
 * different (Library = indexed-corpus folders + documents with
 * folder_id / doc_id / ingest status; Workbench = filesystem entries
 * with just path + size + modified_at). Rather than couple the
 * renderers to one or the other, both views convert their domain
 * data into rows of this shape and pass them through the shared
 * components.
 *
 * Each adapter (see ``web/src/components/workspace/FileList.vue`` for
 * the Library adapter; ``web/src/views/Workspace.vue`` will grow a
 * Workbench adapter in Stage 2) is responsible for:
 *   * mapping its records onto FileRow,
 *   * declaring which row-actions / context-menu items / status
 *     chips are relevant via ``capabilities`` + slots,
 *   * translating the renderer's events back to domain operations.
 *
 *
 * Field reference
 * ===============
 *
 * key       — stable, unique identifier the renderer uses for
 *             selection / rename / drag tracking. Library uses
 *             ``"f:<folder_id>"`` / ``"d:<doc_id>"``; Workbench can
 *             use ``"fs:<path>"``. Treat as opaque downstream.
 *
 * kind      — ``"folder"`` or ``"file"``. Drives the icon (folder
 *             vs filename-extension) and which subset of actions
 *             apply (you can't open a "folder" as a document).
 *
 * name      — display label shown in the name cell.
 *
 * path      — absolute path the row represents. Used for breadcrumbs
 *             / drag payloads / the parent's open handler. Not
 *             user-visible directly; ``name`` is.
 *
 * size      — bytes for files; ``null`` for folders or unknown.
 *
 * createdAt / modifiedAt — ISO-8601 strings or null. Optional
 *             columns; the renderer only renders them when included
 *             in ``columns``.
 *
 * extras    — escape hatch the parent uses to thread domain-specific
 *             data through to its own slot renderers (e.g. Library
 *             passes the full Document so its status-chip slot can
 *             read ``status`` / ``embed_status`` / etc.).
 */

/**
 * @typedef {Object} FileRow
 * @property {string} key
 * @property {'folder' | 'file'} kind
 * @property {string} name
 * @property {string} path
 * @property {number | null} [size]
 * @property {string | null} [createdAt]
 * @property {string | null} [modifiedAt]
 * @property {Record<string, any>} [extras]
 */

/**
 * Capabilities the parent enables for a given mount of the renderer.
 * Defaults are conservative — Workbench (filesystem) wants almost
 * everything, Library (indexed corpus) has all of these plus
 * domain-specific ones it handles in slots.
 *
 * @typedef {Object} FileTableCapabilities
 * @property {boolean} [select]      // single click selects, default true
 * @property {boolean} [multiSelect] // Ctrl/Shift, default true
 * @property {boolean} [rename]      // inline rename via context menu, default false
 * @property {boolean} [dragMove]    // drag rows onto folder rows to move, default false
 * @property {boolean} [contextMenu] // right-click opens context menu, default true
 */

/**
 * Convenience: list of column ids the renderer knows. Parents pick
 * which ones to show via ``columns`` prop.
 */
export const FILE_TABLE_COLUMNS = Object.freeze([
  'name',       // always; left-most
  'type',       // file extension as label, or "Folder"
  'size',       // formatted bytes
  'created',
  'modified',
])

export const DEFAULT_FILE_TABLE_COLUMNS = Object.freeze([
  'name', 'type', 'size', 'modified',
])

export const DEFAULT_CAPABILITIES = Object.freeze({
  select: true,
  multiSelect: true,
  rename: false,
  dragMove: false,
  contextMenu: true,
})
