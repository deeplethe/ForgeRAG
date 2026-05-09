<template>
  <div class="workspace">
    <header class="workspace__top">
      <div class="workspace__heading">
        <h1>{{ t('workspace.title') }}</h1>
        <p class="workspace__subtitle">{{ t('workspace.subtitle') }}</p>
      </div>
      <div class="workspace__top-actions">
        <button
          class="btn btn--ghost"
          :disabled="busy"
          @click="onMakeFolder"
        >
          <FolderPlus :size="14" :stroke-width="1.75" />
          <span>{{ t('workspace.new_folder') }}</span>
        </button>
        <label class="btn btn--ghost" :class="{ 'is-disabled': busy }">
          <Upload :size="14" :stroke-width="1.75" />
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
    </header>

    <!-- Breadcrumb path navigator -->
    <nav class="workspace__crumbs" v-if="crumbs.length">
      <button
        v-for="(c, i) in crumbs"
        :key="c.path"
        class="crumb"
        :class="{ 'crumb--last': i === crumbs.length - 1 }"
        @click="open(c.path)"
      >
        {{ c.label }}
      </button>
    </nav>

    <main class="workspace__body">
      <div v-if="loading" class="workspace__state">
        <Skeleton v-for="i in 3" :key="i" class="workspace__skeleton" />
      </div>

      <div v-else-if="error" class="workspace__state workspace__state--error">
        <AlertCircle :size="20" :stroke-width="1.75" />
        <p>{{ t('workspace.load_error', { msg: error }) }}</p>
        <button class="btn btn--ghost" @click="load(currentPath)">
          {{ t('common.retry') || 'Retry' }}
        </button>
      </div>

      <div
        v-else-if="!entries.length"
        class="workspace__state workspace__state--empty"
      >
        <FolderKanban :size="36" :stroke-width="1.25" />
        <h2>{{ t('workspace.empty_title') }}</h2>
        <p>{{ t('workspace.empty_subtitle') }}</p>
        <div class="workspace__empty-actions">
          <button class="btn btn--primary" @click="onMakeFolder">
            <FolderPlus :size="14" :stroke-width="1.75" />
            <span>{{ t('workspace.new_folder') }}</span>
          </button>
          <button class="btn btn--ghost" @click="triggerUpload">
            <Upload :size="14" :stroke-width="1.75" />
            <span>{{ t('workspace.upload') }}</span>
          </button>
        </div>
      </div>

      <ul v-else class="workspace__entries">
        <li
          v-for="entry in entries"
          :key="entry.path"
          class="entry"
          :class="{ 'entry--dir': entry.is_dir }"
        >
          <button
            class="entry__main"
            @click="onEntryActivate(entry)"
            :title="entry.path"
          >
            <Folder
              v-if="entry.is_dir"
              :size="18"
              :stroke-width="1.5"
              class="entry__icon entry__icon--dir"
            />
            <FileText
              v-else
              :size="18"
              :stroke-width="1.5"
              class="entry__icon entry__icon--file"
            />
            <span class="entry__name">{{ entry.name }}</span>
            <span v-if="!entry.is_dir" class="entry__size">
              {{ fmtSize(entry.size_bytes) }}
            </span>
          </button>
          <div class="entry__actions">
            <button
              v-if="entry.is_dir"
              class="entry__action"
              :title="t('workspace.open_chat_here')"
              @click="onOpenChat(entry)"
            >
              <MessageSquare :size="14" :stroke-width="1.75" />
              <span>{{ t('workspace.open_chat_here') }}</span>
            </button>
            <a
              v-else
              class="entry__action"
              :href="downloadUrl(entry.path)"
              :title="t('workspace.download')"
            >
              <Download :size="14" :stroke-width="1.75" />
            </a>
          </div>
        </li>
      </ul>
    </main>

    <!-- Hint footer when in a sub-folder -->
    <footer v-if="currentPath && currentPath !== '/'" class="workspace__hint">
      <button class="btn btn--ghost btn--small" @click="onOpenChatCurrent">
        <MessageSquare :size="14" :stroke-width="1.75" />
        <span>{{ t('workspace.open_chat_in', { path: currentPath }) }}</span>
      </button>
    </footer>
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
  FolderKanban,
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
import Skeleton from '@/components/Skeleton.vue'
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
  // Always include root; then split currentPath into a chain of
  // clickable parents.
  const out = [{ path: '/', label: t('workspace.root') }]
  if (currentPath.value && currentPath.value !== '/') {
    const parts = currentPath.value
      .replace(/^\/+|\/+$/g, '')
      .split('/')
    let acc = ''
    for (const p of parts) {
      acc += '/' + p
      out.push({ path: acc, label: p })
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
  // Files: clicking the row body does nothing in v1.0; download
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
    const target =
      currentPath.value === '/' ? `/${name}` : `${currentPath.value}/${name}`
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

function triggerUpload() {
  uploadInput.value?.click()
}

async function onUpload(event) {
  const file = event.target.files?.[0]
  // Reset the input so the same file can be re-uploaded later.
  event.target.value = ''
  if (!file || busy.value) return
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
  }
}

function downloadUrl(path) {
  return workdirDownloadUrl(path)
}

function fmtSize(n) {
  if (!n || n < 1024) return `${n || 0} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

// Re-load when the URL's ?path= changes (e.g. user clicks a
// breadcrumb that calls router.replace, OR they navigate back).
watch(
  () => route.query.path,
  (newPath) => {
    const p = newPath || '/'
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
.workspace {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 24px;
  gap: 16px;
}

.workspace__top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.workspace__heading h1 {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.workspace__subtitle {
  margin: 0;
  color: var(--color-text-muted, #888);
  font-size: 13px;
}

.workspace__top-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.upload-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}

.workspace__crumbs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  padding: 8px 0;
  font-size: 13px;
}

.crumb {
  background: none;
  border: none;
  color: var(--color-text-muted, #888);
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: inherit;
}

.crumb:hover {
  background: var(--color-surface-2, #1f1f1f);
  color: var(--color-text, #fff);
}

.crumb--last {
  color: var(--color-text, #fff);
  font-weight: 500;
  cursor: default;
}

.crumb:not(:last-child)::after {
  content: '/';
  margin-left: 8px;
  color: var(--color-text-muted, #555);
}

.workspace__body {
  flex: 1;
  overflow: auto;
}

.workspace__state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 48px 16px;
  color: var(--color-text-muted, #888);
}

.workspace__state--error p {
  color: var(--color-error, #ef4444);
}

.workspace__state--empty h2 {
  margin: 8px 0 0;
  font-size: 16px;
  font-weight: 500;
}

.workspace__state--empty p {
  margin: 0;
  font-size: 13px;
}

.workspace__empty-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.workspace__skeleton {
  height: 48px;
  margin-bottom: 8px;
}

.workspace__entries {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.entry {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 6px;
  transition: background 80ms ease;
}

.entry:hover {
  background: var(--color-surface-2, #1f1f1f);
}

.entry__main {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 12px;
  background: none;
  border: none;
  color: inherit;
  text-align: left;
  cursor: pointer;
  padding: 0;
  font: inherit;
}

.entry--dir .entry__main {
  cursor: pointer;
}

.entry__icon--dir {
  color: var(--color-accent, #fbbf24);
}

.entry__icon--file {
  color: var(--color-text-muted, #888);
}

.entry__name {
  flex: 1;
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.entry__size {
  color: var(--color-text-muted, #888);
  font-size: 12px;
  margin-left: 8px;
}

.entry__actions {
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 80ms ease;
}

.entry:hover .entry__actions {
  opacity: 1;
}

.entry__action {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  background: none;
  border: 1px solid var(--color-border, #2a2a2a);
  border-radius: 4px;
  color: var(--color-text-muted, #888);
  cursor: pointer;
  font-size: 12px;
  text-decoration: none;
}

.entry__action:hover {
  border-color: var(--color-accent, #3291ff);
  color: var(--color-accent, #3291ff);
}

.workspace__hint {
  border-top: 1px solid var(--color-border, #2a2a2a);
  padding-top: 12px;
  display: flex;
  justify-content: flex-end;
}

.btn--small {
  font-size: 12px;
  padding: 4px 10px;
}

.is-disabled {
  opacity: 0.5;
  pointer-events: none;
}
</style>
