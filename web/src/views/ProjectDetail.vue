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
      <div v-if="project" class="proj-detail__actions"></div>
    </header>

    <main class="proj-detail__body">
      <div v-if="loading" class="proj-detail__placeholder">
        <Skeleton class="proj-detail__skeleton" />
      </div>

      <div v-else-if="error" class="proj-detail__placeholder proj-detail__placeholder--error">
        <AlertCircle :size="20" :stroke-width="1.75" />
        <p>{{ t('workspace.detail.load_error', { msg: error }) }}</p>
      </div>

      <div v-else-if="project" class="proj-detail__placeholder">
        <MessageSquare :size="36" :stroke-width="1.25" />
        <h2>{{ t('workspace.detail.no_chats_title') }}</h2>
        <p>{{ t('workspace.detail.no_chats_subtitle') }}</p>
        <p class="proj-detail__hint">
          {{ t('workspace.detail.phase0_note') }}
        </p>
      </div>
    </main>

  </div>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { AlertCircle, ArrowLeft, MessageSquare } from 'lucide-vue-next'

import { getProject } from '@/api'
import Skeleton from '@/components/Skeleton.vue'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()

const project = ref(null)
const loading = ref(true)
const error = ref('')

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
  overflow-y: auto;
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
</style>
