<template>
  <div class="workbench">
    <!-- Toolbar — visually identical to Library's. Same height (min-h-[52px]),
         padding (px-5 py-3), border, surface — so navigating between Library
         and Workbench feels like a continuous page-header line. The buttons
         use the shared ``.toolbar-btn`` look (defined locally to avoid a
         cross-component scoped-style import). -->
    <div class="wb-toolbar">
      <Breadcrumb :crumbs="crumbs" @navigate="open" />

      <div class="flex-1"></div>

      <button
        v-if="currentPath && currentPath !== '/'"
        class="toolbar-btn"
        :title="t('workspace.open_chat_in', { path: currentPath })"
        @click="onOpenChatCurrent"
      >
        <MessageSquare :size="14" :stroke-width="1.5" />
        <span>{{ t('workspace.open_chat_here') }}</span>
      </button>
      <button
        class="toolbar-btn"
        :disabled="busy"
        @click="onMakeFolder"
      >
        <FolderPlus :size="14" :stroke-width="1.5" />
        <span>{{ t('workspace.new_folder') }}</span>
      </button>
      <label
        class="toolbar-btn"
        :class="{ 'is-disabled': busy }"
        :title="t('workspace.upload')"
      >
        <Upload :size="14" :stroke-width="1.5" />
        <span>{{ t('workspace.upload') }}</span>
        <input
          ref="uploadInput"
          type="file"
          class="upload-input"
          :disabled="busy"
          @change="onUpload"
        />
      </label>
    </div>

    <!-- Body — same vertical rhythm as Library's main pane. -->
    <div class="wb-body">
      <main class="wb-main">
        <div
          v-if="error"
          class="flex items-center gap-2 text-[11px] text-red-400 mx-4 my-3 px-3 py-2 border border-red-500/30 rounded bg-red-500/5"
        >
          <AlertCircle :size="14" :stroke-width="1.75" />
          <span>{{ t('workspace.load_error', { msg: error }) }}</span>
          <button
            class="ml-auto toolbar-btn"
            @click="load(currentPath)"
          >{{ t('common.retry') || 'Retry' }}</button>
        </div>

        <!-- File list — same column structure + row styling as Library's
             FileList. Workspace entries are filesystem-style
             ({path, name, is_dir, size_bytes, modified_at}); folders rank
             above files (mirrors FileList) and Type comes from the
             extension just like Library does. -->
        <div class="file-list" v-if="!error">
          <div
            v-if="loading && !entries.length"
            class="file-list__loading"
          >Loading…</div>
          <table class="w-full text-[11px]">
            <colgroup>
              <col class="col-name" />
              <col class="col-type" />
              <col class="col-size" />
              <col class="col-modified" />
              <col class="col-actions" />
            </colgroup>
            <thead>
              <tr class="text-t3">
                <th class="list-th">Name</th>
                <th class="list-th">Type</th>
                <th class="list-th">Size</th>
                <th class="list-th">Modified</th>
                <th class="list-th"></th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="entry in sortedEntries"
                :key="entry.path"
                class="list-row group"
                @dblclick="onEntryActivate(entry)"
              >
                <td>
                  <div class="name-cell">
                    <FileIcon
                      :kind="entry.is_dir ? 'folder' : 'file'"
                      :name="entry.name"
                      :size="16"
                      class="row-icon"
                    />
                    <button
                      class="name-text text-left truncate w-full"
                      :title="entry.path"
                      @click="onEntryActivate(entry)"
                    >{{ entry.name }}</button>
                  </div>
                </td>
                <td>{{ entry.is_dir ? 'Folder' : fmtType(entry.name) }}</td>
                <td>{{ entry.is_dir ? '—' : fmtSize(entry.size_bytes) }}</td>
                <td>{{ fmtDate(entry.modified_at) }}</td>
                <td class="row-actions">
                  <div class="row-actions-inner">
                    <button
                      v-if="entry.is_dir"
                      class="row-action-btn"
                      :title="t('workspace.open_chat_here')"
                      @click.stop="onOpenChat(entry)"
                    >
                      <MessageSquare :size="12" :stroke-width="1.5" />
                    </button>
                    <a
                      v-else
                      class="row-action-btn"
                      :href="downloadUrl(entry.path)"
                      :title="t('workspace.download')"
                      @click.stop
                    >
                      <Download :size="12" :stroke-width="1.5" />
                    </a>
                  </div>
                </td>
              </tr>
              <tr v-if="!loading && !entries.length">
                <td colspan="5" class="list-empty">
                  {{ t('workspace.empty_title') }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </main>
    </div>

    <FilePreview
      v-model:open="previewOpen"
      :path="previewPath"
      :filename="previewFilename"
      :preview-url="previewSrcUrl"
      :download-url="previewDownloadUrl"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  AlertCircle,
  Download,
  FolderPlus,
  MessageSquare,
  Upload,
} from 'lucide-vue-next'

