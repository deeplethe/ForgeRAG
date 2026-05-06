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
import { patchMe, getMyUsage } from '@/api/admin'
import UserAvatar from '@/components/UserAvatar.vue'

const { t } = useI18n()

const me = ref(null)
const loading = ref(true)
const usage = ref(null) // { input_tokens, output_tokens, total_tokens, message_count }

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

    <!-- ── Identity card: avatar + email + role ── -->
    <section class="card">
      <div class="identity-row">
        <UserAvatar :name="identityKey" :size="40" />
        <div class="identity-meta">
          <div class="email">{{ me.email || me.username || '—' }}</div>
          <div class="role">{{ me.role === 'admin' ? t('user_menu.role_admin') : t('user_menu.role_user') }}</div>
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

.identity-row { display: flex; align-items: center; gap: 12px; }
.identity-meta { min-width: 0; }
.email { font-size: 13px; color: var(--color-t1); font-weight: 500; }
.role { font-size: 11px; color: var(--color-t3); margin-top: 2px; text-transform: lowercase; }

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
