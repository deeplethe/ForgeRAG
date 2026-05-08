<script setup>
/**
 * /settings/users — admin-only user management.
 *
 * Lists every account in the workspace, exposing the four
 * status transitions (approve / suspend / reactivate / delete)
 * + role flips (admin ↔ user). Wires directly to the
 * /api/v1/admin/users surface defined in api/routes/admin.py.
 *
 * Layout pattern:
 *   - One row per user (table semantics for screen-readers,
 *     CSS grid for the responsive layout — narrow viewports
 *     collapse Last-login + status into a single meta line).
 *   - Filter chips (status) + a free-text search.
 *   - Per-row "More" menu houses the destructive actions
 *     so the table doesn't get cluttered with red buttons.
 *   - Confirm dialogs (useDialog) for every irreversible
 *     action: suspend, demote, delete.
 *
 * Self-protection (mirrors the backend's ``cannot X yourself``
 * gates in admin.py) — we DISABLE rather than HIDE the
 * disallowed actions on the current user's own row, so admins
 * understand WHY the option isn't doing anything.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  listUsers,
  approveUser,
  suspendUser,
  reactivateUser,
  patchUser,
  deleteUser,
  listUserUsage,
  avatarUrlFor,
} from '@/api/admin'
import { getMe } from '@/api/auth'
import { useDialog } from '@/composables/useDialog'
import { Search, MoreHorizontal, ShieldCheck, ShieldOff, Trash2, UserCheck, PauseCircle } from 'lucide-vue-next'
import UserAvatar from '@/components/UserAvatar.vue'

const { t } = useI18n()
const { confirm, toast } = useDialog()

const users = ref([])
const me = ref(null)
const loading = ref(true)
// Per-user usage: { user_id → { input_tokens, output_tokens, total_tokens, message_count } }
// Fetched once on mount; not refreshed on row mutations because the
// numbers don't change with role/status edits — only after new chats.
const usageByUser = ref({})
const filter = ref('all') // 'all' | 'active' | 'pending_approval' | 'suspended'
const query = ref('')
const openMenuId = ref(null)
// Viewport-coordinate placement for the per-row "more" menu. The
// menu is teleported to <body> with position: fixed so the table's
// ``overflow: hidden`` (needed for rounded-corner clipping) doesn't
// chop it off when it's wider/taller than the actions cell. Anchored
// to the trigger's bottom-right by default; flips upward when there
// isn't room below. Either ``top`` or ``bottom`` is set depending
// on placement — the unused side stays ``null`` and CSS ignores it.
const menuPos = ref({ top: null, bottom: null, right: 0, placement: 'down' })
const busyId = ref(null) // user_id currently mutating — disables that row

const filteredUsers = computed(() => {
  const q = query.value.trim().toLowerCase()
  return users.value.filter((u) => {
    if (filter.value !== 'all' && u.status !== filter.value) return false
    if (!q) return true
    const hay = [u.email, u.display_name, u.username].filter(Boolean).join(' ').toLowerCase()
    return hay.includes(q)
  })
})

onMounted(async () => {
  // Pull /me, /users, and /users/usage all in parallel — none of
  // them depends on another, and the page can paint the moment
  // any one comes back. Usage failure isn't fatal: the column
  // just shows zeros if it doesn't load.
  const [_me, _users, _usage] = await Promise.allSettled([
    getMe(),
    listUsers(),
    listUserUsage(),
  ])
  if (_me.status === 'fulfilled') me.value = _me.value
  if (_users.status === 'fulfilled') users.value = _users.value
  else toast(t('settings.users.error_load'), { variant: 'error' })
  if (_usage.status === 'fulfilled') {
    const map = {}
    for (const row of _usage.value || []) map[row.user_id] = row
    usageByUser.value = map
  }
  loading.value = false
})

function usageOf(u) {
  return usageByUser.value[u.user_id] || null
}

function fmtNum(n) {
  return (n || 0).toLocaleString()
}

// Click-outside closes any open per-row menu. The menu is small
// (3-4 items), so a global listener is simpler than per-row refs.
// We accept clicks on either the trigger cell (.row-menu) OR the
// teleported popover (.menu-popover) — without the second check,
// any click inside the menu would close it before its handler ran.
function onDocClick(e) {
  if (!openMenuId.value) return
  if (e.target.closest('.row-menu') || e.target.closest('.menu-popover')) return
  openMenuId.value = null
}
// Scroll inside the table or anywhere on the page invalidates the
// popover's anchor — easier to close it than to follow the trigger.
function onScrollOrResize() {
  if (openMenuId.value) openMenuId.value = null
}
watch(openMenuId, (v) => {
  if (v) {
    document.addEventListener('click', onDocClick)
    // Capture phase so it fires for scroll events on inner containers too.
    window.addEventListener('scroll', onScrollOrResize, true)
    window.addEventListener('resize', onScrollOrResize)
  } else {
    document.removeEventListener('click', onDocClick)
    window.removeEventListener('scroll', onScrollOrResize, true)
    window.removeEventListener('resize', onScrollOrResize)
  }
})

function toggleMenu(u, evt) {
  if (openMenuId.value === u.user_id) {
    openMenuId.value = null
    return
  }
  // Anchor to the trigger button's viewport rect. We position with
  // (top, right) so the menu is right-aligned to the icon — the same
  // visual line as before, just escaped from the table's clipping.
  const rect = evt.currentTarget.getBoundingClientRect()
  const MENU_HEIGHT_GUESS = 140  // 3 items + divider + padding; cheap heuristic
  const spaceBelow = window.innerHeight - rect.bottom
  const placement = spaceBelow < MENU_HEIGHT_GUESS && rect.top > MENU_HEIGHT_GUESS
    ? 'up'
    : 'down'
  menuPos.value = {
    top: placement === 'down' ? rect.bottom + 4 : null,
    bottom: placement === 'up' ? window.innerHeight - rect.top + 4 : null,
    right: Math.max(8, window.innerWidth - rect.right),
    placement,
  }
  openMenuId.value = u.user_id
}

function isMe(u) {
  return me.value && u.user_id === me.value.user_id
}

function nameOf(u) {
  return u.display_name || (u.email ? u.email.split('@')[0] : null) || u.username
}

function avatarKey(u) {
  return (u.display_name || u.email || u.username || '').trim()
}

function fmtDate(d) {
  if (!d) return t('settings.users.never_logged_in')
  try {
    return new Date(d).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
    })
  } catch {
    return d
  }
}

// ── Per-row mutation helpers ───────────────────────────────────
// Each mutation:
//   1. Optionally confirm via useDialog
//   2. Mark the row busy (UI disables actions)
//   3. Call backend, replace the row in `users` with response
//   4. Toast success / show error
//   5. Close any open menu

async function _runMutation(u, action, opts = {}) {
  busyId.value = u.user_id
  openMenuId.value = null
  try {
    const updated = await action()
    if (updated && updated.user_id) {
      const idx = users.value.findIndex((x) => x.user_id === u.user_id)
      if (idx >= 0) users.value[idx] = updated
    } else if (opts.removeOnSuccess) {
      users.value = users.value.filter((x) => x.user_id !== u.user_id)
    }
    if (opts.successMessage) toast(opts.successMessage, { variant: 'success' })
  } catch (e) {
    toast(t('settings.users.error_action', { msg: e.message || '' }), { variant: 'error' })
  } finally {
    busyId.value = null
  }
}

function onApprove(u) {
  return _runMutation(u, () => approveUser(u.user_id))
}

async function onSuspend(u) {
  if (isMe(u)) {
    toast(t('settings.users.error_self_suspend'), { variant: 'warn' })
    return
  }
  const ok = await confirm({
    title: t('settings.users.suspend_confirm_title', { name: nameOf(u) }),
    description: t('settings.users.suspend_confirm_desc'),
    confirmText: t('settings.users.suspend_confirm_button'),
    variant: 'destructive',
  })
  if (!ok) return
  return _runMutation(u, () => suspendUser(u.user_id))
}

function onReactivate(u) {
  return _runMutation(u, () => reactivateUser(u.user_id))
}

async function onToggleAdmin(u) {
  const promote = u.role !== 'admin'
  if (!promote && isMe(u)) {
    toast(t('settings.users.error_self_demote'), { variant: 'warn' })
    return
  }
  const ok = await confirm({
    title: promote
      ? t('settings.users.promote_confirm_title', { name: nameOf(u) })
      : t('settings.users.demote_confirm_title', { name: nameOf(u) }),
    description: promote
      ? t('settings.users.promote_confirm_desc')
      : t('settings.users.demote_confirm_desc'),
    confirmText: promote
      ? t('settings.users.promote_confirm_button')
      : t('settings.users.demote_confirm_button'),
    variant: promote ? 'default' : 'destructive',
  })
  if (!ok) return
  return _runMutation(u, () => patchUser(u.user_id, { role: promote ? 'admin' : 'user' }))
}

async function onDelete(u) {
  if (isMe(u)) {
    toast(t('settings.users.error_self_delete'), { variant: 'warn' })
    return
  }
  const ok = await confirm({
    title: t('settings.users.delete_confirm_title', { name: nameOf(u) }),
    description: t('settings.users.delete_confirm_desc'),
    confirmText: t('settings.users.delete_confirm_button'),
    variant: 'destructive',
  })
  if (!ok) return
  return _runMutation(
    u,
    async () => {
      await deleteUser(u.user_id)
      return null
    },
    { removeOnSuccess: true },
  )
}

function statusLabel(s) {
  return {
    active: t('settings.users.status_active'),
    pending_approval: t('settings.users.status_pending'),
    suspended: t('settings.users.status_suspended'),
    deleted: t('settings.users.status_deleted'),
  }[s] || s
}

// The "promote yourself to admin" no-op is harmless; the only
// disallowed flip is "demote yourself" which the backend rejects
// with a 400. Mirror that here so the menu greys it out instead
// of bouncing through an error toast.
function promotable(u) {
  return !(isMe(u) && u.role === 'admin')
}
</script>

<template>
  <div class="users-page">
    <header class="page-header">
      <h2 class="page-title">{{ t('settings.users.title') }}</h2>
      <p class="page-subtitle">{{ t('settings.users.subtitle') }}</p>
    </header>

    <!-- ── Filter + search ── -->
    <div class="toolbar">
      <div class="filter-chips">
        <button
          v-for="f in [
            { key: 'all', label: t('settings.users.filter_all') },
            { key: 'active', label: t('settings.users.filter_active') },
            { key: 'pending_approval', label: t('settings.users.filter_pending') },
            { key: 'suspended', label: t('settings.users.filter_suspended') },
          ]"
          :key="f.key"
          class="chip"
          :class="{ 'is-active': filter === f.key }"
          @click="filter = f.key"
        >{{ f.label }}</button>
      </div>
      <div class="search-wrap">
        <Search :size="14" :stroke-width="1.75" class="search-icon" />
        <input
          v-model="query"
          class="search-input"
          :placeholder="t('settings.users.search_placeholder')"
        />
      </div>
    </div>

    <!-- ── Table ── -->
    <div class="table">
      <div class="table-head">
        <div class="col-user">{{ t('settings.users.col_user') }}</div>
        <div class="col-role">{{ t('settings.users.col_role') }}</div>
        <div class="col-status">{{ t('settings.users.col_status') }}</div>
        <div class="col-usage">{{ t('settings.usage.total_tokens') }}</div>
        <div class="col-login">{{ t('settings.users.col_last_login') }}</div>
        <div class="col-actions"></div>
      </div>

      <div v-if="loading" class="empty">{{ t('settings.users.loading') }}</div>
      <div v-else-if="!filteredUsers.length" class="empty">{{ t('settings.users.empty') }}</div>

      <div
        v-for="u in filteredUsers"
        :key="u.user_id"
        class="row"
        :class="{ 'is-busy': busyId === u.user_id, 'is-pending': u.status === 'pending_approval', 'is-suspended': u.status === 'suspended' }"
      >
        <!-- User column: avatar + name + email -->
        <div class="col-user user-cell">
          <UserAvatar
            :name="avatarKey(u)"
            :img-url="avatarUrlFor(u.user_id, u.has_avatar)"
            :size="28"
          />
          <div class="user-meta">
            <div class="name-row">
              <span class="name">{{ nameOf(u) }}</span>
              <span v-if="isMe(u)" class="you-badge">{{ t('settings.users.you_badge') }}</span>
            </div>
            <div class="email">{{ u.email || u.username }}</div>
          </div>
        </div>

        <!-- Role -->
        <div class="col-role">
          <span class="role-pill" :class="{ 'role-admin': u.role === 'admin' }">
            {{ u.role === 'admin' ? t('settings.users.role_admin') : t('settings.users.role_user') }}
          </span>
        </div>

        <!-- Status -->
        <div class="col-status">
          <span class="status-pill" :class="`status-${u.status}`">
            {{ statusLabel(u.status) }}
          </span>
        </div>

        <!-- Usage (admin sees per-user token totals) -->
        <div class="col-usage" :title="usageOf(u) ? `in ${fmtNum(usageOf(u).input_tokens)} · out ${fmtNum(usageOf(u).output_tokens)} · ${fmtNum(usageOf(u).message_count)} answers` : ''">
          <span class="usage-num">{{ fmtNum(usageOf(u)?.total_tokens) }}</span>
        </div>

        <!-- Last login -->
        <div class="col-login">{{ fmtDate(u.last_login_at) }}</div>

        <!-- Actions: primary inline button + "more" menu -->
        <div class="col-actions row-menu">
          <!-- Primary action depends on status -->
          <button
            v-if="u.status === 'pending_approval'"
            class="btn-inline"
            :disabled="busyId === u.user_id"
            @click="onApprove(u)"
          >
            <UserCheck :size="13" :stroke-width="1.75" />
            {{ t('settings.users.action_approve') }}
          </button>
          <button
            v-else-if="u.status === 'suspended'"
            class="btn-inline"
            :disabled="busyId === u.user_id"
            @click="onReactivate(u)"
          >
            <UserCheck :size="13" :stroke-width="1.75" />
            {{ t('settings.users.action_reactivate') }}
          </button>

          <!-- "More" menu -->
          <button
            class="icon-btn"
            :disabled="busyId === u.user_id"
            :aria-label="t('settings.users.menu_more')"
            @click.stop="toggleMenu(u, $event)"
          >
            <MoreHorizontal :size="15" :stroke-width="1.75" />
          </button>

          <!-- Popover is teleported to <body> so the table's
               overflow: hidden (for rounded corners) doesn't clip
               it. Position is computed from the trigger's bounding
               rect on every open. -->
          <Teleport to="body">
            <div
              v-if="openMenuId === u.user_id"
              class="menu-popover"
              :class="{ 'menu-popover--up': menuPos.placement === 'up' }"
              :style="{
                top: menuPos.top != null ? menuPos.top + 'px' : null,
                bottom: menuPos.bottom != null ? menuPos.bottom + 'px' : null,
                right: menuPos.right + 'px',
              }"
              @click.stop
            >
              <button
                v-if="u.status === 'active'"
                class="menu-item"
                :disabled="isMe(u)"
                @click="onSuspend(u)"
              >
                <PauseCircle :size="13" :stroke-width="1.75" />
                {{ t('settings.users.action_suspend') }}
              </button>
              <button
                class="menu-item"
                :disabled="!promotable(u)"
                @click="onToggleAdmin(u)"
              >
                <component :is="u.role === 'admin' ? ShieldOff : ShieldCheck" :size="13" :stroke-width="1.75" />
                {{ u.role === 'admin' ? t('settings.users.action_make_user') : t('settings.users.action_make_admin') }}
              </button>
              <div class="menu-divider"></div>
              <button
                class="menu-item is-destructive"
                :disabled="isMe(u)"
                @click="onDelete(u)"
              >
                <Trash2 :size="13" :stroke-width="1.75" />
                {{ t('settings.users.action_delete') }}
              </button>
            </div>
          </Teleport>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Let the page fill the entire Settings content area so the
   toolbar's right-aligned search and the table's right edge
   land at the same x. ``width: 100%`` is explicit because the
   child grid (``.row``)'s ``minmax(_, fr)`` was sizing to
   intrinsic content rather than parent width without it,
   leaving the table noticeably shorter than the toolbar. */
