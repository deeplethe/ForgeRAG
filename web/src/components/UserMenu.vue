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
    <!-- Trigger card: defined card-look at rest (border + bg), so it
         reads as a tappable surface even before hover. -->
    <button
      v-if="me"
      type="button"
      class="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg border border-line bg-bg hover:bg-bg3 transition-colors"
      :class="{ '!bg-bg3': open }"
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

    <!-- Popup panel: original divider style — sections separated by
         border-b lines, single flat bg. (GitHub + version live in
         the sidebar wordmark row now, so they're not in here.) -->
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