import {
  getWorkdirInfo,
  listWorkdir,
  makeWorkdirFolder,
  uploadWorkdirFile,
  workdirDownloadUrl,
  workdirPreviewUrl,
} from '@/api'
import Breadcrumb from '@/components/workspace/Breadcrumb.vue'
import FileIcon from '@/components/workspace/FileIcon.vue'
import FilePreview from '@/components/preview/FilePreview.vue'
import { useDialog } from '@/composables/useDialog'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const dialog = useDialog()

// Current folder we're showing. Lives in the URL ``?path=`` so a
// shared link reproduces the same view + back/forward navigation
// works without extra state.
const currentPath = ref(route.query.path || '/')
const entries = ref([])
const loading = ref(true)
const error = ref('')
const busy = ref(false)
const uploadInput = ref(null)

const crumbs = computed(() => {
  // Shape matches the shared <Breadcrumb> component contract:
  // ``{ path, name }``. Always include the workspace root; then
  // split currentPath into a chain of clickable parents.
  const out = [{ path: '/', name: t('workspace.root') }]
  if (currentPath.value && currentPath.value !== '/') {
    const parts = currentPath.value
      .replace(/^\/+|\/+$/g, '')
      .split('/')
    let acc = ''
    for (const p of parts) {
      acc += '/' + p
      out.push({ path: acc, name: p })
    }
  }
  return out
})

// Folders first, then files; both alphabetical. Matches Library's
// default sort (name asc) so newcomers see the same ordering.
const sortedEntries = computed(() => {
  const dirs = []
  const files = []
  for (const e of entries.value) {
    (e.is_dir ? dirs : files).push(e)
  }
  const cmp = (a, b) => (a.name || '').localeCompare(b.name || '')
  dirs.sort(cmp)
  files.sort(cmp)
  return [...dirs, ...files]
})

async function load(path) {
  loading.value = true
  error.value = ''
  try {
    // Confirm the workdir is up first (auto-creates on first hit).
    // Cheap enough to do on every navigation; backend just stats
    // the dir and returns.
    await getWorkdirInfo()
    const list = await listWorkdir(path === '/' ? '' : path)
    entries.value = list
    currentPath.value = path
    // Reflect navigation in URL so reload + back/forward work.
    if (route.query.path !== path && !(path === '/' && !route.query.path)) {
      router.replace({ path: route.path, query: path === '/' ? {} : { path } })
    }
  } catch (e) {
    error.value = e?.message || String(e)
    entries.value = []
  } finally {
    loading.value = false
  }
}

function open(path) {
  load(path)
}

function onEntryActivate(entry) {
  if (entry.is_dir) {
    open(entry.path)
    return
  }
  // Open the preview modal for files. The modal's per-kind viewer
  // handles known types (image / video / audio for now; markdown,
  // pdf, code, spreadsheet, docx, html land in subsequent commits).
  // Unsupported extensions render a download fallback inside the
  // modal — the user always has a way out.
  previewEntry.value = entry
  previewOpen.value = true
}

const previewEntry = ref(null)
const previewOpen = ref(false)
const previewPath = computed(() => previewEntry.value?.path || '')
const previewFilename = computed(() => previewEntry.value?.name || '')
const previewSrcUrl = computed(() =>
  previewPath.value ? workdirPreviewUrl(previewPath.value) : '',
)
const previewDownloadUrl = computed(() =>
  previewPath.value ? workdirDownloadUrl(previewPath.value) : '',
)

function onOpenChat(folderEntry) {
  // Navigate to /chat with cwd_path query param. Chat.vue picks
  // it up, threads through to the agent runtime as the agent's
  // working directory.
  router.push({ path: '/chat', query: { cwd: folderEntry.path } })
}

function onOpenChatCurrent() {
  router.push({ path: '/chat', query: { cwd: currentPath.value } })
}

async function onMakeFolder() {
  if (busy.value) return
  const name = await dialog.prompt({
    title: t('workspace.new_folder_dialog.title'),
    description: t('workspace.new_folder_dialog.description'),
    placeholder: t('workspace.new_folder_dialog.placeholder'),
    confirmText: t('workspace.new_folder_dialog.confirm'),
  })
  if (!name) return
  // Reject path-separator-laden names client-side — backend enforces too,
  // but this gives a faster error.
  if (name.includes('/') || name.includes('\\')) {
    dialog.alert({
      title: t('workspace.new_folder_error_title'),
      message: t('workspace.new_folder_error_separator'),
    })
    return
  }
  busy.value = true
  try {
    const target = currentPath.value === '/'
      ? '/' + name
      : currentPath.value.replace(/\/+$/, '') + '/' + name
    await makeWorkdirFolder(target)
    await load(currentPath.value)
  } catch (e) {
    dialog.alert({
      title: t('workspace.new_folder_error_title'),
      message: e?.message || String(e),
    })
  } finally {
    busy.value = false
  }
}

