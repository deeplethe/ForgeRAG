<template>
  <div class="proj-detail">
    <header class="proj-detail__top">
      <button class="proj-detail__back" @click="back">
        <ArrowLeft :size="14" :stroke-width="1.75" />
        <span>{{ t('workspace.detail.back') }}</span>
      </button>
      <div v-if="project" class="proj-detail__heading">
        <h1>{{ project.name }}</h1>
        <p v-if="project.description" class="proj-detail__desc">
          {{ project.description }}
        </p>
        <p v-else class="proj-detail__desc proj-detail__desc--muted">
          {{ t('workspace.detail.no_description') }}
        </p>
      </div>
      <!-- Member-management button intentionally omitted: projects are
           single-writer with no UI-exposed sharing in Phase 0-5. The
           ProjectMembersDialog component file is kept on disk for the
           Phase 6+ read-only-share rollout. -->
      <div v-if="project" class="proj-detail__actions">
        <button
          class="btn btn--primary"
          @click="onOpenChat"
        >
          <MessageSquare :size="14" :stroke-width="1.75" />
          <span>{{ t('workspace.detail.open_chat') }}</span>
        </button>
      </div>
    </header>

    <main class="proj-detail__body">
      <div v-if="loading" class="proj-detail__placeholder">
        <Skeleton class="proj-detail__skeleton" />
      </div>

      <div v-else-if="error" class="proj-detail__placeholder proj-detail__placeholder--error">
        <AlertCircle :size="20" :stroke-width="1.75" />
        <p>{{ t('workspace.detail.load_error', { msg: error }) }}</p>
      </div>

      <ProjectFileBrowser
        v-else-if="project"
        ref="browser"
        :project-id="project.project_id"
        :read-only="!canWrite"
        @import-from-library="onImportFromLibrary"
      />
    </main>

    <LibraryDocPicker
      v-if="project && pickerOpen"
      :project-id="project.project_id"
      :project-name="project.name"
      @close="onPickerClose"
      @imported="onPickerImported"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { AlertCircle, ArrowLeft, MessageSquare } from 'lucide-vue-next'

import { getProject } from '@/api'
import Skeleton from '@/components/Skeleton.vue'
import ProjectFileBrowser from '@/components/workspace/ProjectFileBrowser.vue'
import LibraryDocPicker from '@/components/workspace/LibraryDocPicker.vue'
import { useDialog } from '@/composables/useDialog'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const dialog = useDialog()

const project = ref(null)
const loading = ref(true)
const error = ref('')
const browser = ref(null)
const pickerOpen = ref(false)

// Owner / admin can write; viewer (role='r') cannot. The route layer
// is the source of truth (404s viewer writes); this just dims the
// browser's mutating buttons so the UI matches.
const canWrite = computed(() => {
  const role = project.value?.role
  return role === 'owner' || role === 'admin'
})

async function load() {
  const id = route.params.projectId
  if (!id) return
  loading.value = true
  error.value = ''
  try {
    project.value = await getProject(id)
  } catch (e) {
    error.value = e?.message || String(e)
    project.value = null
  } finally {
    loading.value = false
  }
}

function back() {
  router.push('/workspace')
}

function onImportFromLibrary() {
  pickerOpen.value = true
}

// Opens chat with this project bound. Chat.vue reads ``?project=``
// on mount, fetches the project for the banner, and writes
// ``Conversation.project_id`` on the first send so subsequent
// agent runs route into this project's workdir.
function onOpenChat() {
  if (!project.value) return
  router.push({ path: '/chat', query: { project: project.value.project_id } })
}

function onPickerClose() {
  pickerOpen.value = false
}

function onPickerImported() {
  // Tell the file browser to re-list the workdir so the freshly
  // imported file shows up. ``defineExpose`` from
  // ProjectFileBrowser.vue exposes the reload method.
  const ref_ = browser.value
  if (ref_ && typeof ref_.reload === 'function') {
    ref_.reload()
  }
}

watch(() => route.params.projectId, load)
onMounted(load)
</script>

<style scoped>
.proj-detail {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 20px 32px;
  gap: 16px;
  overflow: hidden;
}

.proj-detail__top {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 16px;
}

.proj-detail__back {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted, #6b7280);
  font-size: 13px;
  cursor: pointer;
}

.proj-detail__back:hover {
  background: var(--surface-muted, #f3f4f6);
  color: var(--text, #111827);
}

.proj-detail__heading h1 {
  margin: 0 0 2px;
  font-size: 18px;
  font-weight: 600;
}

.proj-detail__desc {
  margin: 0;
  color: var(--text-muted, #6b7280);
  font-size: 13px;
  max-width: 720px;
}

.proj-detail__desc--muted {
  font-style: italic;
  opacity: 0.7;
}

.proj-detail__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.proj-detail__body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.proj-detail__placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 64px 16px;
  color: var(--text-muted, #6b7280);
  text-align: center;
}

.proj-detail__placeholder h2 {
  margin: 8px 0 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text, #111827);
}

.proj-detail__placeholder p {
  max-width: 480px;
  margin: 0;
  font-size: 13px;
}

.proj-detail__placeholder--error {
  color: var(--danger, #b91c1c);
}

.proj-detail__hint {
  font-size: 11.5px;
  opacity: 0.6;
}

.proj-detail__skeleton {
  width: 100%;
  height: 80px;
  border-radius: 8px;
}

.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 6px;
  background: transparent;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 120ms ease;
}

.btn--ghost:hover {
  background: var(--surface-muted, #f9fafb);
}

.btn--primary {
  background: var(--accent, #111827);
  border-color: var(--accent, #111827);
  color: white;
}

.btn--primary:hover {
  background: var(--accent-hover, #000);
}
</style>
