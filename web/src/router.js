import { createRouter, createWebHistory } from 'vue-router'
import { getMe } from '@/api/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', component: () => import('@/views/Login.vue'), meta: { public: true } },
    { path: '/chat', component: () => import('@/views/Chat.vue') },
    { path: '/search', component: () => import('@/views/Search.vue') },
    { path: '/workspace', component: () => import('@/views/Workspace.vue') },
    {
      // Legacy redirect: old /repository?doc=X links land on /workspace?doc=X.
      // Workspace embeds DocDetail.vue for the focused-doc view; the
      // original Repository.vue component has been removed.
      path: '/repository',
      redirect: (to) => ({ path: '/workspace', query: to.query }),
    },
    { path: '/ingestion', redirect: '/workspace' },
    { path: '/knowledge-graph', component: () => import('@/views/KnowledgeGraph.vue') },
    { path: '/simulation', component: () => import('@/views/Simulation.vue') },
    { path: '/metrics', component: () => import('@/views/Metrics.vue') },
    { path: '/benchmark', component: () => import('@/views/Benchmark.vue') },
    { path: '/tokens', component: () => import('@/views/Tokens.vue') },
    // ── Settings ─────────────────────────────────────────────────
    // /settings is a shell with its own left sub-nav rendered inside
    // ``Settings.vue``. Sub-routes (account / preferences / users
    // / system / audit) load nested components into Settings.vue's
    // <router-view>. Admin-only sub-routes carry meta.requiresAdmin
    // and are gated client-side AND by route guard (router.beforeEach
    // below) — the same gate the backend enforces on /admin APIs.
    {
      path: '/settings',
      component: () => import('@/views/Settings.vue'),
      redirect: '/settings/profile',
      children: [
        { path: 'profile', component: () => import('@/views/settings/Profile.vue') },
        {
          path: 'users',
          component: () => import('@/views/settings/Users.vue'),
          meta: { requiresAdmin: true },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/chat' },
  ],
})

// Admin gate for /settings/* sub-routes flagged with
// ``meta.requiresAdmin``. Mirrors the backend's role check on the
// admin APIs — the UI never renders these links for non-admins
// (Settings.vue filters them out of the sub-nav), but a hand-typed
// URL still needs bouncing. ``getMe`` is cached one tick by the
// browser; we don't keep our own cache to avoid stale-after-promotion
// edge cases (admin demotes a user → the UI should reflect on next
// nav, not after a reload).
let _meCache = null
let _meCacheAt = 0
async function fetchMeCached() {
  const now = Date.now()
  if (_meCache && now - _meCacheAt < 5000) return _meCache
  try {
    _meCache = await getMe()
    _meCacheAt = now
  } catch {
    _meCache = null
    _meCacheAt = now
  }
  return _meCache
}

router.beforeEach(async (to) => {
  if (!to.matched.some((r) => r.meta?.requiresAdmin)) return true
  const me = await fetchMeCached()
  if (!me || me.role !== 'admin') {
    // Non-admin URL access → bounce to the always-visible profile
    // page rather than the route's parent (avoids redirect loops
    // when /settings itself was the typed URL).
    return { path: '/settings/profile', replace: true }
  }
  return true
})

export default router