async function onUpload(event) {
  const file = event?.target?.files?.[0]
  if (!file) return
  busy.value = true
  try {
    await uploadWorkdirFile(currentPath.value, file)
    await load(currentPath.value)
  } catch (e) {
    dialog.alert({
      title: t('workspace.upload_error_title'),
      message: e?.message || String(e),
    })
  } finally {
    busy.value = false
    if (uploadInput.value) uploadInput.value.value = ''
  }
}

function downloadUrl(path) {
  return workdirDownloadUrl(path)
}

function fmtSize(bytes) {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function fmtDate(d) {
  if (!d) return '—'
  try { return new Date(d).toLocaleString() } catch { return d }
}

function fmtType(name) {
  const m = (name || '').match(/\.([^.]+)$/)
  return m ? m[1].toUpperCase() : '—'
}

watch(
  () => route.query.path,
  (p) => {
    p = p || '/'
    if (p !== currentPath.value) {
      load(p)
    }
  },
)

onMounted(() => {
  load(currentPath.value)
})
</script>

<style scoped>
/* Container — full height, column layout matching Library.vue's
   ``.workspace`` outer shell so the page chrome (toolbar height, body
   scroll behaviour) is identical. */
.workbench {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

/* Toolbar — copies Library Toolbar.vue's surface exactly. The classes
   below are equivalent to ``flex items-center gap-1 px-5 py-3
   border-b border-line bg-bg2 min-h-[52px]``. Pin in CSS so HMR /
   theme tokens flow through cleanly without re-resolving Tailwind
   utilities at every component edit. */
.wb-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
  min-height: 52px;
  flex-shrink: 0;
}

.wb-body {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
}
.wb-main {
  flex: 1 1 auto;
  min-width: 0;
  overflow: auto;
}

/* Toolbar-btn — duplicate of the .toolbar-btn rules in
   components/workspace/Toolbar.vue. The Library toolbar's scoped
   styles can't leak here; copying the few rules keeps the visual
   match without introducing a shared global selector. */
.toolbar-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  font-size: 11px;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.toolbar-btn:hover:not(:disabled):not(.is-disabled) {
  background: var(--color-bg2);
  color: var(--color-t1);
}
.toolbar-btn:disabled,
.toolbar-btn.is-disabled {
  opacity: 0.4;
  cursor: not-allowed;
  pointer-events: none;
}

/* Hidden file input — visually invisible but keeps the label/button
   wrapper as the click target. Chrome won't open the picker on a
   ``display: none`` input, hence the 1×1 + opacity approach. */
.upload-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}

/* List — mirrors components/workspace/FileList.vue's table styles
   one-for-one so workbench rows, hover/selected states, name cells,
   and column widths visually match the Library list view. */
.file-list {
  position: relative;
  padding: 8px 16px;
  min-height: 160px;
  user-select: none;
}
.file-list table {
  border-collapse: collapse;
  table-layout: fixed;
  min-width: 686px;
}
.file-list__loading {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  font-size: 11px;
  color: var(--color-t3);
  letter-spacing: 0.02em;
  animation: fl-loading-pulse 1.4s ease-in-out infinite;
  pointer-events: none;
}
@keyframes fl-loading-pulse {
  0%, 100% { opacity: 0.45; }
  50%      { opacity: 0.9; }
}

.col-name      { width: auto; }
.col-type      { width: 90px; }
.col-size      { width: 96px; }
.col-modified  { width: 150px; }
.col-actions   { width: 56px; }

.list-th {
  text-align: left;
  padding: 6px 8px;
  font-weight: 400;
  font-size: 10px;
  color: var(--color-t3);
  white-space: nowrap;
}

.list-row { cursor: default; color: var(--color-t2); }
.list-row td {
  padding: 6px 8px;
  border-top: 1px solid var(--color-line);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.list-row:hover { background: var(--color-bg3); color: var(--color-t1); }

.list-empty {
  padding: 32px;
  text-align: center;
  color: var(--color-t3);
}

.name-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.name-cell .row-icon {
  flex-shrink: 0;
}
.name-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  background: transparent;
  border: none;
  color: inherit;
  font: inherit;
  cursor: pointer;
  padding: 0;
}

/* Hover-revealed action icons in the rightmost column — matches the
   Library list affordance pattern (icons appear when the row is
   pointed at). */
.row-actions { text-align: right; }
.row-actions-inline { display: inline-flex; gap: 2px; }
.row-actions-inner {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.12s;
}
.list-row:hover .row-actions-inner { opacity: 1; }
.row-action-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  text-decoration: none;
  transition: background 0.12s, color 0.12s;
}
.row-action-btn:hover {
  background: var(--color-bg2);
  color: var(--color-t1);
}
</style>
