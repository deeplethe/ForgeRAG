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

// Workdir — folder-as-cwd user-private file tree (the agent's workspace)
export {
  getWorkdirInfo,
  listWorkdir,
  makeWorkdirFolder,
  uploadWorkdirFile,
  workdirDownloadUrl,
  workdirPreviewUrl,
} from './workdir'

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

// Folders + document path
export {
  listFolders, getFolderTree, getFolderSpaces, getFolderInfo,
  createFolder, renameFolder, moveFolder, deleteFolder,
  moveDocument, bulkMoveDocuments, renameDocument,
  listFolderMembers, addFolderMember, updateFolderMemberRole, removeFolderMember,
} from './folders'

// Projects (agent-workspace surface)
export {
  listProjects, createProject, getProject, updateProject, deleteProject,
  listProjectMembers, addProjectMember, removeProjectMember,
  // workdir file ops
  listProjectFiles, uploadProjectFile, projectFileDownloadUrl,
  moveProjectFile, deleteProjectFile, mkdirProjectFile,
  // workdir trash
  listProjectTrash, restoreProjectTrash, purgeProjectTrash, emptyProjectTrash,
  // library import
  importDocFromLibrary,
} from './projects'

// Trash
export {
  listTrash, getTrashStats,
  restoreFromTrash, purgeTrashItems, emptyTrash,
} from './trash'

// First-boot setup wizard (unauthenticated; self-disables once configured)
export {
  getSetupStatus, listSetupPresets, testSetupLlm, commitSetup,
} from './setup'

// Admin (user management) + per-user usage + avatar + audit log
export {
  listUsers, getUser, approveUser, suspendUser, reactivateUser,
  patchUser, deleteUser, patchMe,
  listUserUsage, getUserUsage, getMyUsage,
  uploadMyAvatar, deleteMyAvatar, avatarUrlFor,
  listAuditLog,
} from './admin'
