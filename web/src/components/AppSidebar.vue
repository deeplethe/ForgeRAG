<script setup>
import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import UserMenu from './UserMenu.vue'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()

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

    <!-- User card + popup menu (replaces previous user row + footer) -->
    <div class="px-2 pt-2 pb-2.5 border-t border-line">
      <UserMenu :me="me" />
    </div>
  </nav>
</template>
