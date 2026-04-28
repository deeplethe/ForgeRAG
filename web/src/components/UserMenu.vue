<!--
  UserMenu — sits at the bottom of the sidebar. Shows an avatar +
  username card; clicking pops a panel UPWARD with:
    • Language switcher (i18n locale)
    • Theme switcher (light / dark)
    • GitHub link
    • Version
    • Sign out

  Designed to grow: the `panel-section` slots are easy to add more
  items to (account settings, keyboard shortcuts, etc.) without
  re-arranging the rest.
-->
<template>
  <div ref="rootEl" class="relative">
    <!-- Trigger card (always visible at sidebar bottom) -->
    <button
      v-if="me"
      type="button"
      class="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-md transition-colors"
      :class="open ? 'bg-bg3' : 'hover:bg-bg3'"
      @click="toggle"
    >
      <span
        class="w-7 h-7 shrink-0 rounded-full flex items-center justify-center text-[11px] font-semibold uppercase select-none"
        :style="{ background: avatarBg, color: '#fff' }"
      >{{ initial }}</span>
      <span class="flex-1 min-w-0 text-left">
        <span class="block text-[12px] text-t1 truncate">{{ me.username }}</span>
        <span class="block text-[10px] text-t3 truncate">
          {{ me.role === 'admin' ? t('user_menu.role_admin') : me.role }}
        </span>
      </span>
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
        class="text-t3 shrink-0 transition-transform"
        :class="open ? 'rotate-180' : ''">
        <path d="M6 9l6 6 6-6"/>
      </svg>
    </button>

    <!-- Popup panel -->
    <Transition name="popup">
      <div
        v-if="open"
        class="absolute bottom-full left-0 right-0 mb-1.5 rounded-xl border border-line bg-bg shadow-lg overflow-hidden z-30"
      >
        <!-- ── Section: Language ───────────────────────────────────── -->
        <div class="px-3 pt-2.5 pb-2 border-b border-line">
          <div class="text-[10px] uppercase tracking-wider text-t3 mb-1.5">{{ t('user_menu.language') }}</div>
          <div class="flex gap-1">
            <button
              v-for="loc in locales"
              :key="loc.code"
              type="button"
              class="flex-1 px-2 py-1 rounded text-[12px] transition-colors"
              :class="currentLocale === loc.code
                ? 'bg-brand text-white'
                : 'text-t2 hover:bg-bg3 border border-line'"
              @click="onSetLocale(loc.code)"
            >{{ loc.label }}</button>
          </div>
        </div>

        <!-- ── Section: Theme ──────────────────────────────────────── -->
        <div class="px-3 py-2 border-b border-line">
          <div class="text-[10px] uppercase tracking-wider text-t3 mb-1.5">{{ t('user_menu.theme') }}</div>
          <div class="flex gap-1">
            <button
              type="button"
              class="flex-1 px-2 py-1 rounded text-[12px] flex items-center justify-center gap-1.5 transition-colors"
              :class="!isDark
                ? 'bg-brand text-white'
                : 'text-t2 hover:bg-bg3 border border-line'"
              @click="onSetTheme('light')"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="4"/>
                <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
              </svg>
              {{ t('user_menu.theme_light') }}
            </button>
            <button
              type="button"
              class="flex-1 px-2 py-1 rounded text-[12px] flex items-center justify-center gap-1.5 transition-colors"
              :class="isDark
                ? 'bg-brand text-white'
                : 'text-t2 hover:bg-bg3 border border-line'"
              @click="onSetTheme('dark')"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
              </svg>
              {{ t('user_menu.theme_dark') }}
            </button>
          </div>
        </div>

        <!-- ── Section: Links + version ────────────────────────────── -->
        <div class="px-3 py-2 border-b border-line flex items-center justify-between text-[11px]">
          <a
            href="https://github.com/deeplethe/ForgeRAG"
            target="_blank"
            rel="noopener"
            class="flex items-center gap-1.5 text-t2 hover:text-t1 transition-colors"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 .3a12 12 0 00-3.8 23.38c.6.11.82-.26.82-.58v-2.02c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.08-.74.08-.73.08-.73 1.2.09 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49 1 .1-.78.42-1.3.76-1.6-2.67-.31-5.47-1.34-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.14-.3-.54-1.52.1-3.18 0 0 1-.32 3.3 1.23a11.5 11.5 0 016.02 0c2.28-1.55 3.29-1.23 3.29-1.23.64 1.66.24 2.88.12 3.18a4.65 4.65 0 011.23 3.22c0 4.61-2.8 5.62-5.48 5.92.42.36.81 1.1.81 2.22v3.29c0 .32.22.7.82.58A12 12 0 0012 .3"/>
            </svg>
            {{ t('common.github') }}
          </a>
          <span class="text-t3">v{{ version }}</span>
        </div>

        <!-- ── Section: Sign out ───────────────────────────────────── -->
        <button
          type="button"
          class="w-full flex items-center gap-2 px-3 py-2 text-[12px] text-t2 hover:bg-bg3 transition-colors"
          @click="onLogout"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/>
          </svg>
          {{ t('user_menu.sign_out') }}
        </button>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { setLocale, SUPPORTED_LOCALES } from '@/i18n'
import { useTheme } from '@/composables/useTheme'
import { useDialog } from '@/composables/useDialog'
import { logout } from '@/api/auth'

const props = defineProps({
  me: { type: Object, default: null },
})

const { t, locale } = useI18n()
const { isDark, setTheme } = useTheme()
const { confirm } = useDialog()

const open = ref(false)
const rootEl = ref(null)
const locales = SUPPORTED_LOCALES
const currentLocale = computed(() => locale.value)

const version = import.meta.env.VITE_APP_VERSION || '0.2.1'

const initial = computed(() => {
  const u = (props.me?.username || '').trim()
  return u ? u.charAt(0).toUpperCase() : '?'
})
// Deterministic per-username hue — keeps the avatar visually stable
// across locale / theme toggles, and visually distinct between users.
const avatarBg = computed(() => {
  const u = props.me?.username || ''
  let h = 0
  for (let i = 0; i < u.length; i++) h = (h * 31 + u.charCodeAt(i)) >>> 0
  return `hsl(${h % 360}, 55%, 50%)`
})

function toggle() { open.value = !open.value }
function close() { open.value = false }

function onSetLocale(code) { setLocale(code) }
function onSetTheme(mode) { setTheme(mode) }

async function onLogout() {
  close()
  const ok = await confirm({
    title: t('user_menu.sign_out_confirm_title'),
    description: t('user_menu.sign_out_confirm_desc'),
    confirmText: t('user_menu.sign_out_button'),
  })
  if (!ok) return
  try { await logout() } catch {}
  window.location.href = '/login'
}

// Click-outside
function onDocClick(e) {
  if (!open.value || !rootEl.value) return
  if (!rootEl.value.contains(e.target)) close()
}
onMounted(() => document.addEventListener('mousedown', onDocClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))
</script>

<style scoped>
.popup-enter-active, .popup-leave-active { transition: opacity .15s ease, transform .15s ease; }
.popup-enter-from, .popup-leave-to { opacity: 0; transform: translateY(4px); }
</style>
