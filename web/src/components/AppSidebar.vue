<script setup>
import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import UserMenu from './UserMenu.vue'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()

// Pinned next to the wordmark so version + repo link are discoverable
// without cluttering the user-settings menu.
const version = import.meta.env.VITE_APP_VERSION || '0.2.1'

const props = defineProps({
  conversations: Array,
  currentConvId: String,
  benchmarkRunning: { type: Boolean, default: false },
  me: { type: Object, default: null },
})
const emit = defineEmits(['select-conv', 'new-chat', 'delete-conv'])

// Tabs are i18n-driven; ``label_key`` resolves at render time so a
// language toggle re-labels them live without re-rendering the array.
const tabs = computed(() => [
  { path: '/chat', label_key: 'sidebar.tabs.chat', isChat: true },
  { path: '/workspace', label_key: 'sidebar.tabs.workspace' },
  { path: '/knowledge-graph', label_key: 'sidebar.tabs.knowledge_graph' },
  { path: '/simulation', label_key: 'sidebar.tabs.simulation' },
  { path: '/metrics', label_key: 'sidebar.tabs.metrics' },
  { path: '/benchmark', label_key: 'sidebar.tabs.benchmark', dev: true },
  { path: '/tokens', label_key: 'sidebar.tabs.tokens' },
])

function isTabDisabled(tab) {
  if (tab.dev) return true
  // When benchmark is running, only the Benchmark tab is clickable
  return props.benchmarkRunning && tab.path !== '/benchmark'
}

function onTabClick(tab) {
  if (isTabDisabled(tab)) return
  if (tab.isChat) {
    if (route.path.startsWith('/chat')) {
      // Already on chat — create a new conversation
      emit('new-chat')
    } else {
      // Coming back from another page — just navigate, preserve state
      // (streaming may still be in progress)
      router.push('/chat')
    }
  } else {
    router.push(tab.path)
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

function isTabActive(tab) {
  if (!route.path.startsWith(tab.path)) return false
  // Chat tab only highlights when no conversation is selected
  if (tab.isChat && props.currentConvId) return false
  return true
}
</script>

<template>
  <nav class="w-60 shrink-0 flex flex-col border-r border-line bg-bg2">
    <!-- Logo + product-identity row.
         Version + GitHub link live HERE (next to the wordmark) so the
         UserMenu can stay focused on user-controlled settings. The
         version is intentionally low-emphasis (10px / t3); the GitHub
         icon is a small affordance pinned right. -->
    <div class="px-4 pt-4 pb-5 flex items-center gap-2">
      <button
        @click="emit('new-chat'); router.push('/chat')"
        class="wordmark text-[15px] hover:opacity-80 transition-opacity cursor-pointer"
      >ForgeRAG</button>
      <span class="text-[10px] text-t3 select-none">v{{ version }}</span>
      <a
        href="https://github.com/deeplethe/ForgeRAG"
        target="_blank"
        rel="noopener"
        class="ml-auto p-1 -mr-1 rounded text-t3 hover:text-t1 hover:bg-bg3 transition-colors"
        :title="t('common.github')"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
          <path d="M12 .3a12 12 0 00-3.8 23.38c.6.11.82-.26.82-.58v-2.02c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.08-.74.08-.73.08-.73 1.2.09 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49 1 .1-.78.42-1.3.76-1.6-2.67-.31-5.47-1.34-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.14-.3-.54-1.52.1-3.18 0 0 1-.32 3.3 1.23a11.5 11.5 0 016.02 0c2.28-1.55 3.29-1.23 3.29-1.23.64 1.66.24 2.88.12 3.18a4.65 4.65 0 011.23 3.22c0 4.61-2.8 5.62-5.48 5.92.42.36.81 1.1.81 2.22v3.29c0 .32.22.7.82.58A12 12 0 0012 .3"/>
        </svg>
      </a>
    </div>

    <!-- Tabs — Vercel-density: 13px label, 8px-12px inset, ~32px row height. -->
    <div class="px-3 flex flex-col gap-0.5">
      <button
        v-for="tab in tabs" :key="tab.path"
        @click="onTabClick(tab)"
        :disabled="isTabDisabled(tab)"
        class="px-3 py-2 rounded-md text-[13px] text-left transition-colors"
        :class="isTabDisabled(tab)
          ? 'text-t3/80 cursor-not-allowed'
          : isTabActive(tab)
            ? 'bg-bg3 text-t1 font-medium'
            : 'text-t2 hover:bg-bg3'"
      >{{ t(tab.label_key) }}<span v-if="tab.dev" class="ml-1 text-[10px] text-t3/80">{{ t('sidebar.in_dev') }}</span><span v-else-if="isTabDisabled(tab) && benchmarkRunning" class="ml-1 text-[10px] text-t3/30">{{ t('sidebar.locked') }}</span></button>
    </div>

    <!-- Conversations (always visible) -->
    <div class="px-3 pt-5 pb-1.5">
      <div class="text-[11px] text-t3 tracking-wider mb-2 px-1">{{ t('sidebar.recents') }}</div>
      <button
        @click="onNewChat"
        class="w-full text-[12px] text-left px-3 py-2 rounded-md border border-dashed border-line text-t3 hover:bg-bg3 transition-colors"
      >{{ t('sidebar.new_chat') }}</button>
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
        <span class="flex-1 truncate">{{ c.title || t('sidebar.untitled') }}</span>
        <button
          class="opacity-0 group-hover:opacity-40 hover:!opacity-100 text-[10px] ml-1"
          @click.stop="emit('delete-conv', c.conversation_id)"
        >✕</button>
      </div>
    </div>

    <!-- User card + popup menu. No top divider: padding + the card's
         own ``bg-bg`` (vs sidebar's ``bg-bg2``) is enough visual
         separation from the conversation list above. -->
    <div class="px-2 pt-2 pb-2.5">
      <UserMenu :me="me" />
    </div>
  </nav>
</template>
