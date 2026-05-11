<script setup>
/**
 * Settings shell — left sub-nav + right ``<router-view>``.
 *
 * Same chrome for everyone; the difference between admin and a
 * regular user is the number of items in the sub-nav. Admin-only
 * sub-tabs carry ``meta.requiresAdmin`` on their route definitions
 * (see ``router.js``); we gate them at TWO layers:
 *   1. ``v-if="isAdmin"`` on the nav entry so non-admins don't
 *      see the link.
 *   2. ``router.beforeEach`` redirects unauthorized URL access
 *      back to /settings/profile (defence in depth — typing the
 *      URL by hand still bounces).
 *
 * Layout: full-screen page with main app sidebar visible to the
 * left (the user is still inside the app shell), this component
 * adds a SECOND narrow nav for the settings sections, then the
 * content panel. Mirrors Vercel / Linear / GitHub settings IA.
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { getMe } from '@/api/auth'
import { ChevronLeft } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const me = ref(null)
const isAdmin = computed(() => me.value?.role === 'admin')

onMounted(async () => {
  try { me.value = await getMe() } catch {}
})

// Flat link list. We deliberately don't group into sections —
// with only 2-3 entries the ACCOUNT / WORKSPACE labels were
// noisier than the links themselves. If this ever grows past
// ~6 items, reintroduce sections then.
const links = computed(() => {
  const all = [
    { path: '/settings/profile', label: t('settings.nav.profile') },
    { path: '/settings/sessions', label: t('settings.nav.sessions') },
    { path: '/settings/metrics', label: t('settings.nav.metrics') },
    { path: '/settings/scheduled-tasks', label: t('settings.nav.scheduled_tasks') },
    { path: '/settings/plugins', label: t('settings.nav.plugins') },
    { path: '/settings/team-tools', label: t('settings.nav.team_tools') },
    { path: '/settings/tokens', label: t('settings.nav.tokens'), adminOnly: true },
    { path: '/settings/users', label: t('settings.nav.users'), adminOnly: true },
    { path: '/settings/audit', label: t('settings.nav.audit'), adminOnly: true },
  ]
  return all.filter((l) => !l.adminOnly || isAdmin.value)
})

function isActive(path) {
  return route.path === path
}

function goBack() {
  // Best-effort: send the user back to wherever they came from.
  // History fallback to /chat if this is a fresh tab.
  if (window.history.length > 1) router.back()
  else router.push('/chat')
}
</script>

<template>
  <div class="settings-shell">
    <!-- Left sub-nav (settings-specific). The main app sidebar
         from AppSidebar.vue is rendered by the layout above this
         component; this is a SECOND, scoped nav. -->
    <aside class="sub-nav">
      <button class="back-btn" @click="goBack">
        <ChevronLeft :size="14" :stroke-width="1.75" />
        <span>{{ t('settings.back') }}</span>
      </button>
      <h1 class="sub-nav-title">{{ t('settings.title') }}</h1>
      <nav class="link-list">
        <router-link
          v-for="link in links"
          :key="link.path"
          :to="link.path"
          class="nav-link"
          :class="{ 'is-active': isActive(link.path) }"
        >{{ link.label }}</router-link>
      </nav>
    </aside>

    <!-- Right content panel — sub-routes render here. -->
    <main class="settings-content">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.settings-shell {
  display: flex;
  height: 100%;
  background: var(--color-bg2);
}
.sub-nav {
  width: 220px;
  flex-shrink: 0;
  padding: 20px 12px;
  border-right: 1px solid var(--color-line);
  background: var(--color-bg2);
}
.back-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  margin-bottom: 12px;
  background: transparent;
  border: none;
  border-radius: 6px;
  color: var(--color-t3);
  font-size: 0.75rem;
  cursor: pointer;
  transition: color .15s, background-color .15s;
}
.back-btn:hover { color: var(--color-t2); background: var(--color-bg3); }
.sub-nav-title {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-t1);
  margin: 0 6px 16px;
  letter-spacing: -0.01em;
}
.link-list { display: flex; flex-direction: column; gap: 2px; }
.nav-link {
  display: block;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 0.8125rem;
  color: var(--color-t2);
  text-decoration: none;
  transition: background-color .12s, color .12s;
}
.nav-link:hover { background: var(--color-bg3); color: var(--color-t1); }
.nav-link.is-active {
  /* Use the design-system's selected-state token so the
     active sub-nav link matches the first-level sidebar's
     active style (which uses ``bg-bg-selected``). Without
     this, the sub-nav's active state was just one shade off
     bg2 and read as much weaker than the main tabs. */
  background: var(--color-bg-selected);
  color: var(--color-t1);
  font-weight: 500;
}
.settings-content {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 32px 40px;
  /* Match the sub-nav's bg2 so the entire Settings surface
     reads as one canvas. Cards inside use ``var(--color-bg)``
     which is one step darker — they sit slightly recessed,
     same layering Vercel's settings pages use in dark mode. */
  background: var(--color-bg2);
}
</style>
