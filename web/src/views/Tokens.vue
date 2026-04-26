<template>
  <div class="tokens-page">
    <!-- ── Header ───────────────────────────────────────────────────── -->
    <div class="page-head">
      <div>
        <h1 class="text-[13px] text-t1 font-medium">Tokens &amp; Sessions</h1>
        <p class="text-[11px] text-t3 mt-0.5">Manage API tokens for CLI / SDK + active web sessions.</p>
      </div>
      <div class="flex gap-2">
        <button @click="showNewToken = true" class="btn-primary">+ New Token</button>
        <button @click="onChangePassword" class="btn-secondary">Change Password</button>
      </div>
    </div>

    <div class="page-body">
      <!-- ── API Tokens ─────────────────────────────────────────────── -->
      <section class="panel card">
        <header class="card-head">
          <div>
            <h2 class="card-title">API Tokens</h2>
            <p class="card-sub">Use as <code>Authorization: Bearer &lt;token&gt;</code> header. Forge_… format, 44 chars.</p>
          </div>
        </header>
        <table v-if="tokensLoading || tokens.length" class="t">
          <thead>
            <tr>
              <th>Name</th>
              <th>Prefix</th>
              <th>Created</th>
              <th>Last used</th>
              <th>Expires</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody v-if="tokensLoading">
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
            <tr v-for="t in tokens" :key="t.token_id">
              <td>{{ t.name }}</td>
              <td><code class="text-t3">{{ t.hash_prefix }}</code></td>
              <td class="text-t3">{{ fmtDate(t.created_at) }}</td>
              <td class="text-t3">{{ fmtDate(t.last_used_at) || '—' }}</td>
              <td class="text-t3">{{ fmtDate(t.expires_at) || 'never' }}</td>
              <td>
                <span v-if="t.revoked_at" class="chip chip-err">revoked</span>
                <span v-else-if="isExpired(t)" class="chip chip-warn">expired</span>
                <span v-else class="chip chip-ok">active</span>
              </td>
              <td>
                <button v-if="!t.revoked_at"
                  @click="onRevokeToken(t)"
                  class="row-action"
                >Revoke</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!tokensLoading && !tokens.length" class="empty-cell">No tokens yet.</div>
      </section>

      <!-- ── Active Sessions ─────────────────────────────────────────── -->
      <section class="panel card">
        <header class="card-head">
          <div>
            <h2 class="card-title">Active Sessions</h2>
            <p class="card-sub">Web logins only. "This device" is your current session.</p>
          </div>
          <button
            v-if="sessions.length > 1"
            @click="onSignOutOthers"
            class="btn-secondary"
          >Sign out other devices</button>
        </header>
        <table v-if="sessionsLoading || sessions.length" class="t">
          <thead>
            <tr>
              <th>User agent</th>
              <th>IP</th>
              <th>Started</th>
              <th>Last seen</th>
              <th></th>
            </tr>
          </thead>
          <tbody v-if="sessionsLoading">
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
                <span v-if="s.is_current" class="chip chip-ok mr-2">this device</span>
                <span class="text-t2">{{ shortUA(s.user_agent) }}</span>
              </td>
              <td class="text-t3">{{ s.ip || '—' }}</td>
              <td class="text-t3">{{ fmtDate(s.created_at) }}</td>
              <td class="text-t3">{{ fmtDate(s.last_seen_at) }}</td>
              <td>
                <button v-if="!s.is_current"
                  @click="onRevokeSession(s)"
                  class="row-action"
                >Revoke</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!sessionsLoading && !sessions.length" class="empty-cell">No active sessions.</div>
      </section>
    </div>

    <!-- ── New Token Modal ─────────────────────────────────────────── -->
    <div v-if="showNewToken" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <form @submit.prevent="onCreateToken" class="w-96 p-5 rounded-lg bg-bg border border-line shadow-lg">
        <div class="text-[13px] text-t1 font-medium mb-1">New API Token</div>
        <div class="text-[10px] text-t3 mb-4">Give this token a label so you can identify it later.</div>

        <label class="block text-[10px] text-t3 mb-1">Name</label>
        <input v-model="newTokenName" placeholder="e.g. laptop-cli"
          class="input mb-3" autofocus />

        <label class="block text-[10px] text-t3 mb-1">Expires on <span class="text-t3">(leave blank = never)</span></label>
        <DatePicker
          v-model="newTokenExpiresAt"
          :min-date="tomorrow"
          placeholder="Pick an expiration date"
          show-shortcuts
          class="mb-4"
        />

        <div class="flex justify-end gap-2">
          <button type="button" @click="showNewToken = false" class="btn-secondary">Cancel</button>
          <button type="submit" :disabled="!newTokenName" class="btn-primary">Create</button>
        </div>

        <div v-if="newTokenSecret" class="mt-4 p-3 rounded border"
             style="background: var(--color-warn-bg); border-color: var(--color-warn-fg); border-opacity: 0.3;">
          <div class="text-[10px] font-medium mb-1" style="color: var(--color-warn-fg);">⚠ Save this token — it won't appear again</div>
          <div class="flex items-center gap-2">
            <code class="font-mono text-[11px] break-all flex-1">{{ newTokenSecret }}</code>
            <button type="button" @click="copySecret"
              class="text-[10px] text-t2 hover:text-t1">📋</button>
          </div>
        </div>
      </form>
    </div>

    <ChangePasswordModal
      :open="showChangePwd"
      :forced="false"
      @close="showChangePwd = false"
      @changed="onPasswordChanged"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  listTokens, createToken, revokeToken,
  listSessions, revokeSession, signOutOtherSessions,
} from '@/api/auth'
import ChangePasswordModal from '@/components/ChangePasswordModal.vue'
import Skeleton from '@/components/Skeleton.vue'
import DatePicker from '@/components/DatePicker.vue'
import { useDialog } from '@/composables/useDialog'

