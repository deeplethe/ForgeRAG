<!--
  ProjectMembersDialog — read-only viewer manager for projects.

  ⚠️ NOT MOUNTED ANYWHERE in Phase 0–5 by design. The component
  file is kept on disk for the Phase 6+ read-only-share rollout —
  see "Project = owner-write + read-only share" in
  docs/roadmaps/agent-workspace.md. Until then, projects are
  single-user from the UI's perspective (owner only), even though
  the backend supports read-only invitations.

  When Phase 6+ wires this back in, only the email + role='r' add
  flow is supported; no role select (writers are never an option),
  no inheritance, no invitation tokens.

  Usage (when re-enabled):
    <ProjectMembersDialog
      :project="project"
      @close="..."
      @updated="onMembersUpdated"
    />
-->
<template>
  <Teleport to="body">
    <Transition name="dialog">
      <div
        class="dialog-backdrop"
        @click.self="onClose"
        @keydown.esc="onClose"
      >
        <div class="panel" role="dialog" aria-modal="true" tabindex="-1">
          <header class="header">
            <div>
              <div class="title">{{ t('workspace.members.title') }}</div>
              <div class="subtitle">
                {{ t('workspace.members.subtitle', { name: project.name }) }}
              </div>
            </div>
            <button class="close-btn" @click="onClose" aria-label="Close">
              <X :size="16" :stroke-width="1.75" />
            </button>
          </header>

          <div class="add-row">
            <input
              v-model="newEmail"
              type="email"
              class="email-input"
              :placeholder="t('workspace.members.email_placeholder')"
              @keydown.enter.prevent="onAdd"
            />
            <button
              class="btn btn--primary"
              :disabled="!newEmail || adding"
              @click="onAdd"
            >
              {{ adding ? t('workspace.members.adding') : t('workspace.members.add') }}
            </button>
          </div>
          <p class="add-row__hint">{{ t('workspace.members.add_hint') }}</p>

          <div v-if="error" class="error-row">
            <AlertCircle :size="14" :stroke-width="1.75" />
            <span>{{ error }}</span>
          </div>

          <div class="members">
            <div v-if="loading" class="empty">{{ t('common.loading') }}</div>
            <div v-else-if="!members.length" class="empty">
              {{ t('workspace.members.empty') }}
            </div>

            <div
              v-for="m in members"
              :key="m.user_id"
              class="member-row"
            >
              <UserAvatar
                :name="m.display_name || m.username"
                :size="24"
              />
              <div class="member-meta">
                <span class="member-name">
                  {{ m.display_name || m.username }}
                </span>
                <span class="member-email">
                  {{ m.email || m.username }}
                </span>
              </div>
              <span class="role-tag" :class="`role-tag--${m.role}`">
                {{ m.role === 'owner'
                    ? t('workspace.members.role_owner')
                    : t('workspace.members.role_r') }}
              </span>
              <button
                v-if="m.role !== 'owner'"
                class="remove-btn"
                :disabled="removing === m.user_id"
                @click="onRemove(m)"
                :aria-label="t('workspace.members.remove')"
              >
                <Trash2 :size="14" :stroke-width="1.75" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { AlertCircle, Trash2, X } from 'lucide-vue-next'
import {
  addProjectMember,
  listProjectMembers,
  removeProjectMember,
} from '@/api'
import UserAvatar from '@/components/UserAvatar.vue'

const { t } = useI18n()
const props = defineProps({
  project: { type: Object, required: true },
})
const emit = defineEmits(['close', 'updated'])

const members = ref([])
const loading = ref(false)
const error = ref('')
const newEmail = ref('')
const adding = ref(false)
const removing = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    members.value = await listProjectMembers(props.project.project_id)
    emit('updated', members.value)
  } catch (e) {
    error.value = _msg(e)
  } finally {
    loading.value = false
  }
}

async function onAdd() {
  if (!newEmail.value || adding.value) return
  adding.value = true
  error.value = ''
  try {
    members.value = await addProjectMember(
      props.project.project_id,
      newEmail.value.trim(),
    )
    newEmail.value = ''
    emit('updated', members.value)
  } catch (e) {
    error.value = _msg(e)
  } finally {
    adding.value = false
  }
}

async function onRemove(member) {
  removing.value = member.user_id
  error.value = ''
  try {
    members.value = await removeProjectMember(
      props.project.project_id,
      member.user_id,
    )
    emit('updated', members.value)
  } catch (e) {
    error.value = _msg(e)
  } finally {
    removing.value = ''
  }
}

function _msg(e) {
  return e?.message || String(e)
}

function onClose() {
  emit('close')
}

onMounted(load)
</script>

<style scoped>
.dialog-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.panel {
  width: min(540px, 90vw);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  background: var(--surface, #fff);
  border-radius: 12px;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.2);
  padding: 20px 24px;
  gap: 16px;
}

.header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.title {
  font-size: 16px;
  font-weight: 600;
  color: var(--text, #111827);
}

.subtitle {
  margin-top: 2px;
  font-size: 12.5px;
  color: var(--text-muted, #6b7280);
}

.close-btn {
  background: transparent;
  border: 0;
  padding: 4px;
  border-radius: 6px;
  cursor: pointer;
  color: var(--text-muted, #6b7280);
}

.close-btn:hover {
  background: var(--surface-muted, #f3f4f6);
  color: var(--text, #111827);
}

.add-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 8px;
  align-items: center;
}

.email-input {
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 6px;
  font-size: 13px;
}

.role-select {
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 6px;
  font-size: 13px;
  background: var(--surface, #fff);
}

.role-select--inline {
  height: 28px;
  font-size: 12px;
}

.btn {
  height: 32px;
  padding: 0 14px;
  border: 1px solid transparent;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
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

.error-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(220, 38, 38, 0.08);
  color: var(--danger, #b91c1c);
  font-size: 12.5px;
}

.members {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.empty {
  padding: 16px;
  text-align: center;
  color: var(--text-muted, #6b7280);
  font-size: 13px;
}

.member-row {
  display: grid;
  grid-template-columns: auto 1fr auto auto;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 6px;
}

.member-row:hover {
  background: var(--surface-muted, #f9fafb);
}

.member-meta {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.member-name {
  font-size: 13px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.member-email {
  font-size: 11.5px;
  color: var(--text-muted, #6b7280);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.role-tag {
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-radius: 4px;
  background: var(--surface-muted, #f3f4f6);
  color: var(--text-muted, #6b7280);
}

.role-tag--owner {
  background: rgba(217, 119, 6, 0.12);
  color: #92400e;
}

.remove-btn {
  background: transparent;
  border: 0;
  padding: 4px;
  border-radius: 4px;
  color: var(--text-muted, #6b7280);
  cursor: pointer;
}

.remove-btn:hover {
  background: rgba(220, 38, 38, 0.08);
  color: var(--danger, #b91c1c);
}

.remove-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dialog-enter-active, .dialog-leave-active {
  transition: opacity 150ms ease;
}
.dialog-enter-from, .dialog-leave-to {
  opacity: 0;
}
</style>
