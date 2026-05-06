<script setup>
/**
 * /settings/profile — every user's account page.
 *
 * Editable here:
 *   * Display name (the friendly label that shows up in chat /
 *     audit / @mentions). Distinct from email which is the
 *     login identifier and only changeable via admin flow.
 *   * Password (current + new). Same /auth/change-password
 *     endpoint the existing ChangePasswordModal uses.
 *
 * Read-only fields (email, role, user_id) are shown for context
 * but not editable from here. Admin can change another user's
 * role from /settings/users; you cannot change your own role.
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { getMe, changePassword } from '@/api/auth'
import { patchMe, getMyUsage, uploadMyAvatar, deleteMyAvatar, avatarUrlFor } from '@/api/admin'
import UserAvatar from '@/components/UserAvatar.vue'
import { useDialog } from '@/composables/useDialog'
import { Camera, Trash2 } from 'lucide-vue-next'

const { t } = useI18n()
const { confirm, toast } = useDialog()

const me = ref(null)
const loading = ref(true)
const usage = ref(null) // { input_tokens, output_tokens, total_tokens, message_count }
const avatarBust = ref(0) // bumped after upload/delete to force img refresh
const avatarBusy = ref(false)
const fileInputEl = ref(null)

// Password form state — kept in a single reactive object so the
// "save" handler can clear it cleanly on success.
const pw = reactive({ current: '', next: '', confirm: '' })
const pwSaving = ref(false)
const pwError = ref('')
const pwSuccess = ref(false)

// Display name form state.
const displayName = ref('')
const dnSaving = ref(false)
const dnError = ref('')
const dnSuccess = ref(false)

// Source string for the avatar — same fallback chain the UI
// already shows next to the avatar so colour + initials match
// the visible label.
const identityKey = computed(() =>
  (me.value?.display_name || me.value?.email || me.value?.username || '').trim()
)

// Avatar image URL — only when /me said has_avatar=true. The
// ``avatarBust`` counter is appended on the URL so a fresh
// upload bypasses any browser cache; the GET handler also
// sends Cache-Control: no-cache as a defensive belt.
const avatarUrl = computed(() =>
  avatarUrlFor(me.value?.user_id, me.value?.has_avatar, avatarBust.value),
)

function pickAvatarFile() {
  fileInputEl.value?.click()
}

async function onAvatarFileChosen(e) {
  const file = e.target?.files?.[0]
  // Reset the input so picking the same file again still fires
  // ``change`` (the browser dedupes by default).
  if (fileInputEl.value) fileInputEl.value.value = ''
  if (!file) return
  if (!/^image\/(png|jpe?g|webp)$/i.test(file.type)) {
    toast(t('settings.profile.avatar_bad_type'), { variant: 'error' })
    return
  }
  if (file.size > 2 * 1024 * 1024) {
    toast(t('settings.profile.avatar_too_big'), { variant: 'error' })
    return
  }
  avatarBusy.value = true
  try {
    const updated = await uploadMyAvatar(file)
    me.value = updated
    avatarBust.value = Date.now()
    toast(t('settings.profile.avatar_saved'), { variant: 'success' })
  } catch (err) {
    toast(t('settings.profile.avatar_upload_failed', { msg: err.message || '' }), { variant: 'error' })
  } finally {
    avatarBusy.value = false
  }
}

async function onAvatarRemove() {
  const ok = await confirm({
    title: t('settings.profile.avatar_remove_confirm_title'),
    description: t('settings.profile.avatar_remove_confirm_desc'),
    confirmText: t('settings.profile.avatar_remove_confirm_button'),
    variant: 'destructive',
  })
  if (!ok) return
  avatarBusy.value = true
  try {
    const updated = await deleteMyAvatar()
    me.value = updated
    avatarBust.value = Date.now()
    toast(t('settings.profile.avatar_removed'), { variant: 'success' })
  } catch (err) {
    toast(t('settings.profile.avatar_upload_failed', { msg: err.message || '' }), { variant: 'error' })
  } finally {
    avatarBusy.value = false
  }
}

onMounted(async () => {
  // /me and /me/usage are independent — fire them in parallel so
  // the avatar/email card and the usage card paint together
  // instead of usage flickering in late.
  const [meRes, usageRes] = await Promise.allSettled([getMe(), getMyUsage()])
  if (meRes.status === 'fulfilled') {
    me.value = meRes.value
    displayName.value = me.value?.display_name || ''
  }
  if (usageRes.status === 'fulfilled') usage.value = usageRes.value
  loading.value = false
})

function fmtNum(n) {
  return (n || 0).toLocaleString()
}

async function onSaveDisplayName() {
  dnError.value = ''
  dnSuccess.value = false
  dnSaving.value = true
  try {
    const updated = await patchMe({ display_name: displayName.value })
    me.value = updated
    // Backend may have normalised whitespace / cleared the field —
    // re-sync the input so the disabled-when-unchanged button
    // reflects the saved state, not the typed-and-stripped state.
    displayName.value = updated.display_name || ''
    dnSuccess.value = true
  } catch (e) {
    dnError.value = e.message || 'Save failed'
  } finally {
    dnSaving.value = false
  }
}

async function onSavePassword() {
  pwError.value = ''
  pwSuccess.value = false
  if (pw.next !== pw.confirm) {
    pwError.value = t('settings.profile.password_mismatch')
    return
  }
  if (pw.next.length < 8) {
    pwError.value = t('settings.profile.password_too_short')
    return
  }
  pwSaving.value = true
  try {
    await changePassword(pw.current, pw.next)
    pw.current = ''; pw.next = ''; pw.confirm = ''
    pwSuccess.value = true
  } catch (e) {
    pwError.value = e.message || 'Failed to change password'
  } finally {
    pwSaving.value = false
  }
}
</script>

<template>
  <div v-if="!loading && me" class="profile-page">
    <h2 class="page-title">{{ t('settings.profile.title') }}</h2>

    <!-- ── Identity card: avatar editor + email + role ── -->
    <section class="card">
      <div class="identity-row">
        <!-- Click the disc → file picker. Hover shows the camera
             overlay; while busy the disc dims to signal in-flight. -->
        <button
          type="button"
          class="avatar-edit"
          :class="{ 'is-busy': avatarBusy }"
          :disabled="avatarBusy"
          :title="t('settings.profile.avatar_change')"
          @click="pickAvatarFile"
        >
          <UserAvatar
            :name="identityKey"
            :img-url="avatarUrl"
            :size="56"
          />
          <span class="avatar-overlay">
            <Camera :size="16" :stroke-width="2" />
          </span>
        </button>
        <input
          ref="fileInputEl"
          type="file"
          accept="image/png,image/jpeg,image/webp"
          class="hidden-input"
          @change="onAvatarFileChosen"
        />

        <div class="identity-meta">
          <div class="email">{{ me.email || me.username || '—' }}</div>
          <div class="role">{{ me.role === 'admin' ? t('user_menu.role_admin') : t('user_menu.role_user') }}</div>
          <div class="avatar-actions">
            <button type="button" class="link-btn" @click="pickAvatarFile" :disabled="avatarBusy">
              {{ me.has_avatar ? t('settings.profile.avatar_change') : t('settings.profile.avatar_upload') }}
            </button>
            <button
              v-if="me.has_avatar"
              type="button"
              class="link-btn link-btn-destructive"
              :disabled="avatarBusy"
              @click="onAvatarRemove"
            >
              <Trash2 :size="11" :stroke-width="1.75" />
              {{ t('settings.profile.avatar_remove') }}
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- ── Usage ── -->
    <section v-if="usage" class="card">
      <h3 class="card-title">{{ t('settings.usage.title') }}</h3>
      <p class="card-hint">{{ t('settings.usage.subtitle') }}</p>
      <div class="usage-grid">
        <div class="usage-stat">
          <div class="usage-num">{{ fmtNum(usage.input_tokens) }}</div>
          <div class="usage-label">{{ t('settings.usage.input_tokens') }}</div>
        </div>
        <div class="usage-stat">
          <div class="usage-num">{{ fmtNum(usage.output_tokens) }}</div>
          <div class="usage-label">{{ t('settings.usage.output_tokens') }}</div>
        </div>
        <div class="usage-stat usage-stat-emphasis">
          <div class="usage-num">{{ fmtNum(usage.total_tokens) }}</div>
          <div class="usage-label">{{ t('settings.usage.total_tokens') }}</div>
        </div>
        <div class="usage-stat">
          <div class="usage-num">{{ fmtNum(usage.message_count) }}</div>
          <div class="usage-label">{{ t('settings.usage.messages') }}</div>
        </div>
      </div>
    </section>

    <!-- ── Display name ── -->
    <section class="card">
      <h3 class="card-title">{{ t('settings.profile.display_name') }}</h3>
      <p class="card-hint">{{ t('settings.profile.display_name_hint') }}</p>
      <form class="form-row" @submit.prevent="onSaveDisplayName">
        <input
          v-model="displayName"
          class="input"
          :placeholder="t('settings.profile.display_name_placeholder')"
          maxlength="64"
        />
        <button type="submit" class="btn-primary" :disabled="dnSaving || displayName === (me.display_name || '')">
          {{ dnSaving ? t('settings.profile.saving') : t('settings.profile.save') }}
        </button>
      </form>
      <div v-if="dnError" class="form-error">{{ dnError }}</div>
      <div v-else-if="dnSuccess" class="form-success">{{ t('settings.profile.saved') }}</div>
    </section>

    <!-- ── Password ── -->
    <section class="card">
      <h3 class="card-title">{{ t('settings.profile.password') }}</h3>
      <p class="card-hint">{{ t('settings.profile.password_hint') }}</p>
      <form class="form-stack" @submit.prevent="onSavePassword">
        <input
          v-model="pw.current"
          type="password"
          class="input"
          :placeholder="t('settings.profile.password_current')"
          autocomplete="current-password"
          required
        />
        <input
          v-model="pw.next"
          type="password"
          class="input"
          :placeholder="t('settings.profile.password_new')"
          autocomplete="new-password"
          minlength="8"
          required
        />
        <input
          v-model="pw.confirm"
          type="password"
          class="input"
          :placeholder="t('settings.profile.password_confirm')"
          autocomplete="new-password"
          required
        />
        <button type="submit" class="btn-primary" :disabled="pwSaving">
          {{ pwSaving ? t('settings.profile.saving') : t('settings.profile.password_change') }}
        </button>
      </form>
      <div v-if="pwError" class="form-error">{{ pwError }}</div>
      <div v-else-if="pwSuccess" class="form-success">{{ t('settings.profile.password_changed') }}</div>
    </section>
  </div>
</template>

<style scoped>
.profile-page { max-width: 640px; }
.page-title {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--color-t1);
  margin: 0 0 20px;
}
.card {
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  background: var(--color-bg);
  padding: 18px 20px;
  margin-bottom: 16px;
}
.card-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-t1);
  margin: 0 0 4px;
}
.card-hint {
  font-size: 11px;
  color: var(--color-t3);
  margin: 0 0 12px;
  line-height: 1.5;
}

.identity-row { display: flex; align-items: center; gap: 14px; }
.identity-meta { min-width: 0; }
.email { font-size: 13px; color: var(--color-t1); font-weight: 500; }
.role { font-size: 11px; color: var(--color-t3); margin-top: 2px; text-transform: lowercase; }

/* Avatar editor — clickable disc with a hover camera overlay.
   Same approach Linear uses on its profile page: the avatar IS
   the picker, no separate "Upload" button cluttering the row. */
