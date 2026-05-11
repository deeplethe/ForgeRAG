<template>
  <!--
    Notification bell — sits in the app header. Shows a count badge
    when conversations have agent answers the user hasn't read yet
    (``conversation.unread === true``, computed server-side as
    ``last_assistant_at > last_read_at``).

    Click opens a small dropdown listing the unread conversations
    newest-first; clicking an item navigates to that chat (which
    bumps ``last_read_at`` server-side via the existing /read
    route + watch in Chat.vue).

    Polling cadence: 30s. The same data drives the sidebar's blue
    dots; this is just an aggregated "what changed while I wasn't
    looking at the sidebar" view.

    No new backend — uses ``listConversations({limit, unread})``
    that's already wired.
  -->
  <div ref="rootEl" class="bell-wrap">
    <button
      type="button"
      class="bell-trigger"
      :class="{ 'bell-trigger--has-unread': unreadCount > 0, 'bell-trigger--open': open }"
      :title="unreadCount
        ? t('notifications.tooltip_unread', { n: unreadCount })
        : t('notifications.tooltip_idle')"
      @click="toggle"
    >
      <BellIcon :size="16" :stroke-width="1.6" />
      <span v-if="unreadCount > 0" class="bell-badge">{{ unreadCount > 99 ? '99+' : unreadCount }}</span>
    </button>

    <Transition name="popup">
      <div v-if="open" class="bell-menu" @click.stop>
        <div class="bell-menu__head">
          <span class="bell-menu__title">{{ t('notifications.title') }}</span>
          <button
            v-if="unreadCount > 0"
            class="bell-menu__mark-all"
            :title="t('notifications.mark_all_tooltip')"
            @click="markAllRead"
          >{{ t('notifications.mark_all') }}</button>
        </div>

        <div v-if="loading && unreadList.length === 0" class="bell-menu__hint">
          {{ t('common.loading') }}
        </div>
        <div v-else-if="unreadList.length === 0" class="bell-menu__empty">
          <div class="bell-menu__empty-icon"><BellOff :size="20" :stroke-width="1.3" /></div>
          <div class="bell-menu__empty-text">{{ t('notifications.empty') }}</div>
        </div>
        <div v-else class="bell-menu__list">
          <button
            v-for="c in unreadList"
            :key="c.conversation_id"
            class="bell-item"
            @click="openConv(c)"
          >
            <div class="bell-item__dot" />
            <div class="bell-item__body">
              <div class="bell-item__title">{{ c.title || t('notifications.untitled') }}</div>
              <div class="bell-item__meta">{{ fmtAgo(c.last_assistant_at) }}</div>
            </div>
          </button>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Bell as BellIcon, BellOff } from 'lucide-vue-next'
import { listConversations, markConversationRead } from '@/api'

const { t } = useI18n()
const router = useRouter()

const rootEl = ref(null)
const open = ref(false)
// Cap at 50 — anything beyond that the user goes to the sidebar to
// triage. Bell is for "what changed since I last looked", not a
// general inbox.
const PAGE = 50
const POLL_INTERVAL_MS = 30_000

const conversations = ref([])
const loading = ref(false)
let _pollTimer = null

const unreadList = computed(() => {
  return conversations.value
    .filter((c) => c.unread)
    .sort((a, b) => {
      const ta = a.last_assistant_at ? Date.parse(a.last_assistant_at) : 0
      const tb = b.last_assistant_at ? Date.parse(b.last_assistant_at) : 0
      return tb - ta
    })
})
const unreadCount = computed(() => unreadList.value.length)

async function refresh() {
  loading.value = true
  try {
    const res = await listConversations({ limit: PAGE, offset: 0 })
    conversations.value = res?.items || []
  } catch { /* non-fatal — keep stale data */ } finally {
    loading.value = false
  }
}

function toggle() {
  open.value = !open.value
  if (open.value) refresh()
}
function close() { open.value = false }

function openConv(c) {
  close()
  router.push({ path: `/chat/${c.conversation_id}` })
  // Optimistic local clear so the badge updates without waiting for
  // the next poll — the route navigation triggers Chat.vue's
  // markConversationRead call which is the canonical clear.
  conversations.value = conversations.value.map((row) =>
    row.conversation_id === c.conversation_id ? { ...row, unread: false } : row,
  )
}

async function markAllRead() {
  // POST mark-read for every currently-unread conv in parallel.
  // Best-effort; errors silently leave the row "unread" and the
  // next poll syncs reality.
  const targets = unreadList.value.slice()
  for (const c of targets) {
    try { markConversationRead(c.conversation_id) } catch { /* swallow */ }
  }
  conversations.value = conversations.value.map((row) =>
    row.unread ? { ...row, unread: false } : row,
  )
}

