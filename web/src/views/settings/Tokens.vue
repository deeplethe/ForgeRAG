<script setup>
/**
 * /settings/tokens — admin-only API token management.
 *
 * Tokens are CLI / SDK / programmatic-access credentials.
 * Self-serve token creation is admin-only because each token
 * inherits its creator's role at issue time, so handing this
 * out to regular users would let them spawn bearer tokens that
 * bypass per-folder authz checks they don't have on web.
 *
 * (Sessions — the everyday "where am I logged in" view —
 * lives at /settings/sessions and is open to every user.)
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { listTokens, createToken, revokeToken } from '@/api/auth'
import Skeleton from '@/components/Skeleton.vue'
import DatePicker from '@/components/DatePicker.vue'
import { useDialog } from '@/composables/useDialog'

const { t } = useI18n()
const { confirm, toast } = useDialog()

const tokens = ref([])
const loading = ref(false)

const showNew = ref(false)
const newName = ref('')
const newExpiresAt = ref('')
const newSecret = ref('')

const tomorrow = computed(() => {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  return d
})

onMounted(refresh)

async function refresh() {
  loading.value = true
  try {
    tokens.value = (await listTokens()) || []
  } catch (e) {
    console.error('list tokens failed', e)
  } finally {
    loading.value = false
  }
}

async function onCreate() {
  let days = null
  if (newExpiresAt.value) {
    const picked = new Date(newExpiresAt.value)
    if (!isNaN(picked)) {
      const ms = picked.getTime() - Date.now()
      days = Math.max(1, Math.ceil(ms / 86400000))
    }
  }
  try {
    const r = await createToken(newName.value, days)
    newSecret.value = r.token
    newName.value = ''
    newExpiresAt.value = ''
    await refresh()
  } catch (e) {
    toast(t('settings.tokens.create_failed', { msg: e.message }), { variant: 'error' })
  }
}

async function onRevoke(token) {
  const ok = await confirm({
    title: t('settings.tokens.revoke_confirm_title', { name: token.name }),
    description: t('settings.tokens.revoke_confirm_desc'),
    confirmText: t('settings.tokens.revoke_confirm_button'),
    variant: 'destructive',
  })
  if (!ok) return
  try {
    await revokeToken(token.token_id)
    await refresh()
    toast(t('settings.tokens.revoked_toast'), { variant: 'success' })
  } catch (e) {
    toast('Revoke failed: ' + e.message, { variant: 'error' })
  }
}

function copySecret() {
  navigator.clipboard?.writeText(newSecret.value)
}

function fmtDate(d) {
  if (!d) return ''
  try { return new Date(d).toLocaleString() } catch { return d }
}

function isExpired(token) {
  return token.expires_at && new Date(token.expires_at) < new Date()
}
</script>

<template>
  <div class="tokens-page">
    <header class="page-header">
      <div>
        <h2 class="page-title">{{ t('settings.tokens.title') }}</h2>
        <p class="page-subtitle">{{ t('settings.tokens.subtitle') }}</p>
      </div>
      <button @click="showNew = true" class="btn-primary">+ {{ t('settings.tokens.new') }}</button>
    </header>

    <section class="card">
      <table v-if="loading || tokens.length" class="t">
        <thead>
          <tr>
            <th>{{ t('settings.tokens.col_name') }}</th>
            <th>{{ t('settings.tokens.col_prefix') }}</th>
            <th>{{ t('settings.tokens.col_created') }}</th>
            <th>{{ t('settings.tokens.col_last_used') }}</th>
            <th>{{ t('settings.tokens.col_expires') }}</th>
            <th>{{ t('settings.tokens.col_status') }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody v-if="loading">
          <tr v-for="i in 3" :key="'sk' + i">
            <td><Skeleton :w="120" /></td>
            <td><Skeleton :w="64" /></td>
            <td><Skeleton :w="100" /></td>
            <td><Skeleton :w="100" /></td>
            <td><Skeleton :w="60" /></td>
            <td><Skeleton :w="50" :h="16" :rounded="5" /></td>
            <td><Skeleton :w="40" /></td>
          </tr>
        </tbody>
        <tbody v-else>
          <tr v-for="token in tokens" :key="token.token_id">
            <td>{{ token.name }}</td>
            <td><code class="prefix">{{ token.hash_prefix }}</code></td>
            <td class="text-t3">{{ fmtDate(token.created_at) }}</td>
            <td class="text-t3">{{ fmtDate(token.last_used_at) || '—' }}</td>
            <td class="text-t3">{{ fmtDate(token.expires_at) || t('settings.tokens.never') }}</td>
            <td>
              <span v-if="token.revoked_at" class="chip chip-err">{{ t('settings.tokens.status_revoked') }}</span>
              <span v-else-if="isExpired(token)" class="chip chip-warn">{{ t('settings.tokens.status_expired') }}</span>
              <span v-else class="chip chip-ok">{{ t('settings.tokens.status_active') }}</span>
            </td>
            <td>
              <button v-if="!token.revoked_at"
                @click="onRevoke(token)"
                class="row-action"
              >{{ t('settings.tokens.revoke') }}</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!loading && !tokens.length" class="empty-cell">
        {{ t('settings.tokens.empty') }}
      </div>
    </section>

    <!-- New token modal -->
    <div v-if="showNew" class="modal-backdrop">
      <form @submit.prevent="onCreate" class="modal">
        <div class="modal-title">{{ t('settings.tokens.new_modal_title') }}</div>
        <div class="modal-hint">{{ t('settings.tokens.new_modal_hint') }}</div>

        <label class="field-label">{{ t('settings.tokens.field_name') }}</label>
        <input v-model="newName" :placeholder="t('settings.tokens.field_name_ph')"
          class="input mb-3" autofocus />

        <label class="field-label">
          {{ t('settings.tokens.field_expires') }}
          <span class="field-hint">{{ t('settings.tokens.field_expires_hint') }}</span>
        </label>
        <DatePicker
          v-model="newExpiresAt"
          :min-date="tomorrow"
          :placeholder="t('settings.tokens.field_expires_ph')"
          show-shortcuts
          class="mb-4"
        />

        <div class="modal-actions">
          <button type="button" @click="showNew = false" class="btn-secondary">{{ t('common.cancel') }}</button>
          <button type="submit" :disabled="!newName" class="btn-primary">{{ t('settings.tokens.create') }}</button>
        </div>

        <div v-if="newSecret" class="secret-box">
          <div class="secret-warn">⚠ {{ t('settings.tokens.save_warning') }}</div>
          <div class="secret-row">
            <code class="secret">{{ newSecret }}</code>
            <button type="button" @click="copySecret" class="copy-btn">📋</button>
          </div>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
/* Fills the Settings content area for visual parity with the
   sibling sub-pages (Sessions / Users) — all three data-table
   surfaces span the same width now. */
