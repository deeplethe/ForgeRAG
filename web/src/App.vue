<script setup>
import { ref, provide, onMounted, onUnmounted, computed } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { listConversations, deleteConversation, getBenchmarkStatus } from '@/api'
import { onUnauthorized } from '@/api/client'
import { getMe } from '@/api/auth'
import AppSidebar from '@/components/AppSidebar.vue'
import ChangePasswordModal from '@/components/ChangePasswordModal.vue'
import DialogHost from '@/components/DialogHost.vue'
import GlobalUploadPanel from '@/components/GlobalUploadPanel.vue'

const route = useRoute()
const router = useRouter()

const convs = ref([])
const convId = ref(null)
const benchmarkRunning = ref(false)
const me = ref(null)
const showForcedPwd = ref(false)

const isPublicRoute = computed(() => !!route.meta?.public)

async function loadConvs() {
  try { convs.value = (await listConversations({ limit: 100 })).items || [] } catch {}
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

/* Poll benchmark status to lock/unlock tabs */
let _bmPoll = null
async function pollBenchmark() {
  if (isPublicRoute.value) return
  try {
    const s = await getBenchmarkStatus()
    benchmarkRunning.value = ['generating', 'running', 'scoring'].includes(s?.status)
  } catch { benchmarkRunning.value = false }
}
onMounted(() => {
  probeMe()
  pollBenchmark()
  _bmPoll = setInterval(pollBenchmark, 3000)
})
onUnmounted(() => { if (_bmPoll) clearInterval(_bmPoll) })

provide('me', me)

provide('benchmarkRunning', benchmarkRunning)

function selectConv(id) { convId.value = id }
function newChat() { convId.value = null }
async function delConv(id) {
  try { await deleteConversation(id) } catch {}
  if (convId.value === id) convId.value = null
  loadConvs()
}

provide('convId', convId)
provide('convs', convs)
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
      :currentConvId="convId"
      :benchmarkRunning="benchmarkRunning"
      :me="me"
      @select-conv="selectConv"
      @new-chat="newChat"
      @delete-conv="delConv"
    />
    <!-- Content host with cached pages. KeepAlive preserves component
         instances on navigation — state, scroll, DOM all survive. User
         coming back to Workspace / Metrics / Tokens etc. sees them
         instantly with prior data, no skeleton flicker.

         KnowledgeGraph is EXCLUDED: its sigma WebGL teardown logic runs
         in onBeforeRouteLeave + onUnmounted, and re-initialization needs
         a real fresh mount. Caching it would tangle the lifecycle.

         No <Transition> wrapper — instant route swaps. A fade animation
         on tab switches felt sluggish for a dashboard; cached pages
         already pop in with their prior state, which is the real win. -->
    <div class="flex-1 min-w-0 h-full route-host">
      <RouterView v-slot="{ Component }">
        <KeepAlive :exclude="['KnowledgeGraph']">
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
