/**
 * Workspace — single source of truth for the file-manager UI.
 *
 * One instance per Workspace.vue page. Child components (FolderTree,
 * FileGrid/List, MillerColumn, ContextMenu, ...) receive this via
 * props / provide-inject so they can share selection + current path
 * state without each owning their own.
 */

import { computed, reactive, ref } from 'vue'
import {
  bulkMoveDocuments,
  createFolder,
  deleteFolder,
  getFolderTree,
  listDocuments,
  moveDocument,
  moveFolder,
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

  // ── View mode: 'grid' | 'list' | 'miller' ──────────────────────────
  const viewMode = ref(
    localStorage.getItem('workspace.viewMode') || 'grid',
  )
  function setViewMode(mode) {
    if (!['grid', 'list', 'miller'].includes(mode)) return
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
  const breadcrumbs = computed(() => {
    if (currentPath.value === ROOT_PATH) return [{ name: 'Home', path: '/' }]
    const parts = currentPath.value.split('/').filter(Boolean)
    const crumbs = [{ name: 'Home', path: '/' }]
    let acc = ''
    parts.forEach(p => {
      acc += '/' + p
      crumbs.push({ name: p, path: acc })
    })
    return crumbs
  })

  // ── Loaders ───────────────────────────────────────────────────────

  async function loadTree(depth = 4) {
    treeLoading.value = true
    try {
      tree.value = await getFolderTree('/', depth, false)
    } catch (e) {
      console.error('loadTree failed:', e)
      tree.value = null
    } finally {
      treeLoading.value = false
    }
  }

  async function loadContents(path = currentPath.value) {
    contentsLoading.value = true
    try {
      // Fetch folder tree at depth=1 to get direct children
      const node = await getFolderTree(path, 1, false)
      childFolders.value = (node?.children || []).filter(c => !c.is_system || c.path === path)
      // Fetch documents under this path (prefix search via list endpoint)
      // Listing with path_prefix isn't a current backend feature, so we
      // request a generous page and filter client-side. OK for < 1000 docs.
      const docs = await listDocuments({ limit: 500, offset: 0 })
      const items = docs?.items || []
      const norm = path === '/' ? '' : path
      childDocuments.value = items.filter(d => {
        if (!d.path) return false
        // Exact children of the current folder: path starts with "<norm>/"
        // AND no further "/" after that
        const head = norm ? norm + '/' : '/'
        if (!d.path.startsWith(head)) return false
        const tail = d.path.slice(head.length)
        return tail.length > 0 && !tail.includes('/')
      })
    } catch (e) {
      console.error('loadContents failed:', e)
      childFolders.value = []
      childDocuments.value = []
    } finally {
      contentsLoading.value = false
    }
  }

  async function navigate(path) {
    currentPath.value = path
    clearSelection()
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

  return {
    // state
    currentPath,
    tree, treeLoading,
    childFolders, childDocuments, contentsLoading,
    viewMode, setViewMode,
    selection, isSelected, toggleSelect, selectAll, clearSelection,
    clipboard, setClipboard, clearClipboard, hasClipboard,
    breadcrumbs,
    // actions
    loadTree, loadContents, navigate,
    opCreateFolder, opRenameFolder, opMoveFolder, opDeleteFolder,
    opMoveDocument, opBulkMoveDocuments,
  }
}
