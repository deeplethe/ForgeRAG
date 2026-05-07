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

  // ── Breadcrumbs ───────────────────────────────────────────────────
  // First segment is the literal "/" (the workspace root), not a "Home"
  // label — matches how paths are stored everywhere else.
  const breadcrumbs = computed(() => {
    if (currentPath.value === ROOT_PATH) return [{ name: '/', path: '/' }]
    const parts = currentPath.value.split('/').filter(Boolean)
    const crumbs = [{ name: '/', path: '/' }]
    let acc = ''
    parts.forEach(p => {
      acc += '/' + p
      crumbs.push({ name: p, path: acc })
    })
    return crumbs
  })

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
    await Promise.all([loadTree(), loadContents()])
  }

  async function opRenameFolder(path, newName) {
    await renameFolder(path, newName)
    // If we renamed the current folder, update path
    if (path === currentPath.value) {
      const parts = currentPath.value.split('/')
      parts[parts.length - 1] = newName
      currentPath.value = parts.join('/') || '/'
    }
    await Promise.all([loadTree(), loadContents()])
  }

  async function opMoveFolder(path, toParentPath) {
    await moveFolder(path, toParentPath)
    await Promise.all([loadTree(), loadContents()])
  }

  async function opDeleteFolder(path) {
    await deleteFolder(path)
    await Promise.all([loadTree(), loadContents()])
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
    breadcrumbs,
    // actions
    loadTree, loadContents, navigate,
    opCreateFolder, opRenameFolder, opMoveFolder, opDeleteFolder,
    opMoveDocument, opBulkMoveDocuments, opRenameDocument,
  }
}
