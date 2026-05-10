<script setup>
/**
 * /settings/sessions — every user's active web sessions.
 *
 * Split out from the old top-level /tokens page (which bundled
 * tokens + sessions). Tokens are now admin-only and live at
 * /settings/tokens; sessions stay user-scoped. Password change
 * lives on /settings/profile, so this page is purely the
 * "where am I logged in?" view.
 */
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { listSessions, revokeSession, signOutOtherSessions } from '@/api/auth'
import Skeleton from '@/components/Skeleton.vue'
import { useDialog } from '@/composables/useDialog'

const { t } = useI18n()
const { confirm, toast } = useDialog()

const sessions = ref([])
const loading = ref(false)

onMounted(refresh)

async function refresh() {
  loading.value = true
  try {
    sessions.value = (await listSessions()) || []
  } catch (e) {
    console.error('list sessions failed', e)
  } finally {
    loading.value = false
  }
}

async function onRevoke(s) {
  const ok = await confirm({
    title: t('settings.sessions.revoke_confirm_title'),
    description: t('settings.sessions.revoke_confirm_desc'),
    confirmText: t('settings.sessions.revoke_confirm_button'),
    variant: 'destructive',
  })
  if (!ok) return
  try {
    await revokeSession(s.session_id)
    await refresh()
    toast(t('settings.sessions.revoked_toast'), { variant: 'success' })
  } catch (e) {
    toast('Revoke failed: ' + e.message, { variant: 'error' })
  }
}

async function onSignOutOthers() {
  const ok = await confirm({
    title: t('settings.sessions.others_confirm_title'),
    description: t('settings.sessions.others_confirm_desc'),
    confirmText: t('settings.sessions.others_confirm_button'),
    variant: 'destructive',
  })
  if (!ok) return
  try {
    await signOutOtherSessions()
    await refresh()
    toast(t('settings.sessions.others_done_toast'), { variant: 'success' })
  } catch (e) {
    toast('Failed: ' + e.message, { variant: 'error' })
  }
}

function fmtDate(d) {
  if (!d) return ''
  try { return new Date(d).toLocaleString() } catch { return d }
}

function shortUA(ua) {
  if (!ua) return 'Unknown'
  const m = ua.match(/(Chrome|Safari|Firefox|Edge|curl|python-requests|httpx|Postman)\/[\d.]+/)
  return m ? m[0] : ua.slice(0, 40)
}
</script>

<template>
  <div class="sessions-page">
    <header class="page-header">
      <div>
        <h2 class="page-title">{{ t('settings.sessions.title') }}</h2>
        <p class="page-subtitle">{{ t('settings.sessions.subtitle') }}</p>
      </div>
      <button
        v-if="sessions.length > 1"
        @click="onSignOutOthers"
        class="btn-secondary"
      >{{ t('settings.sessions.sign_out_others') }}</button>
    </header>

    <section class="card">
      <table v-if="loading || sessions.length" class="t">
        <thead>
          <tr>
            <th>{{ t('settings.sessions.col_ua') }}</th>
            <th>{{ t('settings.sessions.col_ip') }}</th>
            <th>{{ t('settings.sessions.col_started') }}</th>
            <th>{{ t('settings.sessions.col_last_seen') }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody v-if="loading">
          <tr v-for="i in 2" :key="'sk' + i">
            <td><Skeleton :w="180" /></td>
            <td><Skeleton :w="80" /></td>
            <td><Skeleton :w="120" /></td>
            <td><Skeleton :w="120" /></td>
            <td><Skeleton :w="40" /></td>
          </tr>
        </tbody>
        <tbody v-else>
          <tr v-for="s in sessions" :key="s.session_id">
            <td>
              <span v-if="s.is_current" class="chip chip-ok mr-2">{{ t('settings.sessions.this_device') }}</span>
              <span class="text-t2">{{ shortUA(s.user_agent) }}</span>
            </td>
            <td class="text-t3">{{ s.ip || '—' }}</td>
            <td class="text-t3">{{ fmtDate(s.created_at) }}</td>
            <td class="text-t3">{{ fmtDate(s.last_seen_at) }}</td>
            <td>
              <button v-if="!s.is_current"
                @click="onRevoke(s)"
                class="row-action"
              >{{ t('settings.sessions.revoke') }}</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!loading && !sessions.length" class="empty-cell">
        {{ t('settings.sessions.empty') }}
      </div>
    </section>
  </div>
</template>

<style scoped>
/* Fills the Settings content area for visual parity with the
   sibling sub-pages (Tokens / Users) — all three data-table
   surfaces span the same width now. */
.sessions-page { width: 100%; }
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 18px;
}
.page-title {
  font-size: 1.125rem;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--color-t1);
  margin: 0 0 4px;
}
.page-subtitle {
  font-size: 0.75rem;
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
  font-size: 0.75rem;
}
.t thead th {
  text-align: left;
  font-weight: 500;
  font-size: 0.6875rem;
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

.chip {
  display: inline-block;
  font-size: 0.625rem;
  padding: 1px 6px;
  border-radius: 3px;
  letter-spacing: 0.02em;
}
.chip-ok {
  background: color-mix(in srgb, #10b981 14%, transparent);
  color: #047857;
}

.row-action {
  font-size: 0.6875rem;
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
  font-size: 0.75rem;
}

.btn-secondary {
  height: 28px;
  padding: 0 12px;
  font-size: 0.75rem;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  background: var(--color-bg);
  color: var(--color-t1);
  cursor: pointer;
  transition: background-color .12s;
}
.btn-secondary:hover { background: var(--color-bg3); }
</style>
