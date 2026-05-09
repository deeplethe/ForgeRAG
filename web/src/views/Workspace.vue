<template>
  <div class="flex flex-col h-full">
    <!-- Toolbar: breadcrumb on the left, actions on the right.
         Same vertical metrics as Library's Toolbar so the page-header
         line is continuous when navigating between the two views. -->
    <div class="flex items-center gap-1 px-5 py-3 border-b border-line bg-bg2 min-h-[52px]">
      <Breadcrumb :crumbs="crumbs" @navigate="open" />

      <div class="flex-1"></div>

      <button
        v-if="currentPath && currentPath !== '/'"
        class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] text-t2 hover:bg-bg3 hover:text-t1 transition-colors cursor-pointer"
        :title="t('workspace.open_chat_in', { path: currentPath })"
        @click="onOpenChatCurrent"
      >
        <MessageSquare :size="14" :stroke-width="1.5" />
        <span>{{ t('workspace.open_chat_here') }}</span>
      </button>
      <button
        class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] text-t2 hover:bg-bg3 hover:text-t1 transition-colors cursor-pointer disabled:opacity-50 disabled:pointer-events-none"
        :disabled="busy"
        @click="onMakeFolder"
      >
        <FolderPlus :size="14" :stroke-width="1.5" />
        <span>{{ t('workspace.new_folder') }}</span>
      </button>
      <label
        class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] text-t2 hover:bg-bg3 hover:text-t1 transition-colors cursor-pointer"
        :class="{ 'opacity-50 pointer-events-none': busy }"
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

    <!-- Body — single full-width pane, padded to match Library. -->
    <main class="flex-1 overflow-auto px-5 py-4">
      <div v-if="loading" class="text-[11px] text-t3 px-3 py-6 text-center">
        Loading…
      </div>

      <div
        v-else-if="error"
        class="flex items-center gap-2 text-[11px] text-red-400 px-3 py-3 border border-red-500/30 rounded bg-red-500/5"
      >
        <AlertCircle :size="14" :stroke-width="1.75" />
        <span>{{ t('workspace.load_error', { msg: error }) }}</span>
        <button
          class="ml-auto toolbar-btn"
          @click="load(currentPath)"
        >{{ t('common.retry') || 'Retry' }}</button>
      </div>

      <div
        v-else-if="!entries.length"
        class="text-[11px] text-t3 px-3 py-12 text-center"
      >
        {{ t('workspace.empty_title') }}
      </div>

      <table v-else class="w-full text-[11px]">
        <colgroup>
          <col class="w-auto" />
          <col class="w-24" />
          <col class="w-40" />
        </colgroup>
        <thead>
          <tr class="text-t3 border-b border-line">
            <th class="text-left px-3 py-2 font-normal">Name</th>
            <th class="text-right px-3 py-2 font-normal">Size</th>
            <th class="text-right px-3 py-2 font-normal"></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="entry in entries"
            :key="entry.path"
            class="border-b border-line/50 hover:bg-bg2 group"
          >
            <td class="px-3 py-1.5">
              <button
                class="flex items-center gap-2 w-full text-left"
                @click="onEntryActivate(entry)"
                :title="entry.path"
              >
                <Folder
                  v-if="entry.is_dir"
                  :size="14"
                  :stroke-width="1.5"
                  class="text-amber-400 shrink-0"
                />
                <FileText
                  v-else
                  :size="14"
                  :stroke-width="1.5"
                  class="text-t3 shrink-0"
                />
                <span class="truncate">{{ entry.name }}</span>
              </button>
            </td>
            <td class="text-right px-3 py-1.5 text-t3 tabular-nums">
              <span v-if="!entry.is_dir">{{ fmtSize(entry.size_bytes) }}</span>
              <span v-else>—</span>
            </td>
            <td class="text-right px-3 py-1.5">
              <div class="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <button
                  v-if="entry.is_dir"
                  class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] text-t3 hover:text-t1 hover:bg-bg3 transition-colors cursor-pointer"
                  :title="t('workspace.open_chat_here')"
                  @click="onOpenChat(entry)"
                >
                  <MessageSquare :size="12" :stroke-width="1.5" />
                  <span>{{ t('workspace.open_chat_here') }}</span>
                </button>
                <a
                  v-else
                  class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] text-t3 hover:text-t1 hover:bg-bg3 transition-colors cursor-pointer no-underline"
                  :href="downloadUrl(entry.path)"
                  :title="t('workspace.download')"
                >
                  <Download :size="12" :stroke-width="1.5" />
                </a>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  AlertCircle,
  Download,
  FileText,
  Folder,
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
} from '@/api'
import Breadcrumb from '@/components/workspace/Breadcrumb.vue'
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
  }
  // Files: clicking the row body does nothing in v0.6; download
  // is via the explicit Download action on the right. Future:
  // inline preview for known types (txt / pdf / images / xlsx).
}

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
.upload-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}
</style>
