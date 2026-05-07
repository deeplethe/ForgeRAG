<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  MessageSquare,
  Search,
  FolderOpen,
  Network,
  BarChart3,
  Loader2,
  MoreHorizontal,
  Star,
  Pencil,
  Trash2,
} from 'lucide-vue-next'
import UserMenu from './UserMenu.vue'
import Skeleton from './Skeleton.vue'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()

// Pinned next to the wordmark so version + repo link are discoverable
// without cluttering the user-settings menu.
const version = import.meta.env.VITE_APP_VERSION || '0.2.3'

const props = defineProps({
  conversations: Array,
  conversationsLoading: { type: Boolean, default: false },
  // Append-page in flight — drives the tail spinner under the list.
  conversationsLoadingMore: { type: Boolean, default: false },
  // False once we've fetched a partial / empty page. The sentinel
  // stops triggering ``load-more-conversations`` once this flips,
  // and the IntersectionObserver disconnects so it doesn't keep
  // observing forever.
  conversationsHasMore: { type: Boolean, default: true },
  // Set<string> of conversation_ids currently being deleted — those
  // rows hide their trash button to prevent a double-fire while the
  // optimistic removal is in flight (the row itself disappears from
  // ``conversations`` immediately, but Set membership outlives any
  // race where the row is briefly re-added on rollback).
  deletingConvs: { type: Object, default: () => new Set() },
  currentConvId: String,
  me: { type: Object, default: null },
})
const emit = defineEmits([
  'select-conv',
  'new-chat',
  'delete-conv',
  'rename-conv',
  'toggle-favorite-conv',
  'load-more-conversations',
])

// Per-row context menu state. ``openMenuId`` holds the
// conversation_id whose dot-menu is currently expanded; null
// means no menu open. We position the menu absolutely under
// the trigger button — a single global element is simpler than
// portaling, and we never have more than one open at a time
// because the click-outside handler closes the previous one.
const openMenuId = ref(null)
function toggleMenu(id, event) {
  // Stop the row's @click from also firing — we don't want
  // opening the menu to navigate into the conversation.
  event?.stopPropagation()
  openMenuId.value = openMenuId.value === id ? null : id
}
// Document-level click-outside. Adds the listener only while
// a menu is open so we don't pay the global-listener tax in
// the common case.
function _onDocClick(e) {
  // Anchor on the .conv-menu container so clicking inside the
  // popover (e.g. on a menu item) doesn't immediately close it
  // before its handler runs.
  if (!e.target.closest('.conv-menu')) {
    openMenuId.value = null
  }
}
watch(openMenuId, (v) => {
  if (v) document.addEventListener('click', _onDocClick)
  else document.removeEventListener('click', _onDocClick)
})

// Tabs are i18n-driven; ``label_key`` resolves at render time so a
// language toggle re-labels them live without re-rendering the array.
// Lucide icon per tab — 14px, stroke 1.75 to match the app's
// other icon usage (UserMenu, Settings sub-nav back button).
// Network is the closest "graph" visual in lucide; BarChart3
// reads as "metrics" without being too dashboard-y.
const tabs = computed(() => [
  { path: '/chat', label_key: 'sidebar.tabs.chat', isChat: true, icon: MessageSquare },
  { path: '/search', label_key: 'sidebar.tabs.search', icon: Search },
  { path: '/workspace', label_key: 'sidebar.tabs.workspace', icon: FolderOpen },
  { path: '/knowledge-graph', label_key: 'sidebar.tabs.knowledge_graph', icon: Network },
  { path: '/metrics', label_key: 'sidebar.tabs.metrics', icon: BarChart3 },
  // /simulation + /benchmark + /tokens used to live here. /tokens
  // moved into /settings/{sessions,tokens}; simulation + benchmark
  // were removed (simulation hit the deleted /api/v1/query;
  // benchmark will be rebuilt). The avatar menu's Settings link
  // is the entry point for account / token / user management.
])

