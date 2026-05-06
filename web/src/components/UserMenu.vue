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
    <!-- Trigger card: rests on the sidebar's bg-bg2 with its own
         bg-bg, so the lightness step makes it read as a card without
         needing a border to spell that out. -->
    <button
      v-if="me"
      type="button"
      class="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg bg-bg hover:bg-bg3 transition-colors"
      :class="{ '!bg-bg3': open }"
      @click="toggle"
    >
      <span
        class="w-7 h-7 shrink-0 rounded-full flex items-center justify-center text-[11px] font-semibold uppercase select-none"
        :style="{ background: avatarBg, color: '#fff' }"
      >{{ initial }}</span>
      <span class="flex-1 min-w-0 text-left">
        <span class="block text-[12px] text-t1 truncate">{{ displayLabel }}</span>
        <span class="block text-[10px] text-t3 truncate">
          {{ me.role === 'admin' ? t('user_menu.role_admin') : t('user_menu.role_user') }}
        </span>
      </span>
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
        class="text-t3 shrink-0 transition-transform"
        :class="open ? 'rotate-180' : ''">
        <path d="M6 9l6 6 6-6"/>
      </svg>
    </button>

    <!-- Popup panel: Geist-style row menu. Each row is `label LEFT,
         control RIGHT`, no section dividers — vertical padding does
         the grouping. Selected state on segmented controls uses
         neutral bg-bg3 elevation, not the brand blue (Vercel blue is
         reserved for actual CTAs like Send / cite-active). -->
    <Transition name="popup">
      <div
        v-if="open"
        class="absolute bottom-full left-0 right-0 mb-1.5 rounded-xl border border-line bg-bg shadow-lg py-1.5 z-30"
      >
        <!-- ── Language: label + segmented control ─────────────────── -->
        <div class="flex items-center justify-between gap-3 px-3 py-1.5">
          <span class="text-[12px] text-t1">{{ t('user_menu.language') }}</span>
          <div class="flex items-center gap-0.5 p-0.5 rounded-md border border-line">
            <button
              v-for="loc in locales"
              :key="loc.code"
              type="button"
              class="px-2 py-0.5 rounded text-[11px] transition-colors"
              :class="currentLocale === loc.code
                ? 'bg-bg3 text-t1'
                : 'text-t3 hover:text-t2'"
              @click="onSetLocale(loc.code)"
            >{{ loc.label }}</button>
          </div>
        </div>

        <!-- ── Theme: label + segmented control (icons) ────────────── -->
        <div class="flex items-center justify-between gap-3 px-3 py-1.5">
          <span class="text-[12px] text-t1">{{ t('user_menu.theme') }}</span>
          <div class="flex items-center gap-0.5 p-0.5 rounded-md border border-line">
            <button
              type="button"
              class="w-7 h-6 rounded flex items-center justify-center transition-colors"
              :class="!isDark ? 'bg-bg3 text-t1' : 'text-t3 hover:text-t2'"
              :title="t('user_menu.theme_light')"
              @click="onSetTheme('light')"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="4"/>
                <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
              </svg>
            </button>
            <button
              type="button"
              class="w-7 h-6 rounded flex items-center justify-center transition-colors"
              :class="isDark ? 'bg-bg3 text-t1' : 'text-t3 hover:text-t2'"
              :title="t('user_menu.theme_dark')"
              @click="onSetTheme('dark')"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
              </svg>
            </button>
          </div>
        </div>

        <!-- Subtle separator before action rows. -->
        <div class="my-1 border-t border-line"></div>

        <!-- ── Settings: label LEFT, chevron RIGHT.
             Visible to every user (their own profile, password,
             prefs); admin-only sub-tabs are gated inside the
             /settings page itself, not at this entry. -->
        <button
          type="button"
          class="w-full flex items-center justify-between gap-3 px-3 py-2 text-[12px] text-t1 hover:bg-bg3 transition-colors"
          @click="onOpenSettings"
        >
          <span>{{ t('user_menu.settings') }}</span>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-t3">
            <path d="M9 18l6-6-6-6"/>
          </svg>
        </button>

        <!-- ── Sign out: label LEFT, icon RIGHT (Geist row pattern) ── -->
        <button
          type="button"
          class="w-full flex items-center justify-between gap-3 px-3 py-2 text-[12px] text-t1 hover:bg-bg3 transition-colors"
          @click="onLogout"
        >
          <span>{{ t('user_menu.sign_out') }}</span>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-t3">
            <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/>
          </svg>
        </button>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
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
const router = useRouter()

const open = ref(false)
const rootEl = ref(null)
const locales = SUPPORTED_LOCALES
const currentLocale = computed(() => locale.value)

// Identity primary key for label / avatar derivation. Prefer
// display_name (user-set, friendly), fall back to email
// local-part, then to legacy username, then "?".
const identityKey = computed(() => {
  const m = props.me || {}
  return (m.display_name
    || (m.email ? m.email.split('@')[0] : '')
    || m.username
    || '').trim()
})
const displayLabel = computed(() => identityKey.value || '?')
const initial = computed(() => {
  const k = identityKey.value
  return k ? k.charAt(0).toUpperCase() : '?'
})
// Deterministic per-identity hue — stable across locale / theme
// toggles, visually distinct between users. Keyed off the same
// fallback chain so the avatar colour matches the visible label.
const avatarBg = computed(() => {
  const k = identityKey.value
  let h = 0
  for (let i = 0; i < k.length; i++) h = (h * 31 + k.charCodeAt(i)) >>> 0
  return `hsl(${h % 360}, 55%, 50%)`
})

function toggle() { open.value = !open.value }
function close() { open.value = false }

function onSetLocale(code) { setLocale(code) }
function onSetTheme(mode) { setTheme(mode) }
function onOpenSettings() {
  close()
  router.push('/settings')
}

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
