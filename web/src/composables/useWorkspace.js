/**
 * Workspace — single source of truth for the file-manager UI.
 *
 * One instance per Workspace.vue page. Child components (FolderTree,
 * FileGrid/List, MillerColumn, ContextMenu, ...) receive this via
 * props / provide-inject so they can share selection + current path
 * state without each owning their own.
 *
 * State is preserved across navigations because Workspace.vue is wrapped
 * in <KeepAlive> in App.vue — the component isn't unmounted when the user
 * navigates away.
 */

import { computed, reactive, ref } from 'vue'
import {
  bulkMoveDocuments,
  createFolder,
  deleteFolder,
  getFolderTree,
  getFolderSpaces,
  listDocuments,
  moveDocument,
  moveFolder,
  renameDocument,
  renameFolder,
} from '@/api'

const ROOT_PATH = '/'

export function useWorkspace() {
  // ── Current folder path we're browsing ────────────────────────────
  const currentPath = ref(ROOT_PATH)

  // ── The whole tree (lazy-loaded, root-level + children up to N deep) ──
  const tree = ref(null)     // FolderTreeNode | null
  const treeLoading = ref(false)

  // ── Contents of the current folder ─────────────────────────────────
  const childFolders = ref([])    // list of FolderOut
  const childDocuments = ref([])  // list of DocumentOut
  const contentsLoading = ref(false)

  // ── View mode: 'grid' | 'list' ─────────────────────────────────────
  // Default = grid (the 2D tile view). The legacy 'miller' mode was dropped.
  const _savedMode = localStorage.getItem('workspace.viewMode')
  const viewMode = ref(['grid', 'list'].includes(_savedMode) ? _savedMode : 'grid')
  function setViewMode(mode) {
    if (!['grid', 'list'].includes(mode)) return
    viewMode.value = mode
    localStorage.setItem('workspace.viewMode', mode)
  }

  // ── Selection (Set of item keys: "f:<folder_id>" | "d:<doc_id>") ──
  const selection = reactive(new Set())
  function isSelected(key) { return selection.has(key) }
  function toggleSelect(key, { additive = false } = {}) {
    if (!additive) {
      selection.clear()
      selection.add(key)
      return
    }
    selection.has(key) ? selection.delete(key) : selection.add(key)
  }
  function selectAll(keys) {
    selection.clear()
    keys.forEach(k => selection.add(k))
  }
  function clearSelection() { selection.clear() }

  // ── Clipboard (for cut/copy/paste via right-click menu) ────────────
  const clipboard = reactive({
    op: null,           // 'cut' | 'copy' | null
    items: [],          // list of { type: 'folder'|'doc', path, doc_id? }
    sourcePath: null,
  })

  function setClipboard(op, items, sourcePath) {
    clipboard.op = op
    clipboard.items = items
    clipboard.sourcePath = sourcePath
  }
  function clearClipboard() {
    clipboard.op = null
    clipboard.items = []
    clipboard.sourcePath = null
  }
  const hasClipboard = computed(() => clipboard.op && clipboard.items.length > 0)

  // ── Space resolution ──────────────────────────────────────────────
  // Locate which Space (if any) an absolute path belongs to. The
  // user only ever has visibility into their granted Spaces (per
  // PathRemap on the server), so this lookup also functions as a
  // visibility check. Used by ``breadcrumbs`` to translate paths
  // to a space-relative display form.
  function resolveSpace(absPath) {
    if (!absPath || absPath === ROOT_PATH) return null
    const spaces = tree.value?.children || []
    // Longest-prefix match: handles the (future) case of a Space
    // nested inside another granted root. Stable order otherwise.
    const sorted = [...spaces].sort(
      (a, b) => (b.path?.length || 0) - (a.path?.length || 0),
    )
    for (const space of sorted) {
      if (!space.path) continue
      if (absPath === space.path) return { space, relPath: '' }
      if (absPath.startsWith(space.path + '/')) {
        return { space, relPath: absPath.slice(space.path.length + 1) }
      }
    }
    return null
  }

  // ── Breadcrumbs ───────────────────────────────────────────────────
  // The user only ever sees Space-relative paths — the literal
  // ``/users/<username>/...`` prefix never reaches the UI. The
  // first crumb is always the synthetic "/" (the spaces list);
  // the second is the active Space's display label; remaining
  // crumbs are sub-segments inside that Space. Each crumb still
  // carries the ABSOLUTE path so click handlers (``navigate``)
  // keep working unchanged — only ``name`` is translated.
  const breadcrumbs = computed(() => {
    if (currentPath.value === ROOT_PATH) return [{ name: '/', path: '/' }]
    const resolved = resolveSpace(currentPath.value)
    if (!resolved) {
      // Outside every grant — happens during the brief window
      // between mount and ``loadTree`` resolving, or if the URL
      // has a stale ``?path=`` for a folder the user no longer
      // has access to. Fall back to the absolute split so the
      // user at least sees something sensible until the tree
      // arrives + the resolver re-runs.
      const parts = currentPath.value.split('/').filter(Boolean)
      const crumbs = [{ name: '/', path: '/' }]
      let acc = ''
      parts.forEach((p) => {
        acc += '/' + p
        crumbs.push({ name: p, path: acc })
      })
      return crumbs
    }
    const crumbs = [
      { name: '/', path: '/' },
      // Space root: clicking jumps to the Space's abs path; label
      // is the space's display name (already disambiguated server
      // side when basenames collide).
      { name: resolved.space.name, path: resolved.space.path },
    ]
    if (resolved.relPath) {
      const parts = resolved.relPath.split('/').filter(Boolean)
      let acc = resolved.space.path
      parts.forEach((p) => {
        acc += '/' + p
        crumbs.push({ name: p, path: acc })
      })
    }
    return crumbs
  })

  // ── User-facing path string ───────────────────────────────────────
  // What the address-bar / status-line component shows. Like
  // ``breadcrumbs`` but as a single slash-joined string. Synthetic
  // root → ``/``; Space root → ``/<space_name>``; sub-folder →
  // ``/<space_name>/sub/path``. Absolute ``/users/...`` never
  // surfaces here.
  const displayPath = computed(() => {
    if (currentPath.value === ROOT_PATH) return '/'
    const resolved = resolveSpace(currentPath.value)
    if (!resolved) return currentPath.value
    if (!resolved.relPath) return '/' + resolved.space.name
    return '/' + resolved.space.name + '/' + resolved.relPath
  })

  // Find the user's personal Space ("Home"). Returns the synthesised
  // tree child node (carries ``space_id`` / ``is_personal_space``
  // because ``loadTree`` mixed those in). Used by Workspace.vue's
  // mount to default-land users in their personal Space rather than
  // the multi-Space landing.
  function personalSpace() {
    const spaces = tree.value?.children || []
    return spaces.find((s) => s.is_personal_space) || null
  }

  // ── Loaders ───────────────────────────────────────────────────────

  const treeError = ref(null)
  // Phase 1 of the per-user Spaces model: the tree root is no
  // longer the global ``/``. Instead the backend returns the
  // user's grants as a flat list of "spaces", each with its
  // own subtree. We synthesise a virtual root whose children
  // are the space subtrees — this keeps every existing tree
  // consumer (FolderTree, FolderTreeNode, breadcrumbs)
  // working unchanged because they read ``root.children`` and
  // never the literal ``/``.
  // See docs/roadmaps/per-user-spaces.md.
  async function loadTree(depth = 4) {
    treeLoading.value = true
    treeError.value = null
    try {
      const res = await getFolderSpaces(depth)
      const spaces = res?.spaces || []
      tree.value = {
        // Synthetic root — never displayed itself; only its
        // children are rendered. ``path: '/'`` keeps anything
        // that compares against it for "are we at root" semantics
        // working unchanged.
        folder_id: '__virtual_root__',
        path: '/',
        parent_id: null,
        name: '/',
        is_system: true,
        trashed: false,
        child_folders: spaces.length,
        document_count: 0,
        children: spaces.map((s) => ({
          // Override the folder's natural ``name`` with the
          // space display name so collision-disambiguators
          // ("q4 (admin)") show up in the tree label.
          ...s.tree,
          name: s.space.name,
          // Carry the space metadata through so consumers that
          // need to know "this top-level node is a space root"
          // (Phase 2 / 3 — doc detail breadcrumb, scope picker)
          // can find it without a second fetch.
          space_id: s.space.space_id,
          is_personal_space: s.space.is_personal,
          space_role: s.space.role,
        })),
      }
    } catch (e) {
      console.error('loadTree (spaces) failed:', e)
      tree.value = null
      treeError.value = e?.message || String(e)
    } finally {
      treeLoading.value = false
    }
  }

  // Generation counter — every ``loadContents`` call captures the
  // current value at the start; on resolve it only commits its results
  // if the captured value is still ``_loadGen.value``. Any newer call
  // bumps ``_loadGen``, which silently invalidates the older one's
  // results so a stale response from a slower folder fetch can't
  // overwrite a faster click. No need to actually abort the in-flight
  // request — the response just gets dropped on arrival.
  let _loadGen = 0

  async function loadContents(path = currentPath.value) {
    const gen = ++_loadGen
    contentsLoading.value = true
    try {
      // Synthetic root: the user's "/" view shows their Spaces, not
      // the global tree. We deliberately do NOT call getFolderTree('/')
      // here — that endpoint isn't grant-filtered for non-admins, so
      // it would leak every top-level folder name regardless of access.
      // The Spaces list (loaded by ``loadTree``) is the ONLY source of
      // truth for what's visible at the root.
      if (path === ROOT_PATH) {
        const folders = (tree.value?.children || []).filter((c) => !c.is_system)
        if (gen !== _loadGen) return
        childFolders.value = folders
        childDocuments.value = []   // no documents live at the synthetic root
        return
      }
      // Child folders via the tree endpoint (depth=1 = direct children)
      const node = await getFolderTree(path, 1, false)
      if (gen !== _loadGen) return    // user navigated away mid-flight
      const folders = (node?.children || []).filter(c => !c.is_system || c.path === path)
      // Direct-child documents via server-side path_filter — paginated,
      // no more "pull 500 and filter client-side" hack.
      const docs = await listDocuments({
        path_filter: path,
        recursive: false,
        limit: 200,
        offset: 0,
      })
      if (gen !== _loadGen) return
      childFolders.value = folders
      childDocuments.value = docs?.items || []
    } catch (e) {
      if (gen !== _loadGen) return
      console.error('loadContents failed:', e)
      childFolders.value = []
      childDocuments.value = []
    } finally {
      // Only the latest generation gets to flip the loading flag off;
      // older generations finishing late shouldn't claim "done" while
      // the newer one is still fetching.
      if (gen === _loadGen) contentsLoading.value = false
    }
  }

  async function navigate(path, { force = false } = {}) {
    // Dedup: clicking the already-active folder shouldn't re-fetch unless
    // the caller asks for a refresh (e.g. post-upload).
    if (!force && path === currentPath.value && !contentsLoading.value) return
    currentPath.value = path
    clearSelection()
    // Wipe old contents BEFORE the await so the file grid / list don't
    // linger on the previous folder's data while the new fetch is in
    // flight.
    childFolders.value = []
    childDocuments.value = []
    contentsLoading.value = true
    await loadContents(path)
  }

  // ── Mutations ─────────────────────────────────────────────────────

  async function opCreateFolder(parentPath, name) {
    await createFolder(parentPath, name)
    // ``loadContents('/')`` now reads from ``tree.value.children``,
    // so the tree must be refreshed BEFORE the contents pass. (For
    // sub-paths the order doesn't matter.)
    await loadTree()
    await loadContents()
  }

  async function opRenameFolder(path, newName) {
    await renameFolder(path, newName)
    // If we renamed the current folder, update path
    if (path === currentPath.value) {
      const parts = currentPath.value.split('/')
      parts[parts.length - 1] = newName
      currentPath.value = parts.join('/') || '/'
    }
    // ``loadContents('/')`` now reads from ``tree.value.children``,
    // so the tree must be refreshed BEFORE the contents pass. (For
    // sub-paths the order doesn't matter.)
    await loadTree()
    await loadContents()
  }

  async function opMoveFolder(path, toParentPath) {
    await moveFolder(path, toParentPath)
    // ``loadContents('/')`` now reads from ``tree.value.children``,
    // so the tree must be refreshed BEFORE the contents pass. (For
    // sub-paths the order doesn't matter.)
    await loadTree()
    await loadContents()
  }

  async function opDeleteFolder(path) {
    await deleteFolder(path)
    // ``loadContents('/')`` now reads from ``tree.value.children``,
    // so the tree must be refreshed BEFORE the contents pass. (For
    // sub-paths the order doesn't matter.)
    await loadTree()
    await loadContents()
  }

  async function opMoveDocument(docId, toPath) {
    await moveDocument(docId, toPath)
    await loadContents()
  }

  async function opBulkMoveDocuments(docIds, toPath) {
    await bulkMoveDocuments(docIds, toPath)
    await loadContents()
  }

  async function opRenameDocument(docId, newFilename) {
    await renameDocument(docId, newFilename)
    // Path didn't change folder so the tree is fine; reload contents
    // to refresh the renamed row + any sort order shift.
    await loadContents()
  }

  return {
    // state
    currentPath,
    tree, treeLoading, treeError,
    childFolders, childDocuments, contentsLoading,
    viewMode, setViewMode,
    selection, isSelected, toggleSelect, selectAll, clearSelection,
    clipboard, setClipboard, clearClipboard, hasClipboard,
    breadcrumbs, displayPath,
    resolveSpace, personalSpace,
    // actions
    loadTree, loadContents, navigate,
    opCreateFolder, opRenameFolder, opMoveFolder, opDeleteFolder,
    opMoveDocument, opBulkMoveDocuments, opRenameDocument,
  }
}
