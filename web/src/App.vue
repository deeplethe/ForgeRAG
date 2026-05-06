<script setup>
import { ref, provide, onMounted, computed } from 'vue'
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
const deletingConvs = ref(new Set())   // optimistic-delete in-flight set
const convId = ref(null)
const me = ref(null)
const showForcedPwd = ref(false)

const isPublicRoute = computed(() => !!route.meta?.public)

async function loadConvs() {
  // Show the skeleton only on the very first load — refreshes after
  // user actions (new chat / delete) keep the existing list visible to
  // avoid an annoying re-flicker.
  if (!convsLoaded.value) convsLoading.value = true
  try {
    convs.value = (await listConversations({ limit: 100 })).items || []
  } catch {
  } finally {
    convsLoading.value = false
    convsLoaded.value = true
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

function selectConv(id) { convId.value = id }
function newChat() { convId.value = null }
async function delConv(id) {
  if (deletingConvs.value.has(id)) return        // already in flight, ignore double-click
  // Optimistic: drop the row immediately, also navigate off if we're
  // viewing it. Snapshot for rollback so a server-side failure
  // restores it instead of leaving the UI inconsistent.
  const snapshot = convs.value
  const idx = convs.value.findIndex(c => c.conversation_id === id)
  const removed = idx >= 0 ? convs.value[idx] : null
  if (idx >= 0) convs.value = [...convs.value.slice(0, idx), ...convs.value.slice(idx + 1)]
  if (convId.value === id) convId.value = null

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
      :deleting-convs="deletingConvs"
      :currentConvId="convId"
      :me="me"
      @select-conv="selectConv"
      @new-chat="newChat"
      @delete-conv="delConv"
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
