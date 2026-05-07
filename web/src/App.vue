<script setup>
import { ref, provide, onMounted, computed, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { listConversations, deleteConversation } from '@/api'
import { onUnauthorized } from '@/api/client'
import { getMe } from '@/api/auth'
import { useCapabilitiesStore } from '@/stores/capabilities'
import AppSidebar from '@/components/AppSidebar.vue'
import ChangePasswordModal from '@/components/ChangePasswordModal.vue'
import DialogHost from '@/components/DialogHost.vue'
import GlobalUploadPanel from '@/components/GlobalUploadPanel.vue'

const route = useRoute()
const router = useRouter()

const convs = ref([])
const convsLoading = ref(false)        // true on first load only — drives sidebar skeleton
const convsLoaded = ref(false)
const convsLoadingMore = ref(false)    // append-load in flight; sidebar shows tail spinner
const convsHasMore = ref(true)         // false once an empty page comes back
const deletingConvs = ref(new Set())   // optimistic-delete in-flight set
// Active conversation id is mirrored in the URL as ``?c=<id>`` so
// page refresh, browser back/forward, and shareable links all
// recover the conversation. The ref + the route stay in sync via
// the two watchers below: any setter on either side flows through
// to the other. Sidebar clicks call ``selectConv`` (push, gives a
// history entry); ``send()`` in Chat.vue calls
// ``setActiveConvIdNoHistory`` after creating a new conversation
// so URL reflects reality without polluting history with a
// /chat → /chat?c=X step.
const convId = ref(route.query.c || null)
const me = ref(null)
const showForcedPwd = ref(false)

const CONVS_PAGE_SIZE = 30

const isPublicRoute = computed(() => !!route.meta?.public)

async function loadConvs() {
  // First page only. Show the skeleton on the very first load —
  // refreshes after user actions (new chat / delete) keep the
  // existing list visible to avoid a re-flicker.
  if (!convsLoaded.value) convsLoading.value = true
  try {
    const res = await listConversations({ limit: CONVS_PAGE_SIZE, offset: 0 })
    const items = res?.items || []
    convs.value = items
    convsHasMore.value = items.length === CONVS_PAGE_SIZE
  } catch {
  } finally {
    convsLoading.value = false
    convsLoaded.value = true
  }
}

async function loadMoreConvs() {
  // Append-only: fetch the next page using current length as offset.
  // Bail out if a page is already in flight or we've hit the end.
  if (convsLoadingMore.value || !convsHasMore.value) return
  convsLoadingMore.value = true
  try {
    const res = await listConversations({
      limit: CONVS_PAGE_SIZE,
      offset: convs.value.length,
    })
    const items = res?.items || []
    if (items.length === 0) {
      convsHasMore.value = false
    } else {
      // De-dupe by conversation_id in case a new turn pushed an
      // older row across the page boundary between fetches.
      const seen = new Set(convs.value.map((c) => c.conversation_id))
      const fresh = items.filter((c) => !seen.has(c.conversation_id))
      convs.value = [...convs.value, ...fresh]
      convsHasMore.value = items.length === CONVS_PAGE_SIZE
    }
  } catch {
    // Don't flip hasMore on a transient error — let the user
    // scroll again to retry. The sentinel will fire once the
    // observer re-observes (it's idempotent).
  } finally {
    convsLoadingMore.value = false
  }
}

/* 401 interceptor — any authed call failing redirects to /login */
onUnauthorized((path) => {
  if (isPublicRoute.value) return
  if (path && path.includes('/auth/login')) return  // let the login form surface the error
  const dest = route.fullPath && route.fullPath !== '/login' ? route.fullPath : '/chat'
  router.push({ path: '/login', query: { redirect: dest } })
})

async function probeMe() {
  if (isPublicRoute.value) return
  try {
    me.value = await getMe()
    if (me.value?.must_change_password) showForcedPwd.value = true
    // Only load conversations once we know we're authed
    loadConvs()
  } catch {
    // onUnauthorized handler already redirected on 401
  }
}

function onPasswordChanged() {
  showForcedPwd.value = false
  if (me.value) me.value.must_change_password = false
}

const capabilities = useCapabilitiesStore()

onMounted(() => {
  probeMe()
  // Fetch /health features once so the upload UI can pre-flight
  // image / legacy-office gates without a per-upload round-trip.
  capabilities.refresh()
})

provide('me', me)

// ── URL ↔ convId bidirectional sync ────────────────────────────
// Why two watchers instead of a computed:
// Chat.vue still sets ``convId.value = ...`` directly (after
// createConversation) and we don't want to refactor every
// callsite to call a helper. Whoever mutates the ref or the URL
// first wins; the other side syncs. The ``cur === id`` guards
// stop the watch loops from ping-ponging when both already
// agree.
watch(() => route.query.c, (id) => {
  const next = id || null
  if (convId.value !== next) convId.value = next
})

watch(convId, (id) => {
  // Only sync URL while we're on a /chat path — flipping convId
  // shouldn't kick the user off /workspace etc. The ``selectConv``
  // helper handles cross-route nav explicitly.
  if (!route.path.startsWith('/chat')) return
  const cur = route.query.c || null
  if (cur === id) return
  // ``replace`` instead of ``push``: convId edits triggered by
  // Chat.vue creating a new conversation don't deserve a history
  // entry between /chat and /chat?c=X. Sidebar clicks go through
  // selectConv (below) which uses push for a real history entry.
  router.replace({ path: '/chat', query: id ? { c: id } : {} })
})

function selectConv(id) {
  // Sidebar click → real history entry so back button returns to
  // the previous conversation.
  router.push({ path: '/chat', query: id ? { c: id } : {} })
}
function newChat() {
  router.push({ path: '/chat' })
}

// Public API for non-sidebar callsites (Chat.vue's send() after
// createConversation) — replace, not push.
function setActiveConvIdNoHistory(id) {
  router.replace({ path: '/chat', query: id ? { c: id } : {} })
}

async function delConv(id) {
  if (deletingConvs.value.has(id)) return        // already in flight, ignore double-click
  // Optimistic: drop the row immediately, also navigate off if we're
  // viewing it. Snapshot for rollback so a server-side failure
  // restores it instead of leaving the UI inconsistent.
  const snapshot = convs.value
  const idx = convs.value.findIndex(c => c.conversation_id === id)
  const removed = idx >= 0 ? convs.value[idx] : null
  if (idx >= 0) convs.value = [...convs.value.slice(0, idx), ...convs.value.slice(idx + 1)]
  if (convId.value === id) {
    // Drop the deleted conversation from the URL too — the watch
    // on convId would handle it but explicit push gives a real
    // history entry the user can back out of.
    router.push({ path: '/chat' })
  }

  const next = new Set(deletingConvs.value); next.add(id); deletingConvs.value = next
  try {
    await deleteConversation(id)
    // Server-side delete confirmed — silently sync (no flicker since
    // the row is already gone optimistically).
    loadConvs()
  } catch {
    // Rollback: put the row back. Re-fetch from server in case the
    // backend state diverged for some other reason.
    if (removed) convs.value = snapshot
    loadConvs()
  } finally {
    const after = new Set(deletingConvs.value); after.delete(id); deletingConvs.value = after
  }
}

provide('convId', convId)
provide('setActiveConvIdNoHistory', setActiveConvIdNoHistory)
provide('convs', convs)
provide('convsLoading', convsLoading)
provide('deletingConvs', deletingConvs)
provide('loadConvs', loadConvs)
</script>

<template>
  <!-- Global dialog/toast host — always mounted, public or private routes -->
  <DialogHost />

  <!-- Public routes (login) render standalone, no sidebar -->
  <RouterView v-if="isPublicRoute" />

  <div v-else class="flex w-full h-screen">
    <AppSidebar
      :conversations="convs"
      :conversations-loading="convsLoading"
      :conversations-loading-more="convsLoadingMore"
      :conversations-has-more="convsHasMore"
      :deleting-convs="deletingConvs"
      :currentConvId="convId"
      :me="me"
      @select-conv="selectConv"
      @new-chat="newChat"
      @delete-conv="delConv"
      @load-more-conversations="loadMoreConvs"
    />
    <!-- Content host with cached pages. KeepAlive preserves component
         instances on navigation — state, scroll, DOM all survive. User
         coming back to any tab sees it instantly with prior data, no
         skeleton flicker. KnowledgeGraph included: its sigma instance
         survives across tab switches via onActivated/onDeactivated
         (window listeners bind/unbind on visibility; sigma + the graph
         it's rendering live as long as the cached component does).

         No <Transition> wrapper — instant route swaps. A fade animation
         on tab switches felt sluggish for a dashboard; cached pages
         already pop in with their prior state, which is the real win. -->
    <div class="flex-1 min-w-0 h-full route-host">
      <RouterView v-slot="{ Component }">
        <KeepAlive>
          <component :is="Component" />
        </KeepAlive>
      </RouterView>
    </div>

    <!-- Forced password change on first login (must_change_password=true) -->
    <ChangePasswordModal
      :open="showForcedPwd"
      :forced="true"
      @changed="onPasswordChanged"
    />

    <!-- Global upload queue — always-visible status bar when there's activity -->
    <GlobalUploadPanel />
  </div>
</template>

<style>
.route-host {
  position: relative;
  overflow: hidden;
  background: var(--color-bg2);
}
</style>
