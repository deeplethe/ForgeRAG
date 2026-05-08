<template>
  <div class="workspace">
    <header class="workspace__top">
      <div class="workspace__heading">
        <h1>{{ t('workspace.title') }}</h1>
        <p class="workspace__subtitle">{{ t('workspace.subtitle') }}</p>
      </div>
      <button
        class="btn btn--primary"
        :disabled="creating"
        @click="onCreate"
      >
        <Plus :size="14" :stroke-width="1.75" />
        <span>{{ t('workspace.create_button') }}</span>
      </button>
    </header>

    <main class="workspace__body">
      <div v-if="loading" class="workspace__state">
        <Skeleton v-for="i in 3" :key="i" class="workspace__skeleton" />
      </div>

      <div v-else-if="error" class="workspace__state workspace__state--error">
        <AlertCircle :size="20" :stroke-width="1.75" />
        <p>{{ t('workspace.load_error', { msg: error }) }}</p>
        <button class="btn btn--ghost" @click="load">{{ t('common.retry') || 'Retry' }}</button>
      </div>

      <div v-else-if="!projects.length" class="workspace__state workspace__state--empty">
        <FolderKanban :size="36" :stroke-width="1.25" />
        <h2>{{ t('workspace.empty_title') }}</h2>
        <p>{{ t('workspace.empty_subtitle') }}</p>
        <button class="btn btn--primary" @click="onCreate">
          <Plus :size="14" :stroke-width="1.75" />
          <span>{{ t('workspace.create_button') }}</span>
        </button>
      </div>

      <ul v-else class="workspace__grid">
        <li
          v-for="p in projects"
          :key="p.project_id"
          class="project-card"
          @click="open(p)"
        >
          <div class="project-card__head">
            <h3>{{ p.name }}</h3>
            <span class="project-card__role" :data-role="p.role">{{ p.role }}</span>
          </div>
          <p v-if="p.description" class="project-card__desc">{{ p.description }}</p>
          <p v-else class="project-card__desc project-card__desc--muted">
            {{ t('workspace.project_card.no_description') }}
          </p>
          <footer class="project-card__foot">
            <span v-if="p.owner_username" class="project-card__owner">
              <User :size="12" :stroke-width="1.75" />
              {{ p.owner_username }}
            </span>
            <span v-if="p.member_count > 0" class="project-card__members">
              <Users :size="12" :stroke-width="1.75" />
              {{ p.member_count + 1 }}
            </span>
            <span class="project-card__active">
              {{ p.last_active_at
                  ? t('workspace.project_card.last_active', { rel: relTime(p.last_active_at) })
                  : t('workspace.project_card.never_active') }}
            </span>
          </footer>
        </li>
      </ul>
    </main>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { AlertCircle, FolderKanban, Plus, User, Users } from 'lucide-vue-next'

import { createProject, listProjects } from '@/api'
import Skeleton from '@/components/Skeleton.vue'
import { useDialog } from '@/composables/useDialog'

const { t } = useI18n()
const router = useRouter()
const dialog = useDialog()

const projects = ref([])
const loading = ref(true)
const error = ref('')
const creating = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    projects.value = await listProjects()
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function onCreate() {
  if (creating.value) return
  // Use the dialog composable's prompt for v1 — full dialog with
  // both name + description fields lands in Phase 1's polish pass.
  const name = await dialog.prompt({
    title: t('workspace.create_dialog.title'),
    description: t('workspace.create_dialog.description'),
    placeholder: t('workspace.create_dialog.placeholder'),
    confirmText: t('workspace.create_dialog.confirm'),
  })
  if (!name) return
  creating.value = true
  try {
    const created = await createProject(name.trim(), null)
    projects.value = [created, ...projects.value]
    open(created)
  } catch (e) {
    dialog.alert({
      title: t('workspace.create_error_title'),
      message: e?.message || String(e),
    })
  } finally {
    creating.value = false
  }
}

function open(p) {
  router.push({ path: `/workspace/${p.project_id}` })
}

// Lightweight relative-time formatter — enough for "5 minutes ago" /
// "2 days ago" without pulling in dayjs/luxon. Uses Intl.RelativeTimeFormat
// where available; falls back to ISO date for old browsers.
const _rtf = (() => {
  try {
    return new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  } catch {
    return null
  }
})()
const _UNITS = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['week', 7 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
  ['second', 1],
]
function relTime(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  const now = Date.now()
  const diffSec = Math.round((then - now) / 1000)
  const abs = Math.abs(diffSec)
  if (!_rtf) return new Date(iso).toLocaleDateString()
  for (const [unit, sec] of _UNITS) {
    if (abs >= sec || unit === 'second') {
      return _rtf.format(Math.round(diffSec / sec), unit)
    }
  }
  return _rtf.format(diffSec, 'second')
}

onMounted(load)
</script>

<style scoped>
.workspace {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 24px 32px;
  gap: 20px;
  overflow: hidden;
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
  color: var(--text-muted, #6b7280);
  font-size: 13px;
}

.workspace__body {
  flex: 1;
  overflow-y: auto;
}

.workspace__state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 64px 16px;
  color: var(--text-muted, #6b7280);
  text-align: center;
}

.workspace__state--empty h2 {
  margin: 8px 0 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text, #111827);
}

.workspace__state--empty p {
  max-width: 360px;
  margin: 0 0 8px;
  font-size: 13px;
}

.workspace__state--error {
  color: var(--danger, #b91c1c);
}

.workspace__skeleton {
  width: 100%;
  height: 96px;
  border-radius: 8px;
}

.workspace__grid {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}

.project-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px 16px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
  background: var(--surface, #fff);
  cursor: pointer;
  transition: border-color 120ms ease, box-shadow 120ms ease, transform 120ms ease;
}

.project-card:hover {
  border-color: var(--accent, #111827);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
  transform: translateY(-1px);
}

.project-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.project-card__head h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.project-card__role {
  flex-shrink: 0;
  padding: 2px 7px;
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-radius: 4px;
  background: var(--surface-muted, #f3f4f6);
  color: var(--text-muted, #6b7280);
}

.project-card__role[data-role='owner'] {
  background: rgba(217, 119, 6, 0.12);
  color: #92400e;
}

.project-card__role[data-role='admin'] {
  background: rgba(124, 58, 237, 0.12);
  color: #5b21b6;
}

.project-card__role[data-role='rw'] {
  background: rgba(15, 118, 110, 0.12);
  color: #0f766e;
}

.project-card__desc {
  margin: 0;
  color: var(--text-muted, #6b7280);
  font-size: 12.5px;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.project-card__desc--muted {
  font-style: italic;
  opacity: 0.7;
}

.project-card__foot {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: auto;
  padding-top: 8px;
  font-size: 11.5px;
  color: var(--text-muted, #6b7280);
}

.project-card__owner,
.project-card__members {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.project-card__active {
  margin-left: auto;
}

.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border: 1px solid transparent;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 120ms ease, border-color 120ms ease;
}

.btn--primary {
  background: var(--accent, #111827);
  color: white;
}

.btn--primary:hover {
  background: var(--accent-hover, #000);
}

.btn--primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn--ghost {
  background: transparent;
  border-color: var(--border, #e5e7eb);
  color: var(--text, #111827);
}

.btn--ghost:hover {
  background: var(--surface-muted, #f9fafb);
}
</style>