.users-page { width: 100%; }

.page-header { margin-bottom: 20px; }
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

/* ── Toolbar (filters + search) ─────────────────────────────── */
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.filter-chips { display: flex; gap: 4px; }
.chip {
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  cursor: pointer;
  transition: background-color .12s, color .12s, border-color .12s;
}
.chip:hover { background: var(--color-bg2); color: var(--color-t1); }
.chip.is-active {
  background: var(--color-bg2);
  border-color: var(--color-line);
  color: var(--color-t1);
  font-weight: 500;
}

.search-wrap {
  position: relative;
  flex: 0 0 260px;
}
.search-icon {
  position: absolute;
  left: 8px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--color-t3);
  pointer-events: none;
}
.search-input {
  width: 100%;
  height: 28px;
  padding: 0 8px 0 28px;
  font-size: 12px;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  background: var(--color-bg);
  color: var(--color-t1);
  outline: none;
}
.search-input:focus { border-color: var(--color-line2); box-shadow: var(--ring-focus); }

/* ── Table ──────────────────────────────────────────────────── */
/* ``display: block`` is explicit to override Tailwind's bare
   ``.table`` utility (display: table), which otherwise wins
   because the scoped ruleset doesn't redeclare ``display`` and
   table-display elements size to their content rather than
   stretching to fill the parent. That mismatch was leaving the
   table noticeably shorter than the toolbar's right-aligned
   search input on wide viewports. */
