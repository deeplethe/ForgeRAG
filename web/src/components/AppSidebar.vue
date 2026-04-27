<script setup>
import { useRouter, useRoute } from 'vue-router'
import { logout } from '@/api/auth'
import { useTheme } from '@/composables/useTheme'
import { useDialog } from '@/composables/useDialog'

const { isDark, toggleTheme } = useTheme()
const { confirm } = useDialog()

const router = useRouter()
const route = useRoute()

const props = defineProps({
  conversations: Array,
  currentConvId: String,
  benchmarkRunning: { type: Boolean, default: false },
  me: { type: Object, default: null },
})
const emit = defineEmits(['select-conv', 'new-chat', 'delete-conv'])

const tabs = [
  { path: '/chat', label: 'Chat', isChat: true },
  { path: '/workspace', label: 'Workspace' },
  { path: '/knowledge-graph', label: 'Knowledge Graph' },
  { path: '/simulation', label: 'Simulation' },
  { path: '/metrics', label: 'Metrics' },
  { path: '/benchmark', label: 'Benchmark', dev: true },
  { path: '/tokens', label: 'Tokens & Sessions' },
]

async function onLogout() {
  const ok = await confirm({
    title: 'Sign out?',
    description: 'You will be returned to the login screen.',
    confirmText: 'Sign out',
  })
  if (!ok) return
  try { await logout() } catch {}
  window.location.href = '/login'
}

function isTabDisabled(t) {
  if (t.dev) return true
  // When benchmark is running, only the Benchmark tab is clickable
  return props.benchmarkRunning && t.path !== '/benchmark'
}

function onTabClick(t) {
  if (isTabDisabled(t)) return
  if (t.isChat) {
    if (route.path.startsWith('/chat')) {
      // Already on chat — create a new conversation
      emit('new-chat')
    } else {
      // Coming back from another page — just navigate, preserve state
      // (streaming may still be in progress)
      router.push('/chat')
    }
  } else {
    router.push(t.path)
  }
}

function onNewChat() {
  emit('new-chat')
  if (!route.path.startsWith('/chat')) router.push('/chat')
}

function onSelectConv(convId) {
  emit('select-conv', convId)
  if (!route.path.startsWith('/chat')) router.push('/chat')
}

function isTabActive(t) {
  if (!route.path.startsWith(t.path)) return false
  // Chat tab only highlights when no conversation is selected
  if (t.isChat && props.currentConvId) return false
  return true
}
</script>

<template>
  <nav class="w-60 shrink-0 flex flex-col border-r border-line bg-bg2">
    <!-- Logo -->
    <div class="px-4 pt-4 pb-5">
      <button
        @click="emit('new-chat'); router.push('/chat')"
        class="wordmark text-[15px] hover:opacity-80 transition-opacity cursor-pointer"
      >ForgeRAG</button>
    </div>

    <!-- Tabs — Vercel-density: 13px label, 8px-12px inset, ~32px row height. -->
    <div class="px-3 flex flex-col gap-0.5">
      <button
        v-for="t in tabs" :key="t.path"
        @click="onTabClick(t)"
        :disabled="isTabDisabled(t)"
        class="px-3 py-2 rounded-md text-[13px] text-left transition-colors"
        :class="isTabDisabled(t)
          ? 'text-t3/80 cursor-not-allowed'
          : isTabActive(t)
            ? 'bg-bg3 text-t1 font-medium'
            : 'text-t2 hover:bg-bg3'"
      >{{ t.label }}<span v-if="t.dev" class="ml-1 text-[10px] text-t3/80">(In Dev)</span><span v-else-if="isTabDisabled(t) && benchmarkRunning" class="ml-1 text-[10px] text-t3/30">locked</span></button>
    </div>

    <!-- Conversations (always visible) -->
    <div class="px-3 pt-5 pb-1.5">
      <div class="text-[11px] text-t3 tracking-wider mb-2 px-1">Recents</div>
      <button
        @click="onNewChat"
        class="w-full text-[12px] text-left px-3 py-2 rounded-md border border-dashed border-line text-t3 hover:bg-bg3 transition-colors"
      >+ New chat</button>
    </div>
    <div class="flex-1 overflow-y-auto px-3 space-y-px">
      <div
        v-for="c in conversations" :key="c.conversation_id"
        class="group flex items-center px-3 py-2 rounded-md text-[12px] cursor-pointer transition-colors"
        :class="currentConvId === c.conversation_id && route.path.startsWith('/chat')
          ? 'bg-bg3 text-t1'
          : 'text-t2 hover:bg-bg3'"
        @click="onSelectConv(c.conversation_id)"
      >
        <span class="flex-1 truncate">{{ c.title || 'Untitled' }}</span>
        <button
          class="opacity-0 group-hover:opacity-40 hover:!opacity-100 text-[10px] ml-1"
          @click.stop="emit('delete-conv', c.conversation_id)"
        >✕</button>
      </div>
    </div>

    <!-- User row -->
    <div v-if="me" class="px-4 pt-3 pb-1 flex items-center justify-between text-[12px]">
      <span class="text-t2 truncate">{{ me.username }}<span v-if="me.role !== 'admin'" class="text-t3 ml-1">· {{ me.role }}</span></span>
      <button @click="onLogout" class="text-[11px] text-t3 hover:text-t1">Sign out</button>
    </div>

    <!-- Footer -->
    <div class="px-4 py-3 flex items-center justify-between gap-1">
      <div class="text-[11px] text-t3">v0.2.1</div>
      <div class="flex items-center gap-0.5">
        <button
          @click="toggleTheme"
          :title="isDark ? 'Switch to light' : 'Switch to dark'"
          class="p-1 rounded text-t3 hover:text-t1 hover:bg-bg3 transition-colors"
        >
          <!-- Sun (in dark mode → tap to go light) / Moon (in light → tap to go dark) -->
          <svg v-if="isDark" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="4"/>
            <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
          </svg>
          <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
          </svg>
        </button>
        <a href="https://github.com/deeplethe/ForgeRAG" target="_blank" rel="noopener"
          class="p-1 -mr-1 rounded text-t3 hover:text-t1 hover:bg-bg3 transition-colors">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 .3a12 12 0 00-3.8 23.38c.6.11.82-.26.82-.58v-2.02c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.08-.74.08-.73.08-.73 1.2.09 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49 1 .1-.78.42-1.3.76-1.6-2.67-.31-5.47-1.34-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.14-.3-.54-1.52.1-3.18 0 0 1-.32 3.3 1.23a11.5 11.5 0 016.02 0c2.28-1.55 3.29-1.23 3.29-1.23.64 1.66.24 2.88.12 3.18a4.65 4.65 0 011.23 3.22c0 4.61-2.8 5.62-5.48 5.92.42.36.81 1.1.81 2.22v3.29c0 .32.22.7.82.58A12 12 0 0012 .3"/>
          </svg>
        </a>
      </div>
    </div>
  </nav>
</template>