function isTabDisabled(tab) {
  return !!tab.dev
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

function onMenuFavorite(c) {
  openMenuId.value = null
  emit('toggle-favorite-conv', c)
}
function onMenuRename(c) {
  openMenuId.value = null
  emit('rename-conv', c)
}
function onMenuDelete(c) {
  openMenuId.value = null
  emit('delete-conv', c.conversation_id)
}

function isTabActive(tab) {
  if (!route.path.startsWith(tab.path)) return false
  // Chat tab only highlights when no conversation is selected
  if (tab.isChat && props.currentConvId) return false
  return true
}

/* ── Scroll-loading the conversation list ─────────────────────────
 *
 * We use IntersectionObserver against a 1px sentinel <div> placed
 * AFTER the last row but inside the same scrolling container. As
 * the user scrolls the sentinel into view (or near it via the 80px
 * rootMargin pre-fetch buffer), we fire ``load-more-conversations``
 * and the parent appends the next page.
 *
 * Edge cases handled:
 *   - The first page may not fill the viewport (low conv count).
 *     IntersectionObserver fires immediately on observe(), which
 *     correctly triggers the next page fetch. The parent's
 *     hasMore=false bail-out stops the loop.
 *   - The observer is rebuilt whenever the sentinel ref changes
 *     (template v-if false→true on first load) so we don't observe
 *     a stale node.
 */
const sentinelEl = ref(null)
let _io = null

function _ensureObserver() {
  if (_io) return
  _io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting && props.conversationsHasMore && !props.conversationsLoadingMore) {
        emit('load-more-conversations')
      }
    }
  }, {
    // Pre-fetch when the sentinel is within 80px of the viewport
    // — feels like infinite scroll rather than "stutter then load".
    rootMargin: '80px 0px',
  })
}

watch(sentinelEl, (el, prev) => {
  _ensureObserver()
  if (prev) _io.unobserve(prev)
  if (el) _io.observe(el)
})

watch(
  () => props.conversationsHasMore,
  (more) => {
    // Once the parent says "no more pages", stop observing — the
    // IO callback would no-op anyway (hasMore guard) but
    // disconnecting is cheaper than a recurring no-op.
    if (!more && _io && sentinelEl.value) _io.unobserve(sentinelEl.value)
  },
)