const { confirm, toast } = useDialog()

const tokens = ref([])
const sessions = ref([])
const tokensLoading = ref(false)
const sessionsLoading = ref(false)

const showNewToken = ref(false)
const newTokenName = ref('')
// DatePicker stores ISO yyyy-mm-dd; '' = no expiration. Backend takes a
// "days from now" integer, which we derive at submit time.
const newTokenExpiresAt = ref('')
const newTokenSecret = ref('')

// Tomorrow is the earliest pickable date (a 0-day token would be useless).
const tomorrow = computed(() => {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  return d
})

const showChangePwd = ref(false)

onMounted(() => { refresh() })

async function refresh() {
  tokensLoading.value = true
  sessionsLoading.value = true
  try {
    const [t, s] = await Promise.all([listTokens(), listSessions()])
    tokens.value = t || []
    sessions.value = s || []
  } catch (e) {
    console.error('auth list failed', e)
  } finally {
    tokensLoading.value = false
    sessionsLoading.value = false
  }
}

async function onCreateToken() {
  // Convert picked date → days_from_now (rounded up — partial days favor the
  // user). Empty value = never-expiring token.
  let days = null
  if (newTokenExpiresAt.value) {
    const picked = new Date(newTokenExpiresAt.value)
    if (!isNaN(picked)) {
      const ms = picked.getTime() - Date.now()
      days = Math.max(1, Math.ceil(ms / 86400000))
    }
  }
  try {
    const r = await createToken(newTokenName.value, days)
    newTokenSecret.value = r.token
    newTokenName.value = ''
    newTokenExpiresAt.value = ''
    await refresh()
  } catch (e) { toast('Create failed: ' + e.message, { variant: 'error' }) }
}

async function onRevokeToken(t) {
  const ok = await confirm({
    title: `Revoke token "${t.name}"?`,
    description: 'Clients using it will start getting 401 immediately.',
    confirmText: 'Revoke',
    variant: 'destructive',
  })
  if (!ok) return
  try { await revokeToken(t.token_id); await refresh(); toast('Token revoked', { variant: 'success' }) }
  catch (e) { toast('Revoke failed: ' + e.message, { variant: 'error' }) }
}

async function onRevokeSession(s) {
  const ok = await confirm({
    title: 'Sign out this session?',
    description: 'The device using this session will be logged out next request.',
    confirmText: 'Sign out',
    variant: 'destructive',
  })
  if (!ok) return
  try { await revokeSession(s.session_id); await refresh(); toast('Session signed out', { variant: 'success' }) }
  catch (e) { toast('Revoke failed: ' + e.message, { variant: 'error' }) }
}

async function onSignOutOthers() {
  const ok = await confirm({
    title: 'Sign out all other devices?',
    description: 'You will remain signed in on this device. Others will be logged out next request.',
    confirmText: 'Sign out others',
    variant: 'destructive',
  })
  if (!ok) return
  try { await signOutOtherSessions(); await refresh(); toast('Other sessions signed out', { variant: 'success' }) }
  catch (e) { toast('Failed: ' + e.message, { variant: 'error' }) }
}

function onChangePassword() { showChangePwd.value = true }
function onPasswordChanged() { refresh() }

function copySecret() {
  navigator.clipboard?.writeText(newTokenSecret.value)
}

function fmtDate(d) {
  if (!d) return ''
  try { return new Date(d).toLocaleString() } catch { return d }
}

function isExpired(t) {
  return t.expires_at && new Date(t.expires_at) < new Date()
}

function shortUA(ua) {
  if (!ua) return 'Unknown'
  // Pull out the browser family for readability
  const m = ua.match(/(Chrome|Safari|Firefox|Edge|curl|python-requests|httpx|Postman)\/[\d.]+/)
  return m ? m[0] : ua.slice(0, 40)
}
</script>

<style scoped>
.tokens-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  background: var(--color-bg2);   /* canvas */
}

/* Page header — no border-bottom in Vercel; spacing alone separates it. */
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 18px 24px 16px;
  flex-shrink: 0;
}

.page-body {
  padding: 0 24px 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  overflow-y: auto;
}

/* Card primitive (extends global .panel) */
.card {
  padding: 0;          /* table needs flush edges; use card-head for inset */
  overflow: hidden;
}
.card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 20px;
}
.card-title {
  font-size: 12px;
  color: var(--color-t1);
  font-weight: 500;
}
.card-sub {
  font-size: 11px;
  color: var(--color-t3);
  margin-top: 2px;
}

.empty-cell {
  padding: 20px 16px;
  text-align: center;
  color: var(--color-t3);
  font-size: 11px;
  border-top: 1px solid var(--color-line);
}

/* Vercel-style table: header row in subtle bg, no zebra, hover row */
.t { width: 100%; border-collapse: collapse; font-size: 11px; }
.t thead tr { background: var(--color-bg2); }
.t th {
  text-align: left;
  padding: 9px 16px;
  font-size: 9px;
  font-weight: 500;
  color: var(--color-t3);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-top: 1px solid var(--color-line);
  border-bottom: 1px solid var(--color-line);
  white-space: nowrap;
}
.t td {
  padding: 11px 16px;
  border-top: 1px solid var(--color-line);
  color: var(--color-t1);
}
.t tbody tr:hover { background: var(--color-bg2); }
.t tbody tr:first-child td { border-top: none; }

.row-action {
  font-size: 11px;
  color: var(--color-err-fg);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0;
}
.row-action:hover { text-decoration: underline; }
</style>
