/**
 * OpenCraig API — 统一导出
 *
 * 按模块分文件,这里汇总导出供 Vue 组件使用。
 * Config 走 yaml，不再有 settings 编辑 API。
 *
 * @example
 * import { askQueryStream, listDocuments } from '@/api'
 */

// HTTP client (底层,通常不直接使用)
export { request, get, post, put, patch, del } from './client'

// Health & System
export {
  getHealth,
  getStats,
  getRetrievalStatus,
  rebuildBM25,
  testConnection,
  getInfrastructure,
  getComponentHealth,
} from './health'

// Files (上传层)
export {
  uploadFile,
  uploadFromUrl,
  listFiles,
  getFile,
  fileDownloadUrl,
  filePreviewUrl,
  deleteFile,
} from './files'

// Documents (入库 + 管理 + 子资源)
export {
  ingestDocument,
  uploadAndIngest,
  listDocuments,
  getDocument,
  lookupDocuments,
  deleteDocument,
  stopDocument,
  reparseDocument,
  listBlocks,
  listChunks,
  getTree,
  getTreeNode,
} from './documents'

// Chunks & Blocks (独立访问)
export {
  getChunk,
  getChunkNeighbors,
  searchChunks,
  getChunksByNode,
  getChunkByBlock,
  getBlock,
  blockImageUrl,
  getBlocksByPage,
} from './chunks'

// Agent chat (post-cutover replacement for /query)
export { agentChatStream } from './agent'

// Search (检索本体, 无 LLM)
export {
  search,
} from './search'

// Conversations (多轮对话)
export {
  listConversations,
  createConversation,
  getConversation,
  updateConversation,
  deleteConversation,
  getMessages,
  addMessage,
} from './conversations'

// Traces (查询审计)
export {
  listTraces,
  getTrace,
  deleteTrace,
} from './traces'

// Knowledge Graph
export {
  getGraphStats,
  searchEntities,
  getEntityDetail,
  getSubgraph,
  getFullGraph,
  getGraphExplore,
  getGraphByDoc,
} from './graph'

// Benchmark
export {
  startBenchmark, cancelBenchmark, getBenchmarkStatus,
  listBenchmarkReports, downloadBenchmarkReport,
} from './benchmark'

// Folders + document path
export {
  listFolders, getFolderTree, getFolderInfo,
  createFolder, renameFolder, moveFolder, deleteFolder,
  moveDocument, bulkMoveDocuments, renameDocument,
} from './folders'

// Trash
export {
  listTrash, getTrashStats,
  restoreFromTrash, purgeTrashItems, emptyTrash,
} from './trash'

// Admin (user management)
export {
  listUsers, getUser, approveUser, suspendUser, reactivateUser,
  patchUser, deleteUser, patchMe,
} from './admin'
