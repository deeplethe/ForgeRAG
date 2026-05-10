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
      <UserAvatar :name="identityKey" :img-url="avatarUrl" :size="28" />
      <span class="flex-1 min-w-0 text-left">
        <span class="block text-xs text-t1 truncate">{{ nicknameLabel }}</span>
        <span v-if="subLabel" class="block text-3xs text-t3 truncate">{{ subLabel }}</span>
      </span>
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
        class="text-t3 shrink-0 transition-transform"
        :class="open ? 'rotate-180' : ''">
        <path d="M6 9l6 6 6-6"/>
      </svg>
    </button>

    <!-- Popup panel — Geist-style row menu.
         Visual alignment: ``rounded-lg`` matches the trigger card
         below so they read as one unit when the popup is open
         (the previous ``rounded-xl`` made the menu look bulkier
         than the card it sits on). Custom shadow matches the
         design-system popover used in Settings sub-nav popovers
         (filter pills, etc.) so this menu doesn't stand out as
         the only "shadow-lg" elevated thing in the sidebar. -->
    <Transition name="popup">
      <div v-if="open" class="user-menu-popover">
        <!-- ── Language: label + segmented control ─────────── -->
        <div class="row row-control">
          <span class="row-label">{{ t('user_menu.language') }}</span>
          <div class="seg">
            <button
              v-for="loc in locales"
              :key="loc.code"
              type="button"
              class="seg-btn"
              :class="{ 'is-active': currentLocale === loc.code }"
              @click="onSetLocale(loc.code)"
            >{{ loc.label }}</button>
          </div>
        </div>

        <!-- ── Theme: label + segmented control (icons) ────── -->
        <div class="row row-control">
          <span class="row-label">{{ t('user_menu.theme') }}</span>
          <div class="seg">
            <button
              type="button"
              class="seg-btn seg-icon"
              :class="{ 'is-active': !isDark }"
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
              class="seg-btn seg-icon"
              :class="{ 'is-active': isDark }"
              :title="t('user_menu.theme_dark')"
              @click="onSetTheme('dark')"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
              </svg>
            </button>
          </div>
        </div>

        <div class="divider"></div>

        <!-- ── Settings ── -->
        <button type="button" class="row row-action" @click="onOpenSettings">
          <span class="row-label">{{ t('user_menu.settings') }}</span>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="row-icon">
            <path d="M9 18l6-6-6-6"/>
          </svg>
        </button>

        <!-- ── Sign out ── -->
        <button type="button" class="row row-action" @click="onLogout">
          <span class="row-label">{{ t('user_menu.sign_out') }}</span>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="row-icon">
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
import { avatarUrlFor } from '@/api/admin'
import UserAvatar from './UserAvatar.vue'

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

// Identity primary key for avatar derivation (initials colour
// hash etc.). Prefers the user-set display_name, falls back to
// email local-part, then legacy username.
const identityKey = computed(() => {
  const m = props.me || {}
  return (m.display_name
    || (m.email ? m.email.split('@')[0] : '')
    || m.username
    || '').trim()
})

// Card line 1: the nickname the user picked at registration. We
// fall through to ``username`` when ``display_name`` is empty
// (it's an optional field) so the row never reads "?".
const nicknameLabel = computed(() => {
  const m = props.me || {}
  return (m.display_name || m.username || '?').trim()
})

// Card line 2: the email — that's the canonical login identifier
// and what users will type on the login form. Hidden entirely
// when the account has no email (legacy bootstrap-admin rows
// only — those don't exist with the no-default-admin flow but
// keeping the guard means the card stays clean if one ever
// shows up).
const subLabel = computed(() => (props.me?.email || '').trim())

// Avatar image URL — null when has_avatar=false so the
// component falls straight back to initials without a 404.
const avatarUrl = computed(() =>
  avatarUrlFor(props.me?.user_id, props.me?.has_avatar),
)

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
/* ── Popover container ─────────────────────────────────────────
   Aligned to the trigger card below: same outer width (anchored
   to the parent's flex-row, which the user-card also fills),
   ``rounded-lg`` to mirror the card's corner radius. Shadow
   matches Settings filter popovers so this menu doesn't read
   as the only "elevated" thing in the sidebar. */
.user-menu-popover {
  position: absolute;
  bottom: 100%;
  left: 0;
  right: 0;
  margin-bottom: 6px;
  padding: 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.14);
  z-index: 30;
}

/* Two row shapes share the same height + padding so the menu
   stays a clean stack regardless of whether a row is a control
   strip (label + segmented control) or an action button (label
   + icon). */
.row {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 8px;
  font-size: 0.75rem;
  color: var(--color-t1);
  background: transparent;
  border: none;
  border-radius: var(--r-sm);
  text-align: left;
  cursor: default;
  transition: background-color 0.1s;
}
.row-action { cursor: pointer; }
.row-action:hover { background: var(--color-bg2); }
.row-label { color: var(--color-t1); }
.row-icon { color: var(--color-t3); flex-shrink: 0; }

.divider {
  height: 1px;
  margin: 4px 2px;
  background: var(--color-line);
}

/* Segmented control: thin border, no inner padding (the buttons
   own their own padding). Shrinks compared to the prior ``p-0.5
   border`` block which felt over-engineered for a 2-segment
   toggle. */
.seg {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
}
.seg-btn {
  height: 20px;
  padding: 0 8px;
  font-size: 0.6875rem;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: color 0.1s, background-color 0.1s;
}
.seg-btn:hover { color: var(--color-t2); }
.seg-btn.is-active {
  background: var(--color-bg-selected);
  color: var(--color-t1);
}
.seg-icon {
  width: 26px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.popup-enter-active, .popup-leave-active { transition: opacity .15s ease, transform .15s ease; }
.popup-enter-from, .popup-leave-to { opacity: 0; transform: translateY(4px); }
</style>