// Click-outside handler for the dropdown.
function _onOutsideClick(e) {
  if (!open.value || !rootEl.value) return
  if (!rootEl.value.contains(e.target)) close()
}

onMounted(() => {
  refresh()
  _pollTimer = setInterval(refresh, POLL_INTERVAL_MS)
  document.addEventListener('mousedown', _onOutsideClick)
})
onBeforeUnmount(() => {
  if (_pollTimer) clearInterval(_pollTimer)
  document.removeEventListener('mousedown', _onOutsideClick)
})

// ── Relative-time formatter for the per-row meta ──────────────
function fmtAgo(iso) {
  if (!iso) return ''
  // Backend writes ``datetime.utcnow()`` and Pydantic serialises
  // without a timezone marker ("2026-05-11T07:57:57.139225"). JS's
  // ``Date.parse`` treats that as LOCAL time, which gives the
  // wrong offset wherever the user isn't in UTC. Append 'Z' if
  // the ISO doesn't already carry a marker so it's parsed as UTC.
  let raw = iso
  if (typeof raw === 'string' && !/[zZ]$|[+-]\d{2}:?\d{2}$/.test(raw)) {
    raw = raw + 'Z'
  }
  const t0 = typeof raw === 'string' ? Date.parse(raw) : Number(raw)
  if (!Number.isFinite(t0)) return ''
  const s = Math.max(0, Math.floor((Date.now() - t0) / 1000))
  if (s < 30) return t('notifications.just_now')
  if (s < 90) return t('notifications.a_min_ago')
  if (s < 3600) return t('notifications.n_mins_ago', { n: Math.floor(s / 60) })
  if (s < 7200) return t('notifications.an_hour_ago')
  if (s < 86400) return t('notifications.n_hours_ago', { n: Math.floor(s / 3600) })
  if (s < 172800) return t('notifications.yesterday')
  return t('notifications.n_days_ago', { n: Math.floor(s / 86400) })
}
</script>

<style scoped>
.bell-wrap {
  position: relative;
  display: inline-flex;
  align-items: center;
}
.bell-trigger {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 6px;
  background: transparent;
  border: none;
  color: var(--color-t2);
  cursor: pointer;
  transition: background-color .12s, color .12s;
}
.bell-trigger:hover,
.bell-trigger--open {
  background: var(--color-bg3);
  color: var(--color-t1);
}
.bell-trigger--has-unread { color: var(--color-t1); }
.bell-badge {
  position: absolute;
  top: 2px;
  right: 2px;
  min-width: 14px;
  height: 14px;
  padding: 0 3px;
  border-radius: 7px;
  background: var(--color-brand);
  color: #fff;
  font-size: 0.5625rem;
  font-weight: 600;
  font-feature-settings: "tnum";
  display: inline-flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
  letter-spacing: -0.02em;
}

.bell-menu {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 50;
  width: 320px;
  max-height: 460px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 10px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.bell-menu__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--color-line);
}
.bell-menu__title {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--color-t1);
}
.bell-menu__mark-all {
  font-size: 0.625rem;
  color: var(--color-brand);
  background: transparent;
  border: none;
  cursor: pointer;
}
.bell-menu__mark-all:hover { color: var(--color-brand-hover); }
.bell-menu__hint {
  padding: 24px 16px;
  text-align: center;
  font-size: 0.6875rem;
  color: var(--color-t3);
}
.bell-menu__empty {
  padding: 32px 20px;
  text-align: center;
  color: var(--color-t3);
}
.bell-menu__empty-icon { margin-bottom: 8px; }
.bell-menu__empty-text { font-size: 0.6875rem; }
.bell-menu__list {
  flex: 1;
  overflow-y: auto;
}
.bell-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  padding: 10px 14px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  transition: background-color .12s;
}
.bell-item:hover { background: var(--color-bg-soft); }
.bell-item__dot {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--color-brand);
}
.bell-item__body { min-width: 0; flex: 1; }
.bell-item__title {
  font-size: 0.75rem;
  color: var(--color-t1);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.bell-item__meta {
  font-size: 0.625rem;
  color: var(--color-t3);
  margin-top: 2px;
}

.popup-enter-active, .popup-leave-active { transition: opacity .15s ease, transform .15s ease; }
.popup-enter-from, .popup-leave-to { opacity: 0; transform: translateY(-4px); }
</style>