.avatar-edit {
  position: relative;
  padding: 0;
  border: none;
  background: transparent;
  cursor: pointer;
  border-radius: 50%;
  flex-shrink: 0;
  transition: opacity 0.15s;
}
.avatar-edit:disabled { cursor: not-allowed; }
.avatar-edit.is-busy { opacity: 0.6; pointer-events: none; }
.avatar-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.45);
  color: #fff;
  opacity: 0;
  transition: opacity 0.15s;
}
.avatar-edit:hover .avatar-overlay,
.avatar-edit:focus-visible .avatar-overlay {
  opacity: 1;
}
.hidden-input { display: none; }

.avatar-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 6px;
}
.link-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  background: transparent;
  border: none;
  font-size: 11px;
  color: var(--color-t2);
  cursor: pointer;
  transition: color 0.15s;
}
.link-btn:hover:not(:disabled) { color: var(--color-t1); }
.link-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.link-btn-destructive { color: var(--color-err-fg, #b91c1c); }
.link-btn-destructive:hover:not(:disabled) { color: #991b1b; }

.form-row { display: flex; gap: 8px; }
.form-row .input { flex: 1; }
.form-stack { display: flex; flex-direction: column; gap: 8px; }
.form-stack .btn-primary { align-self: flex-start; }

.input {
  height: 32px;
  padding: 0 10px;
  font-size: 13px;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  background: var(--color-bg);
  color: var(--color-t1);
  outline: none;
}
.input:focus { border-color: var(--color-line2); box-shadow: var(--ring-focus); }

.btn-primary {
  height: 32px;
  padding: 0 14px;
  font-size: 12px;
  font-weight: 500;
  border: none;
  border-radius: var(--r-sm);
  background: var(--color-t1);
  color: var(--color-bg);
  cursor: pointer;
  transition: opacity .15s, background-color .15s;
}
.btn-primary:hover:not(:disabled) { background: var(--color-t1-hover); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

.form-error { margin-top: 8px; font-size: 11px; color: var(--color-err-fg); }
.form-success { margin-top: 8px; font-size: 11px; color: var(--color-ok-fg); }

/* Usage stat grid — 4 cells, the third (Total) is faintly emphasised
   so the eye lands there. Numbers are tabular for clean alignment. */
.usage-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  margin-top: 4px;
}
.usage-stat {
  padding: 12px 14px;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  background: var(--color-bg);
}
.usage-stat-emphasis {
  background: var(--color-bg2);
}
.usage-num {
  font-size: 18px;
  font-weight: 600;
  color: var(--color-t1);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}
.usage-label {
  margin-top: 2px;
  font-size: 11px;
  color: var(--color-t3);
}
</style>