onBeforeUnmount(() => {
  if (_io) {
    _io.disconnect()
    _io = null
  }
})
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
        class="flex items-center gap-2 hover:opacity-80 transition-opacity cursor-pointer"
      >
        <img src="/craig.png" alt="" class="w-6 h-6 rounded-full shrink-0" />
        <span class="wordmark text-[15px]">OpenCraig</span>
      </button>
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
        class="px-3 py-2 rounded-md text-[13px] text-left transition-colors flex items-center gap-2.5"
        :class="isTabDisabled(tab)
          ? 'text-t3/80 cursor-not-allowed'
          : isTabActive(tab)
            ? 'bg-bg-selected text-t1 font-medium'
            : 'text-t2 hover:bg-bg3'"
      >
        <component :is="tab.icon" :size="14" :stroke-width="1.75" class="shrink-0" />
        <span class="flex-1">{{ t(tab.label_key) }}</span>
        <span v-if="tab.dev" class="text-[10px] text-t3/80">{{ t('sidebar.in_dev') }}</span>
      </button>
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
      <!-- Skeleton on first load — same Skeleton primitive + shimmer
           pattern as the workspace folder tree, so visual language is
           consistent across the app. Hidden once we have data;
           refreshes after a user action don't replay it (parent only
           flips ``conversationsLoading`` on the very first fetch). -->
      <div v-if="conversationsLoading && !(conversations && conversations.length)" class="conv-skel-list">
        <Skeleton
          v-for="(w, i) in [80, 60, 90, 50, 75, 55]" :key="i"
          block :w="w + '%'" :h="14" class="conv-skel-row"
        />
      </div>
      <div
        v-for="c in conversations" :key="c.conversation_id"
        class="group relative flex items-center px-3 py-2 rounded-md text-[12px] cursor-pointer transition-colors"
        :class="[
          currentConvId === c.conversation_id && route.path.startsWith('/chat')
            ? 'bg-bg-selected text-t1 is-active'
            : 'text-t2 hover:bg-bg3',
        ]"
        @click="onSelectConv(c.conversation_id)"
      >
        <!-- Star marker for favorites (shown even when not hovered).
             Tiny + amber so the eye picks it up without it
             dominating the title. -->
        <Star
          v-if="c.is_favorite"
          :size="10"
          :stroke-width="0"
          class="conv-star-marker shrink-0 mr-1.5"
          fill="currentColor"
        />
        <span class="flex-1 truncate">{{ c.title || t('sidebar.untitled') }}</span>

        <!-- Three-dot menu trigger. Hidden until row hover (or
             when its own menu is open) — same pattern as the
             previous ✕ button, just a more conventional
             affordance. The wrapping ``conv-menu`` div is the
             click-outside anchor. -->
        <div
          v-if="!deletingConvs.has(c.conversation_id)"
          class="conv-menu relative shrink-0"
        >
          <button
            type="button"
            class="conv-menu-trigger"
            :class="{ 'is-open': openMenuId === c.conversation_id }"
            :aria-label="t('sidebar.conv_menu')"
            @click.stop="toggleMenu(c.conversation_id, $event)"
          >
            <MoreHorizontal :size="13" :stroke-width="1.75" />
          </button>

          <div
            v-if="openMenuId === c.conversation_id"
            class="conv-menu-popover"
            @click.stop
          >
            <button class="conv-menu-row" @click="onMenuFavorite(c)">
              <Star :size="13" :stroke-width="1.75" :fill="c.is_favorite ? 'currentColor' : 'none'" />
              <span>{{ c.is_favorite ? t('sidebar.conv_unfavorite') : t('sidebar.conv_favorite') }}</span>
            </button>
            <button class="conv-menu-row" @click="onMenuRename(c)">
              <Pencil :size="13" :stroke-width="1.75" />
              <span>{{ t('sidebar.conv_rename') }}</span>
            </button>
            <div class="conv-menu-divider"></div>
            <button class="conv-menu-row is-destructive" @click="onMenuDelete(c)">
              <Trash2 :size="13" :stroke-width="1.75" />
              <span>{{ t('sidebar.conv_delete') }}</span>
            </button>
          </div>
        </div>
      </div>

      <!-- Tail spinner: visible while an append-page is in flight. -->
      <div
        v-if="conversationsLoadingMore"
        class="flex items-center justify-center py-2 text-t3"
      >
        <Loader2 :size="12" :stroke-width="1.75" class="animate-spin" />
      </div>

      <!-- Sentinel: 1px element after the last row. The
           IntersectionObserver fires ``load-more-conversations``
           when it scrolls into view (or comes within 80px). Only
           rendered while there's more to load — once hasMore flips
           false the watcher disconnects the observer too. -->
      <div
        v-if="conversationsHasMore && conversations && conversations.length"
        ref="sentinelEl"
        class="h-px"
        aria-hidden="true"
      />
    </div>

    <!-- User card + popup menu. No top divider: padding + the card's
         own ``bg-bg`` (vs sidebar's ``bg-bg2``) is enough visual
         separation from the conversation list above. -->
    <div class="px-2 pt-2 pb-2.5">
      <UserMenu :me="me" />
    </div>
  </nav>
</template>

<style scoped>
/* Conversation-list skeleton — uses the shared <Skeleton> primitive
   (same shimmer animation as the workspace FolderTree). Vertical rhythm
   mirrors a real conversation row's px-3 py-2 padding so the layout
   doesn't reflow when the data lands. */
.conv-skel-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 10px 12px 4px;
}
.conv-skel-row {
  border-radius: 4px;
}

/* ── Per-row context menu ─────────────────────────────────────
   Trigger: hidden by default, fades in on row hover. Stays
   visible when its own menu is open (otherwise the popover
   would close the moment the row's hover state ends).
   Popover: aligned right of the trigger, same shape /
   shadow / token usage as the UserMenu popover so all
   sidebar pop-ups read as one family. */
.conv-star-marker {
  color: #f59e0b;
}
.conv-menu-trigger {
  width: 22px;
  height: 22px;
  margin-left: 4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: 4px;
  color: var(--color-t3);
  opacity: 0;
  cursor: pointer;
  transition: opacity 0.12s, background-color 0.12s, color 0.12s;
}
.group:hover .conv-menu-trigger,
.group.is-active .conv-menu-trigger,
.conv-menu-trigger.is-open {
  opacity: 1;
}
.conv-menu-trigger:hover {
  background: var(--color-bg3);
  color: var(--color-t1);
}

.conv-menu-popover {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  min-width: 168px;
  padding: 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.14);
  z-index: 30;
}
.conv-menu-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 8px;
  font-size: 12px;
  color: var(--color-t1);
  background: transparent;
  border: none;
  border-radius: var(--r-sm);
  text-align: left;
  cursor: pointer;
  transition: background-color 0.1s, color 0.1s;
}
.conv-menu-row:hover { background: var(--color-bg2); }
.conv-menu-row.is-destructive { color: var(--color-err-fg, #b91c1c); }
.conv-menu-row.is-destructive:hover {
  background: color-mix(in srgb, #ef4444 8%, transparent);
}
.conv-menu-divider {
  height: 1px;
  margin: 4px 2px;
  background: var(--color-line);
}
</style>