.table {
  display: block;
  width: 100%;
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  background: var(--color-bg);
  overflow: hidden;
}
.table-head, .row {
  display: grid;
  grid-template-columns: minmax(220px, 1.6fr) 80px 100px 90px 110px 130px;
  gap: 12px;
  align-items: center;
  padding: 0 16px;
}
.col-usage {
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--color-t2);
}
.usage-num {
  font-size: 12px;
}
.table-head {
  height: 36px;
  background: var(--color-bg2);
  border-bottom: 1px solid var(--color-line);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--color-t3);
}
.row {
  height: 56px;
  border-bottom: 1px solid var(--color-line);
  font-size: 13px;
  transition: background-color .12s, opacity .15s;
}
.row:last-child { border-bottom: none; }
.row:hover { background: var(--color-bg2); }
.row.is-busy { opacity: .55; pointer-events: none; }

.empty {
  padding: 32px;
  text-align: center;
  color: var(--color-t3);
  font-size: 12px;
}

/* User cell */
.user-cell { display: flex; align-items: center; gap: 10px; min-width: 0; }
.user-meta { min-width: 0; }
.name-row { display: flex; align-items: center; gap: 6px; }
.name {
  font-weight: 500;
  color: var(--color-t1);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.you-badge {
  font-size: 10px;
  font-weight: 500;
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--color-bg3);
  color: var(--color-t2);
}
.email {
  font-size: 11px;
  color: var(--color-t3);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* Role pill */
.role-pill {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 3px;
  background: var(--color-bg3);
  color: var(--color-t2);
}
.role-pill.role-admin {
  background: color-mix(in srgb, var(--color-accent, #6366f1) 12%, transparent);
  color: var(--color-accent, #6366f1);
}

/* Status pill — colours follow the design rule:
     terminal/blocking states get colour, neutral states stay grey.
   active   → green   (working state, but de-emphasised)
   pending  → amber   (needs admin attention)
   suspended→ red     (bad terminal state)
   deleted  → grey    (rare; row usually gone) */
.status-pill {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 3px;
}
.status-pill.status-active {
  background: var(--color-bg3);
  color: var(--color-t2);
}
.status-pill.status-pending_approval {
  background: color-mix(in srgb, #f59e0b 14%, transparent);
  color: #b45309;
}
.status-pill.status-suspended {
  background: color-mix(in srgb, #ef4444 14%, transparent);
  color: #b91c1c;
}
.status-pill.status-deleted {
  background: var(--color-bg3);
  color: var(--color-t3);
}

.col-login { font-size: 12px; color: var(--color-t3); }

/* Actions cell */
.col-actions {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
}
.btn-inline {
  display: inline-flex; align-items: center; gap: 4px;
  height: 26px; padding: 0 8px;
  font-size: 11px; font-weight: 500;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  color: var(--color-t1);
  cursor: pointer;
  transition: background-color .12s, border-color .12s;
}
.btn-inline:hover:not(:disabled) {
  background: var(--color-bg3);
  border-color: var(--color-line2);
}
.btn-inline:disabled { opacity: .5; cursor: not-allowed; }

.icon-btn {
  height: 26px; width: 26px;
  display: inline-flex; align-items: center; justify-content: center;
  background: transparent;
  border: none;
  border-radius: var(--r-sm);
  color: var(--color-t2);
  cursor: pointer;
  transition: background-color .12s, color .12s;
}
.icon-btn:hover:not(:disabled) { background: var(--color-bg3); color: var(--color-t1); }
.icon-btn:disabled { opacity: .4; cursor: not-allowed; }

/* Per-row dropdown menu — teleported to <body>, viewport-positioned
   via inline ``top``/``bottom``/``right`` from ``menuPos``. Lives
   outside the table so ``.table { overflow: hidden }`` doesn't
   clip it. */
.menu-popover {
  position: fixed;
  min-width: 180px;
  padding: 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
  z-index: 50;
}
.menu-item {
  display: flex; align-items: center; gap: 8px;
  width: 100%;
  padding: 6px 8px;
  font-size: 12px;
  color: var(--color-t1);
  background: transparent;
  border: none;
  border-radius: var(--r-sm);
  text-align: left;
  cursor: pointer;
  transition: background-color .1s, color .1s;
}
.menu-item:hover:not(:disabled) { background: var(--color-bg2); }
.menu-item:disabled { opacity: .45; cursor: not-allowed; }
.menu-item.is-destructive { color: var(--color-err-fg, #b91c1c); }
.menu-item.is-destructive:hover:not(:disabled) {
  background: color-mix(in srgb, #ef4444 8%, transparent);
}
.menu-divider {
  height: 1px;
  background: var(--color-line);
  margin: 4px 2px;
}
</style>