.tokens-page { width: 100%; }

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 18px;
}
.page-title {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--color-t1);
  margin: 0 0 4px;
}
.page-subtitle {
  font-size: 12px;
  color: var(--color-t3);
  margin: 0;
}

.card {
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  background: var(--color-bg);
  overflow: hidden;
}

.t {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.t thead th {
  text-align: left;
  font-weight: 500;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--color-t3);
  padding: 10px 16px;
  background: var(--color-bg2);
  border-bottom: 1px solid var(--color-line);
}
.t tbody td {
  padding: 10px 16px;
  border-bottom: 1px solid var(--color-line);
  color: var(--color-t1);
  vertical-align: middle;
}
.t tbody tr:last-child td { border-bottom: none; }

.prefix {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11px;
  color: var(--color-t3);
}

.chip {
  display: inline-block;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  letter-spacing: 0.02em;
}
.chip-ok {
  background: color-mix(in srgb, #10b981 14%, transparent);
  color: #047857;
}
.chip-warn {
  background: color-mix(in srgb, #f59e0b 14%, transparent);
  color: #b45309;
}
.chip-err {
  background: color-mix(in srgb, #ef4444 14%, transparent);
  color: #b91c1c;
}

.row-action {
  font-size: 11px;
  color: var(--color-err-fg, #b91c1c);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0;
}
.row-action:hover { text-decoration: underline; }

.empty-cell {
  padding: 24px;
  text-align: center;
  color: var(--color-t3);
  font-size: 12px;
}

/* Buttons */
.btn-primary {
  height: 28px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 500;
  border: none;
  border-radius: var(--r-sm);
  background: var(--color-t1);
  color: var(--color-bg);
  cursor: pointer;
  transition: opacity .15s;
}
.btn-primary:hover:not(:disabled) { background: var(--color-t1-hover); }
.btn-primary:disabled { opacity: .5; cursor: not-allowed; }

.btn-secondary {
  height: 28px;
  padding: 0 12px;
  font-size: 12px;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  background: var(--color-bg);
  color: var(--color-t1);
  cursor: pointer;
  transition: background-color .12s;
}
.btn-secondary:hover { background: var(--color-bg3); }

/* Modal */
.modal-backdrop {
  position: fixed; inset: 0;
  z-index: 50;
  display: flex; align-items: center; justify-content: center;
  background: rgba(0, 0, 0, 0.4);
}
.modal {
  width: 24rem;
  padding: 20px;
  border-radius: var(--r-md);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}
.modal-title {
  font-size: 13px;
  color: var(--color-t1);
  font-weight: 500;
  margin-bottom: 4px;
}
.modal-hint {
  font-size: 10px;
  color: var(--color-t3);
  margin-bottom: 16px;
}
.field-label {
  display: block;
  font-size: 10px;
  color: var(--color-t3);
  margin-bottom: 4px;
}
.field-hint { color: var(--color-t3); }
.input {
  width: 100%;
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
.mb-3 { margin-bottom: 12px; }
.mb-4 { margin-bottom: 16px; }

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.secret-box {
  margin-top: 16px;
  padding: 12px;
  border: 1px solid color-mix(in srgb, #f59e0b 30%, transparent);
  border-radius: var(--r-sm);
  background: color-mix(in srgb, #f59e0b 10%, transparent);
}
.secret-warn {
  font-size: 10px;
  font-weight: 500;
  color: #b45309;
  margin-bottom: 4px;
}
.secret-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.secret {
  flex: 1;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11px;
  word-break: break-all;
}
.copy-btn {
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 12px;
  color: var(--color-t2);
}
.copy-btn:hover { color: var(--color-t1); }
</style>
